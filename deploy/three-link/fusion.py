#!/usr/bin/env python3
"""Send simulated commands over three links and replay the receiver log."""

import argparse
import base64
from collections import Counter, defaultdict, deque
from contextlib import closing
import csv
from datetime import datetime
import glob
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import zlib

import serial
from serial.tools import list_ports


LINKS = ('lora', 'wifi', '4g')
MAGIC = b'YT'
LEGACY_MAGIC = b'YR'
BODY = struct.Struct('!2sQIh')
CRC = struct.Struct('!I')
SIZE = BODY.size + CRC.size
END = 0xffffffff
RADIO_ANCHORS = {'lora': (-125, -75), 'wifi': (-85, -55), '4g': (-110, -80)}
RADIO_SAMPLE_MAX_AGE_NS = 6_000_000_000
CLOCK_SYNC_MAX_RTT_NS = 250_000_000
BRIDGE_STATUS_PATH = '/Main_WStatus_Content.asp'


def radio_signal(link, dbm, *, rsrq=None):
    weak, strong = RADIO_ANCHORS[link]
    width = strong - weak
    exponent = (dbm - (weak + strong) / 2) * (2 * math.log(9) / width)
    return dict(metric='RSRP' if link == '4g' else 'RSSI', dbm=dbm, rsrq_db=rsrq,
                quality_0_1=1 / (1 + math.exp(-exponent)), weak_dbm=weak, strong_dbm=strong)


def format_run(run):
    digits = f'{run:014d}'
    minute, occurrence = f'{digits[:8]}-{digits[8:12]}', digits[12:]
    return minute if occurrence == '01' else f'{minute}-{occurrence}'


def reserve_run(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    minute = int(datetime.now().strftime('%Y%m%d%H%M')) * 100
    for occurrence in range(1, 100):
        run = minute + occurrence
        try:
            with (directory / f'tx-{format_run(run)}.jsonl').open('x', encoding='utf-8'):
                return run
        except FileExistsError:
            continue
    raise OSError('同一分钟已创建 99 轮实验，请在下一分钟重试。')


def encode(run, seq, value):
    body = BODY.pack(MAGIC, run, seq, value)
    return body + CRC.pack(zlib.crc32(body))


def decode(data):
    if len(data) != SIZE or data[:2] not in (MAGIC, LEGACY_MAGIC):
        return None
    body = data[:-CRC.size]
    if zlib.crc32(body) != CRC.unpack(data[-CRC.size:])[0]:
        return None
    magic, run, seq, value = BODY.unpack(body)
    return (format_run(run) if magic == MAGIC else f'{run:016x}', seq, value)


class FrameStream:
    def __init__(self, radio_trailer=False):
        self.buffer = bytearray()
        self.radio_trailer = radio_trailer

    def feed(self, chunk, idle=False):
        self.buffer.extend(chunk)
        while len(self.buffer) >= SIZE:
            offset = self.buffer.find(b'Y')
            if offset < 0:
                del self.buffer[:-1]
                return
            del self.buffer[:offset]
            if len(self.buffer) < SIZE:
                return
            frame = decode(self.buffer[:SIZE])
            if not frame:
                del self.buffer[:1]
                continue
            consumed = SIZE
            dbm = None
            if self.radio_trailer:
                tail = self.buffer[SIZE:SIZE + 2]
                if (not tail or tail == b'Y') and not idle:
                    return
                if tail and tail not in (MAGIC, LEGACY_MAGIC):
                    consumed += 1
                    if tail[0] <= 146:
                        dbm = tail[0] - 146
            del self.buffer[:consumed]
            yield frame + (dbm,) if self.radio_trailer else frame


def read_serving_lte(modem):
    modem.reset_input_buffer()
    modem.write(b'AT+QENG="servingcell"\r')
    sample = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        line = modem.readline().decode('ascii', errors='replace').strip()
        if line == 'OK':
            return sample
        if line == 'ERROR' or line.startswith('+CME ERROR'):
            return None
        if not line.startswith('+QENG:'):
            continue
        fields = next(csv.reader([line.split(':', 1)[1].strip()]))
        if len(fields) >= 17 and fields[0] == 'servingcell' and fields[2] == 'LTE':
            try:
                rsrp, rsrq = int(fields[13]), int(fields[14])
            except ValueError:
                continue
            if -156 <= rsrp <= -30 and -40 <= rsrq <= 0:
                sample = radio_signal('4g', rsrp, rsrq=rsrq)
    return sample


def read_bridge_signal(host, source, auth):
    with closing(http.client.HTTPConnection(host, timeout=2, source_address=(source, 0))) as conn:
        conn.request('GET', BRIDGE_STATUS_PATH, headers={'Authorization': auth, 'Cache-Control': 'no-cache'})
        response = conn.getresponse()
        if response.status != 200:
            raise OSError(f'Bridge wireless status returned HTTP {response.status}')
        page = response.read(65536).decode('utf-8', errors='replace')
    section = re.search(r'AP-Client Connection\s*\n-+\s*\n[^\r\n]*\bRSSI[^\r\n]*\r?\n([^\r\n<]+)', page)
    if not section:
        return None
    fields = section[1].split()
    if len(fields) < 2 or not re.fullmatch(r'(?:[\da-f]{2}:){5}[\da-f]{2}', fields[0], re.I):
        return None
    try:
        rssi = int(fields[-1])
    except ValueError:
        return None
    return radio_signal('wifi', rssi) if -130 <= rssi < 0 else None


class Journal:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a', encoding='utf-8', buffering=1)

    def write(self, **fields):
        self.file.write(json.dumps(fields, separators=(',', ':')) + '\n')


def windows_serial_port(config, role, ports=None):
    key, label = ('serial', 'LoRa') if role == 'lora' else ('lte_at', '移远 4G AT')
    if not config.get('serial_auto', True):
        return config[key]
    devices = list_ports.comports() if ports is None else ports
    if role == 'lora':
        matches = [port.device for port in devices if (port.vid, port.pid) == (0x1a86, 0x7523)]
    else:
        matches = [port.device for port in devices if port.vid == 0x2c7c and
                   re.search(r'\bAT(?:\s+command)?\s+port\b', port.description or '', re.I)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise OSError(f'{label} 检测到多个候选串口（{", ".join(sorted(matches))}），请在接口设置中手动指定。')
    raise OSError(f'未检测到 {label} 串口，请检查 USB 连接和设备驱动。')


def open_serial(port):
    line = serial.Serial(baudrate=9600, timeout=0.2, write_timeout=1)
    line.dtr = line.rts = False
    line.port = port
    line.open()
    return line


def windows_interface_index(source):
    source = str(ipaddress.IPv4Address(source))
    script = ("$a=@(Get-NetIPAddress -AddressFamily IPv4 -IPAddress '" + source +
              "' -ErrorAction Stop | Select-Object -ExpandProperty InterfaceIndex -Unique); "
              "if ($a.Count -ne 1) { exit 2 }; [Console]::Out.Write([string]$a[0])")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise OSError(f'未能通过 4G 网卡 IP {source} 唯一确定 Windows 接口索引。')
    try:
        return int(result.stdout.strip())
    except ValueError as error:
        raise OSError(f'Windows 返回了无效的网卡索引：{result.stdout!r}') from error


def connect_lte(host, port, interface, source=None):
    with socket.socket() as conn:
        if os.name == 'nt':
            if source:
                interface = windows_interface_index(source)
            conn.setsockopt(socket.IPPROTO_IP, 31, struct.pack('!I', int(interface)))
        else:
            result = subprocess.run(['ip', '-j', '-4', 'route', 'get', host, 'oif', interface],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode:
                raise OSError(result.stderr.strip())
            route = json.loads(result.stdout)[0]
            if route.get('dev') != interface or not route.get('prefsrc'):
                raise OSError('No IPv4 source on ' + interface)
            # Pin both the device and source. A disappearing route must not fall back to eth0.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
            source = route['prefsrc']
        conn.bind((source, 0))
        conn.settimeout(2)
        conn.connect((host, port))
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn.dup()


class Receiver:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        path = Path(config['log'])
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.latest = {}
        self.radio_samples = {}
        if path.exists():
            for row in read_log(path):
                if row['outcome'] == 'adopted':
                    run = row['run']
                    self.latest[run] = max(self.latest.get(run, 0), row['seq'])
        self.journal = Journal(path)

    def accept(self, link, frame, signal=None):
        run, seq, value = frame
        with self.lock:
            now = time.monotonic_ns()
            if link in self.radio_samples:
                sample_time, reading = self.radio_samples[link]
                if now - sample_time <= RADIO_SAMPLE_MAX_AGE_NS:
                    signal = dict(reading, age_ms=(now - sample_time) / 1e6)
            top = self.latest.get(run, 0)
            if seq in (0, END):
                result = 'start' if seq == 0 else 'end'
            elif seq > top:
                self.latest[run] = seq
                result = 'adopted'
            else:
                result = 'duplicate' if seq == top else 'late'
            self.journal.write(run=run, seq=seq, value=value, link=link, outcome=result,
                               mono_ns=now, rx_mono_ns=now, utc_ns=time.time_ns(), boot=self.boot, radio=signal)

    def wifi(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
            conn.bind((self.config['wifi_ip'], self.config['wifi_port']))
            print('wifi: listening', flush=True)
            while True:
                data, peer = conn.recvfrom(4096)
                frame = decode(data)
                if frame and peer[0] == self.config['wifi_peer']:
                    self.accept('wifi', frame)

    def lora(self):
        with open_serial(self.config['serial']) as conn:
            print('lora: listening', flush=True)
            stream = FrameStream(radio_trailer=self.config.get('lora_rssi_suffix', False))
            while True:
                chunk = conn.read(conn.in_waiting or 1)
                for frame in stream.feed(chunk, idle=not chunk):
                    dbm = frame[3] if len(frame) > 3 else None
                    self.accept('lora', frame[:3], radio_signal('lora', dbm, rsrq=None) if dbm is not None else None)

    def lte_radio(self):
        pattern = self.config.get('lte_at', '/dev/serial/by-id/usb-Quectel_EC20*-if02-port0')
        ports = glob.glob(pattern)
        if len(ports) != 1:
            raise OSError('Expected one EC20 AT port')
        with serial.Serial(ports[0], 115200, timeout=0.2, write_timeout=1) as modem:
            while True:
                reading = read_serving_lte(modem)
                self.save_radio('4g', reading)
                time.sleep(2)

    def wifi_radio(self):
        path = Path(self.config.get('bridge_auth', '/etc/yrt-fusion/bridge-auth.json'))
        try:
            settings = json.loads(path.read_text(encoding='utf-8'))
            secret = f"{settings['username']}:{settings['password']}".encode()
            host, source = settings['host'], settings['source']
        except (ValueError, KeyError, TypeError) as error:
            raise OSError('Invalid bridge wireless status configuration') from error
        auth = 'Basic ' + base64.b64encode(secret).decode('ascii')
        announced = False
        while True:
            reading = read_bridge_signal(host, source, auth)
            self.save_radio('wifi', reading)
            if reading and not announced:
                print(f"wifi: bridge wireless status reports RSSI {reading['dbm']} dBm", flush=True)
                announced = True
            time.sleep(2)

    def save_radio(self, link, reading):
        with self.lock:
            if reading is None:
                self.radio_samples.pop(link, None)
            else:
                self.radio_samples[link] = (time.monotonic_ns(), reading)

    def lte(self):
        with connect_lte(self.config['ecs'], self.config['lte_port'], self.config['lte_interface'],
                         self.config.get('lte_ip')) as conn:
            print('4g: connected to ECS', flush=True)
            conn.settimeout(None)
            # Detect a lost modem or a stale relay session even when no run is active.
            for option, value in ((socket.TCP_KEEPIDLE, 15), (socket.TCP_KEEPINTVL, 5), (socket.TCP_KEEPCNT, 3)):
                conn.setsockopt(socket.IPPROTO_TCP, option, value)
            stream = FrameStream()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    raise OSError('ECS closed the connection')
                for frame in stream.feed(chunk):
                    self.accept('4g', frame)

    @staticmethod
    def maintain(link, listen):
        while True:
            try:
                listen()
            except (OSError, http.client.HTTPException, subprocess.SubprocessError) as error:
                print(f'{link}: {error}', flush=True)
                time.sleep(2)

    def run(self):
        sources = list(zip(LINKS + ('EC20 measurements',), (self.lora, self.wifi, self.lte, self.lte_radio)))
        if Path(self.config.get('bridge_auth', '/etc/yrt-fusion/bridge-auth.json')).is_file():
            sources.append(('Bridge measurements', self.wifi_radio))
        listeners = [(name, threading.Thread(target=self.maintain, args=(name, listen), daemon=True))
                     for name, listen in sources]
        for _, thread in listeners:
            thread.start()
        while True:
            for name, thread in listeners:
                if not thread.is_alive():
                    raise RuntimeError(f'{name} listener stopped')
            time.sleep(1)


def open_sender(link, config):
    if link == 'lora':
        port = windows_serial_port(config, 'lora') if os.name == 'nt' else config['serial']
        return open_serial(port)
    if link == '4g':
        return connect_lte(config['ecs'], config['lte_port'], config['lte_interface'], config['lte_ip'])
    conn = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        conn.bind((config['wifi_ip'], 0))
        conn.settimeout(1)
        conn.connect((config['wifi_peer'], config['wifi_port']))
        return conn
    except OSError:
        conn.close()
        raise


def synchronize_monotonic_clock(config, *, samples=3):
    """Estimate board-minus-PC monotonic offset over SSH; wall clocks are irrelevant."""
    ssh = shutil.which('ssh.exe') or shutil.which('ssh')
    if not ssh:
        return {'status': 'failed', 'error': '未找到 OpenSSH'}
    destination = f"ztl@{config['wifi_peer']}"
    remote = r'''python3 -u -c 'import json,sys,time; boot=open("/proc/sys/kernel/random/boot_id").read().strip(); exec("for _ in sys.stdin:\n t2=time.monotonic_ns(); t3=time.monotonic_ns(); print(json.dumps({\"t2\":t2,\"t3\":t3,\"boot\":boot}),flush=True)")' '''.strip()
    observations = []
    output = queue.Queue()
    try:
        process = subprocess.Popen(
            [ssh, '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', destination, remote],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError as exc:
        return {'status': 'failed', 'error': f'无法启动 SSH 校时 · {exc}'}

    def collect():
        if process.stdout is not None:
            for line in process.stdout:
                output.put(line)

    threading.Thread(target=collect, daemon=True).start()
    try:
        for _ in range(samples + 2):
            before = time.perf_counter_ns()
            try:
                process.stdin.write('sync\n')
                process.stdin.flush()
                deadline = time.monotonic() + 2
                payload = None
                while time.monotonic() < deadline:
                    try:
                        line = output.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    try:
                        payload = json.loads(line)
                        if {'t2', 't3', 'boot'} <= payload.keys():
                            break
                    except (json.JSONDecodeError, AttributeError):
                        payload = None
                after = time.perf_counter_ns()
                if not payload or not {'t2', 't3', 'boot'} <= payload.keys():
                    continue
                t2, t3 = int(payload['t2']), int(payload['t3'])
                rtt = after - before
                offset = ((t2 - before) + (t3 - after)) // 2
                observations.append({'offset_ns': offset, 'rtt_ns': rtt, 'boot': str(payload['boot'])})
            except (OSError, ValueError):
                break
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=1)
    if not observations:
        return {'status': 'failed', 'error': 'SSH 时钟校准失败，请检查免密 SSH 和网桥连接。'}
    best = min(observations, key=lambda sample: sample['rtt_ns'])
    return {
        'status': 'ok' if best['rtt_ns'] <= CLOCK_SYNC_MAX_RTT_NS else 'uncertain',
        'offset_ns': best['offset_ns'],
        'rtt_ns': best['rtt_ns'],
        'boot': best['boot'],
        'samples': len(observations),
    }


def sender(link, pending, config, journal, lock, clock_sync):
    conn = None
    try:
        while True:
            item = pending.get()
            if item is None:
                return
            run_id, seq, value, frame, deadline = item
            started = time.monotonic_ns()
            tx_send_mono_ns = None
            tx_utc_ns = None
            error = None
            try:
                if time.monotonic() >= deadline:
                    raise OSError('Timed out waiting for interface')
                if conn is None:
                    conn = open_sender(link, config)
                if time.monotonic() >= deadline:
                    raise OSError('Interface became available after the deadline')
                tx_send_mono_ns = time.perf_counter_ns()
                tx_utc_ns = time.time_ns()
                if link == 'lora':
                    if conn.write(frame) != len(frame):
                        raise OSError('Incomplete serial write')
                else:
                    conn.sendall(frame)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
                if conn is not None:
                    conn.close()
                    conn = None
            with lock:
                journal.write(run=run_id, seq=seq, value=value, link=link, outcome='failed' if error else 'submitted',
                              started_ns=started, mono_ns=time.monotonic_ns(), utc_ns=time.time_ns(),
                              tx_send_mono_ns=tx_send_mono_ns, tx_utc_ns=tx_utc_ns,
                              clock_sync_status=clock_sync.get('status', 'failed'),
                              clock_offset_ns=clock_sync.get('offset_ns'),
                              clock_sync_rtt_ns=clock_sync.get('rtt_ns'),
                              clock_sync_boot=clock_sync.get('boot'), error=error)
    finally:
        if conn is not None:
            conn.close()


def transmit(config, hz, seqs, omitted, *, run=None, stop=None, clock_sync=None):
    if clock_sync is None:
        clock_sync = synchronize_monotonic_clock(config) if os.name == 'nt' else {'status': 'failed'}
    run = reserve_run(config['logs']) if run is None else run
    stop = stop if stop is not None else threading.Event()
    run_id = format_run(run)
    path = Path(config['logs']) / f'tx-{run_id}.jsonl'
    journal = Journal(path)
    lock = threading.Lock()
    queues = {link: queue.Queue() for link in LINKS if link not in omitted}
    workers = [threading.Thread(target=sender, args=(link, pending, config, journal, lock, clock_sync))
               for link, pending in queues.items()]
    for worker in workers:
        worker.start()

    def publish(seq, value):
        item = (run_id, seq, value, encode(run, seq, value), time.monotonic() + 5)
        for pending in queues.values():
            pending.put(item)

    print(f'RUN={run_id} LOG={path}', flush=True)
    try:
        publish(0, 0)
        stop.wait(2)
        next_send = time.monotonic()
        for seq in seqs:
            if stop.wait(max(0, next_send - time.monotonic())):
                break
            publish(seq, seq % 201 - 100)
            next_send += 1 / hz
    except KeyboardInterrupt:
        print('Stopping run', flush=True)
    finally:
        time.sleep(2)
        publish(END, 0)
        for pending in queues.values():
            pending.put(None)
        for worker in workers:
            worker.join()
        journal.file.close()
    return clock_sync


def read_log(path):
    with Path(path).open(encoding='utf-8') as file:
        for line in file:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f'Skipping incomplete log record in {path}', flush=True)


def arrival_details(rows, tx_rows=()):
    """Join same-run monotonic timestamps using the per-run SSH clock calibration."""
    experiment_start = {}
    first_copy = {}
    pending_tx = defaultdict(deque)
    for tx in tx_rows:
        if tx.get('outcome') == 'submitted':
            pending_tx[tx['run'], tx['seq'], tx['link']].append(tx)
    order = 0
    for row in rows:
        tx = None
        copies = pending_tx[row['run'], row['seq'], row['link']]
        if copies:
            tx = copies.popleft()
        sent_mono_ns = tx.get('tx_send_mono_ns') if tx else None
        received_mono_ns = row.get('rx_mono_ns', row.get('mono_ns'))
        offset_ns = tx.get('clock_offset_ns') if tx else None
        sync_rtt_ns = tx.get('clock_sync_rtt_ns') if tx else None
        send_delay_ms = None
        send_time_status = 'missing_tx' if tx is None else 'missing_sync'
        sync_status = tx.get('clock_sync_status') if tx else None
        if sync_status in ('ok', 'uncertain') and isinstance(sent_mono_ns, int) and isinstance(offset_ns, int) and isinstance(received_mono_ns, int):
            if tx.get('clock_sync_boot') and row.get('boot') != tx['clock_sync_boot']:
                send_time_status = 'board_rebooted'
            else:
                send_delay_ns = received_mono_ns - sent_mono_ns - offset_ns
                send_delay_ms = send_delay_ns / 1e6
                if send_delay_ns < 0:
                    send_time_status = ('sync_negative_uncertain'
                                        if -send_delay_ns <= (sync_rtt_ns or 0) else 'sync_check')
                elif sync_status == 'uncertain':
                    send_time_status = 'sync_uncertain'
                elif send_delay_ns <= (sync_rtt_ns or 0) // 2:
                    send_time_status = 'below_sync_resolution'
                else:
                    send_time_status = 'ok'
        elif sync_status == 'failed':
            send_time_status = 'sync_failed'
        if not 0 < row['seq'] < END:
            yield dict(row, tx_mono_ns=sent_mono_ns, clock_offset_ns=offset_ns,
                       clock_sync_rtt_ns=sync_rtt_ns, send_to_receive_ms=send_delay_ms,
                       send_time_status=send_time_status)
            continue
        order += 1
        clock = received_mono_ns
        boot = row.get('boot')
        start = experiment_start.setdefault(boot, clock)
        first = first_copy.setdefault((boot, row['seq']), clock)
        delay_ms = (clock - first) / 1e6
        yield dict(row, rx_order=order, rx_mono_ns=clock, rx_elapsed_ms=(clock - start) / 1e6,
                   relative_delay_ms=delay_ms, tx_mono_ns=sent_mono_ns, clock_offset_ns=offset_ns,
                   clock_sync_rtt_ns=sync_rtt_ns, send_to_receive_ms=send_delay_ms,
                   send_time_status=send_time_status)


def summarize(path, run, omitted=()):
    rows = [row for row in read_log(path) if row['run'] == run]
    if not rows:
        raise ValueError('Run not found')
    if len({row.get('boot') for row in rows}) > 1:
        raise ValueError('Receiver rebooted during this run. Monotonic times cannot be compared across boots.')
    commands = [row for row in rows if 0 < row['seq'] < END]
    first = min(row['mono_ns'] for row in rows)
    last = max(row['mono_ns'] for row in rows)
    complete = any(row['seq'] == 0 for row in rows) and any(row['seq'] == END for row in rows)
    top = 0
    adopted = []
    sources = Counter()
    for row in commands:
        if row['link'] not in omitted and row['seq'] > top:
            top = row['seq']
            adopted.append(row['mono_ns'])
            sources[row['link']] += 1
    points = [first] + adopted + [last]
    gap_ms = max(b - a for a, b in zip(points, points[1:])) / 1e6
    return (complete, Counter(row['link'] for row in commands), Counter(row['outcome'] for row in commands),
            (len(adopted), gap_ms, dict(sources)))


def report(path, run, omitted=()):
    try:
        complete, links, outcomes, (updates, gap_ms, sources) = summarize(path, run, omitted)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print('Observation bounds:', 'start and end received' if complete else 'partial reception')
    print('Received copies:', dict(links))
    print('Receiver outcomes:', dict(outcomes))
    scope = 'without ' + ', '.join(omitted) if omitted else 'all links'
    print(f'{scope}: {updates} updates, longest gap {gap_ms:.1f} ms, adopted by source {sources}')


def merge(tx_path, rx_path, output):
    sent = list(read_log(tx_path))
    if not sent:
        raise SystemExit('Sender log is empty')
    run = sent[0]['run']
    pending = defaultdict(deque)
    for index, row in enumerate(sent):
        if row['outcome'] == 'submitted':
            pending[row['seq'], row['link']].append(index)
    matched = set()
    received = arrival_details((row for row in read_log(rx_path) if row['run'] == run), sent)

    def merge_row(tx, rx):
        row = tx or rx
        signal = rx.get('radio') or {}
        return (run, row['seq'], row['link'], row['value'], tx.get('outcome'), tx.get('mono_ns'),
                tx.get('error'), rx.get('outcome'), rx.get('mono_ns'), rx.get('boot'),
                rx.get('rx_order'), rx.get('rx_elapsed_ms'), rx.get('relative_delay_ms'),
                tx.get('tx_utc_ns', tx.get('utc_ns')), rx.get('utc_ns'),
                tx.get('tx_send_mono_ns'),
                rx.get('clock_offset_ns'), rx.get('clock_sync_rtt_ns'), rx.get('send_to_receive_ms'),
                rx.get('send_time_status'), signal.get('metric'),
                signal.get('dbm'), signal.get('rsrq_db'), signal.get('age_ms'), signal.get('weak_dbm'),
                signal.get('strong_dbm'), signal.get('quality_0_1'))

    with output.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(('run', 'seq', 'link', 'value', 'tx_result', 'tx_mono_ns', 'tx_error',
                         'rx_result', 'rx_mono_ns', 'rx_boot', 'rx_order', 'rx_elapsed_ms', 'relative_delay_ms',
                         'tx_utc_ns', 'rx_utc_ns', 'tx_send_mono_ns', 'clock_offset_ns',
                         'clock_sync_rtt_ns', 'send_to_receive_ms', 'send_time_status',
                         'rf_metric', 'rf_dbm', 'rf_rsrq_db', 'rf_sample_age_ms', 'rf_weak_dbm',
                         'rf_strong_dbm', 'rf_quality_0_1'))
        for rx in received:
            copies = pending[rx['seq'], rx['link']]
            tx = {}
            if copies:
                index = copies.popleft()
                tx = sent[index]
                matched.add(index)
            writer.writerow(merge_row(tx, rx))
        for index, tx in enumerate(sent):
            if index not in matched:
                writer.writerow(merge_row(tx, {}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='mode', required=True)
    for mode in ('receive', 'send'):
        sub = subs.add_parser(mode)
        sub.add_argument('config', type=Path)
        if mode == 'send':
            sub.add_argument('--count', type=int, default=20)
            sub.add_argument('--hz', type=float, default=1)
            sub.add_argument('--sequences', type=lambda value: [int(s) for s in value.split(',')])
            sub.add_argument('--omit', action='append', default=[], choices=LINKS)
    replay = subs.add_parser('report')
    replay.add_argument('log', type=Path)
    replay.add_argument('run')
    replay.add_argument('--omit', action='append', default=[], choices=LINKS)
    combine = subs.add_parser('merge')
    combine.add_argument('tx', type=Path)
    combine.add_argument('rx', type=Path)
    combine.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.mode == 'report':
        if set(args.omit) == set(LINKS):
            parser.error('at least one link must be enabled')
        report(args.log, args.run, args.omit)
    elif args.mode == 'merge':
        merge(args.tx, args.rx, args.output)
    else:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        if args.mode == 'receive':
            Receiver(config).run()
        else:
            seqs = args.sequences if args.sequences else range(1, args.count + 1)
            if not 0 < args.hz <= 10 or not seqs or not (0 < min(seqs) <= max(seqs) < END):
                parser.error('require 0 < hz <= 10 and sequences within 1..4294967294')
            if set(args.omit) == set(LINKS):
                parser.error('at least one link must be enabled')
            transmit(config, args.hz, seqs, args.omit)


if __name__ == '__main__':
    main()
