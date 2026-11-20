#!/usr/bin/env python3
"""Send simulated commands over three links and replay the receiver log."""

import argparse
from collections import Counter, defaultdict, deque
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import threading
import time
import zlib

import serial


LINKS = ('lora', 'wifi', '4g')
MAGIC = b'YT'
LEGACY_MAGIC = b'YR'
BODY = struct.Struct('!2sQIh')
CRC = struct.Struct('!I')
SIZE = BODY.size + CRC.size
END = 0xffffffff


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
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
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
            del self.buffer[:SIZE if frame else 1]
            if frame:
                yield frame


class Journal:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a', encoding='utf-8', buffering=1)

    def write(self, **fields):
        self.file.write(json.dumps(fields, separators=(',', ':')) + '\n')


def open_serial(port):
    line = serial.Serial(baudrate=9600, timeout=0.2, write_timeout=1)
    line.dtr = line.rts = False
    line.port = port
    line.open()
    return line


def connect_lte(host, port, interface, source=None):
    with socket.socket() as conn:
        if os.name == 'nt':
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
        if path.exists():
            for row in read_log(path):
                if row['outcome'] == 'adopted':
                    run = row['run']
                    self.latest[run] = max(self.latest.get(run, 0), row['seq'])
        self.journal = Journal(path)

    def accept(self, link, frame):
        run, seq, value = frame
        with self.lock:
            now = time.monotonic_ns()
            top = self.latest.get(run, 0)
            if seq in (0, END):
                result = 'start' if seq == 0 else 'end'
            elif seq > top:
                self.latest[run] = seq
                result = 'adopted'
            else:
                result = 'duplicate' if seq == top else 'late'
            self.journal.write(run=run, seq=seq, value=value, link=link, outcome=result,
                               mono_ns=now, utc_ns=time.time_ns(), boot=self.boot)

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
            stream = FrameStream()
            while True:
                for frame in stream.feed(conn.read(conn.in_waiting or 1)):
                    self.accept('lora', frame)

    def lte(self):
        with connect_lte(self.config['ecs'], self.config['lte_port'], self.config['lte_interface']) as conn:
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
            except (OSError, subprocess.SubprocessError) as error:
                print(f'{link}: {error}', flush=True)
                time.sleep(2)

    def run(self):
        listeners = [(name, threading.Thread(target=self.maintain, args=(name, listen), daemon=True))
                     for name, listen in zip(LINKS, (self.lora, self.wifi, self.lte))]
        for _, thread in listeners:
            thread.start()
        while True:
            for name, thread in listeners:
                if not thread.is_alive():
                    raise RuntimeError(f'{name} listener stopped')
            time.sleep(1)


def open_sender(link, config):
    if link == 'lora':
        return open_serial(config['serial'])
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


def sender(link, pending, config, journal, lock):
    conn = None
    try:
        while True:
            item = pending.get()
            if item is None:
                return
            run_id, seq, value, frame, deadline = item
            started = time.monotonic_ns()
            error = None
            try:
                if time.monotonic() >= deadline:
                    raise OSError('Timed out waiting for interface')
                if conn is None:
                    conn = open_sender(link, config)
                if time.monotonic() >= deadline:
                    raise OSError('Interface became available after the deadline')
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
                              started_ns=started, mono_ns=time.monotonic_ns(), utc_ns=time.time_ns(), error=error)
    finally:
        if conn is not None:
            conn.close()


def transmit(config, hz, seqs, omitted, *, run=None, stop=None):
    run = reserve_run(config['logs']) if run is None else run
    stop = stop if stop is not None else threading.Event()
    run_id = format_run(run)
    path = Path(config['logs']) / f'tx-{run_id}.jsonl'
    journal = Journal(path)
    lock = threading.Lock()
    queues = {link: queue.Queue() for link in LINKS if link not in omitted}
    workers = [threading.Thread(target=sender, args=(link, pending, config, journal, lock))
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


def read_log(path):
    with Path(path).open(encoding='utf-8') as file:
        for line in file:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f'Skipping incomplete log record in {path}', flush=True)


def summarize(path, run):
    rows = [row for row in read_log(path) if row['run'] == run]
    if not rows:
        raise ValueError('Run not found')
    if len({row.get('boot') for row in rows}) > 1:
        raise ValueError('Receiver rebooted during this run. Monotonic times cannot be compared across boots.')
    commands = [row for row in rows if 0 < row['seq'] < END]
    first = min(row['mono_ns'] for row in rows)
    last = max(row['mono_ns'] for row in rows)
    complete = any(row['seq'] == 0 for row in rows) and any(row['seq'] == END for row in rows)
    comparisons = []
    for excluded in (None,) + LINKS:
        top = 0
        adopted = []
        sources = Counter()
        for row in commands:
            if row['link'] != excluded and row['seq'] > top:
                top = row['seq']
                adopted.append(row['mono_ns'])
                sources[row['link']] += 1
        points = [first] + adopted + [last]
        gap_ms = max(b - a for a, b in zip(points, points[1:])) / 1e6
        comparisons.append((excluded, len(adopted), gap_ms, dict(sources)))
    return complete, Counter(row['link'] for row in commands), Counter(row['outcome'] for row in commands), comparisons


def report(path, run):
    try:
        complete, links, outcomes, comparisons = summarize(path, run)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print('Observation bounds:', 'start and end received' if complete else 'partial reception')
    print('Received copies:', dict(links))
    print('Receiver outcomes:', dict(outcomes))
    for excluded, updates, gap_ms, sources in comparisons:
        print(f"{'without ' + excluded if excluded else 'all links'}: {updates} updates, "
              f'longest gap {gap_ms:.1f} ms, adopted by source {sources}')


def merge(tx_path, rx_path, output):
    sent = list(read_log(tx_path))
    if not sent:
        raise SystemExit('Sender log is empty')
    run = sent[0]['run']
    received = defaultdict(deque)
    for row in read_log(rx_path):
        if row['run'] == run:
            received[row['seq'], row['link']].append(row)

    def merge_row(tx, rx):
        row = tx or rx
        return (run, row['seq'], row['link'], row['value'], tx.get('outcome'), tx.get('mono_ns'),
                tx.get('error'), rx.get('outcome'), rx.get('mono_ns'), rx.get('boot'))

    with output.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(('run', 'seq', 'link', 'value', 'tx_result', 'tx_mono_ns', 'tx_error',
                         'rx_result', 'rx_mono_ns', 'rx_boot'))
        for row in sent:
            copies = received[row['seq'], row['link']]
            rx = copies.popleft() if copies else {}
            writer.writerow(merge_row(row, rx))
        for copies in received.values():
            for rx in copies:
                writer.writerow(merge_row({}, rx))


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
    combine = subs.add_parser('merge')
    combine.add_argument('tx', type=Path)
    combine.add_argument('rx', type=Path)
    combine.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.mode == 'report':
        report(args.log, args.run)
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
