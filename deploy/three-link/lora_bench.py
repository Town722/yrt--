"""Measure one-way serial-radio latency using a single PC's monotonic clock."""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import random
import secrets
import statistics
import struct
import sys
import threading
import time
import zlib

import serial
from serial.tools import list_ports


# The bench uses the same frame length as the ground station, with its own magic.
MAGIC = b'LB'
BODY = struct.Struct('!2sQIH')
CRC = struct.Struct('!I')
FRAME_SIZE = BODY.size + CRC.size
RECEIVE_GRACE_SEC = 5


def pack_frame(session, seq, frame_size=FRAME_SIZE):
    body = BODY.pack(MAGIC, session, seq, 0) + b'\xa5' * (frame_size - FRAME_SIZE)
    return body + CRC.pack(zlib.crc32(body))


class FrameReader:
    def __init__(self, session, frame_size=FRAME_SIZE):
        self.session = session
        self.frame_size = frame_size
        self.data = bytearray()
        self.timestamps = []

    def feed(self, byte, now_ns):
        self.data.extend(byte)
        self.timestamps.append(now_ns)
        while self.data:
            if not MAGIC.startswith(self.data[:len(MAGIC)]):
                del self.data[0]
                del self.timestamps[0]
                continue
            if len(self.data) < self.frame_size:
                return None
            crc_at = self.frame_size - CRC.size
            body = self.data[:crc_at]
            if zlib.crc32(body) != CRC.unpack_from(self.data, crc_at)[0]:
                del self.data[0]
                del self.timestamps[0]
                continue
            _, session, seq, _ = BODY.unpack_from(body)
            first_ns = self.timestamps[0]
            del self.data[:self.frame_size]
            del self.timestamps[:self.frame_size]
            if session == self.session:
                return seq, first_ns, now_ns
        return None


@dataclass
class Submission:
    started_ns: int
    completed_ns: int
    error: str = ''


def open_port(name, baud):
    line = serial.Serial(baudrate=baud, timeout=0.05, write_timeout=1)
    line.port = name
    line.rts = False
    line.dtr = False
    line.open()
    return line


def measure(tx, rx, count, hz, frame_size=FRAME_SIZE, jitter=0):
    session = secrets.randbits(64)
    reader = FrameReader(session, frame_size)
    stop = threading.Event()
    done = threading.Event()
    arrivals = {}
    submissions = {}
    duplicates = 0
    receive_error = ''

    def receive():
        nonlocal duplicates, receive_error
        try:
            while not stop.is_set():
                byte = rx.read(1)
                received_ns = time.perf_counter_ns()
                if not byte:
                    continue
                frame = reader.feed(byte, received_ns)
                if frame is None:
                    continue
                seq, first_ns, last_ns = frame
                if 1 <= seq <= count:
                    if seq in arrivals:
                        duplicates += 1
                    else:
                        arrivals[seq] = first_ns, last_ns
                    if len(arrivals) == count:
                        done.set()
        except (serial.SerialException, OSError) as exc:
            receive_error = str(exc)
            done.set()

    rx.reset_input_buffer()
    listener = threading.Thread(target=receive)
    listener.start()
    interrupted = False
    try:
        period_ns = round(1_000_000_000 / hz)
        due_ns = time.perf_counter_ns()
        last_progress_ns = due_ns
        for seq in range(1, count + 1):
            frame = pack_frame(session, seq, frame_size)
            while (wait_ns := due_ns - time.perf_counter_ns()) > 0:
                if done.wait(min(wait_ns / 1_000_000_000, 0.1)):
                    break
            if receive_error:
                break
            started_ns = time.perf_counter_ns()
            try:
                written = tx.write(frame)
                error = '' if written == len(frame) else f'仅写入 {written} 字节'
            except (serial.SerialException, OSError) as exc:
                error = str(exc)
            submissions[seq] = Submission(started_ns, time.perf_counter_ns(), error)
            # Avoid a burst of overdue frames when a write or the host stalls.
            due_ns = started_ns + round(period_ns * (1 + random.uniform(-jitter, jitter)))
            if seq == count or started_ns - last_progress_ns >= 5_000_000_000:
                print(f'已尝试发送 {seq}/{count}', flush=True)
                last_progress_ns = started_ns
        if not done.is_set():
            print(f'发包结束，最多再等 {RECEIVE_GRACE_SEC} 秒接收。', flush=True)
            done.wait(RECEIVE_GRACE_SEC)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        listener.join()
    return submissions, arrivals, duplicates, receive_error, interrupted


def percentile_95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def report(submissions, arrivals, duplicates, receive_error, interrupted, args, started_at):
    rows = []
    for seq, submission in submissions.items():
        first_ms = last_ms = span_ms = None
        if seq in arrivals:
            first_ns, last_ns = arrivals[seq]
            first_ms = (first_ns - submission.started_ns) / 1e6
            last_ms = (last_ns - submission.started_ns) / 1e6
            span_ms = (last_ns - first_ns) / 1e6
        write_ms = (submission.completed_ns - submission.started_ns) / 1e6
        rows.append((seq, submission.error, first_ms, last_ms, span_ms, write_ms, submission.started_ns))

    received = sum(row[3] is not None for row in rows)
    sent_ok = sum(not row[1] for row in rows)
    missing = [str(row[0]) for row in rows if not row[1] and row[3] is None]
    print(f'\n{args.tx} → {args.rx}，UART {args.baud} 8N1，{args.hz:g} Hz，{args.frame_bytes} 字节/帧')
    print(f'提交成功 {sent_ok}/{len(rows)}，收齐 {received}，未收到 {len(missing)}，重复 {duplicates}')
    if missing:
        print(f"未收到的序号 {'、'.join(missing[:20])}{'…' if len(missing) > 20 else ''}")
    if sent_ok < len(rows):
        print(f'最近一次发送错误 {next(row[1] for row in reversed(rows) if row[1])}')
    if receive_error:
        print(f'接收串口错误 {receive_error}')
    if interrupted:
        print('测试已中止，以下为已采集的数据。')
    for column, label in ((2, '写入 → 首字节'), (3, '写入 → 完整帧'), (4, '首字节 → 完整帧'), (5, '串口写调用')):
        values = [row[column] for row in rows if not row[1] and row[column] is not None]
        if values:
            print(f'{label}  中位数 {statistics.median(values):.2f} ms  '
                  f'P95 {percentile_95(values):.2f} ms  最小 {min(values):.2f} ms  最大 {max(values):.2f} ms')
    folder = Path.home() / 'Downloads' / 'YRT-LoRa-Bench-Logs'
    folder.mkdir(parents=True, exist_ok=True)
    filename = folder / f'{started_at:%Y%m%d-%H%M%S-%f}_{args.tx}-{args.rx}.csv'
    with filename.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('tx_port', 'rx_port', 'baud', 'hz', 'frame_bytes', 'seq', 'write_error',
                         'write_to_first_ms', 'write_to_frame_ms', 'first_to_frame_ms', 'write_call_ms', 'sent_monotonic_ns'))
        for row in rows:
            writer.writerow((args.tx, args.rx, args.baud, args.hz, args.frame_bytes, *row))
    print(f'逐帧记录已保存到 {filename}')
    return 0 if received and sent_ok == len(rows) and not receive_error and not interrupted else 1


def select_ports(args, ports):
    devices = {port.device.upper(): port for port in ports}
    candidates = [port.device for port in ports if port.vid == 0x1A86 and port.pid in (0x7523, 0x5523, 0x55D4)]
    defaults = candidates if len(candidates) == 2 else ('', '')
    if not args.tx:
        args.tx = input(f'发送端 COM 口{f" [{defaults[0]}]" if defaults[0] else ""}：').strip() or defaults[0]
    if not args.rx:
        args.rx = input(f'接收端 COM 口{f" [{defaults[1]}]" if defaults[1] else ""}：').strip() or defaults[1]
    args.tx, args.rx = args.tx.upper(), args.rx.upper()
    if args.tx == args.rx or args.tx not in devices or args.rx not in devices:
        raise ValueError('请选择列表中两个不同的 LoRa 串口。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='同一台 Windows 电脑的双 LoRa 串口延迟测试')
    parser.add_argument('--ports', action='store_true', help='仅列出串口')
    parser.add_argument('--tx', metavar='COMx', help='发送端串口')
    parser.add_argument('--rx', metavar='COMy', help='接收端串口')
    parser.add_argument('--count', type=int, default=50, help='帧数，默认 50')
    parser.add_argument('--hz', type=float, default=2, help='发包频率，默认 2 Hz')
    parser.add_argument('--baud', type=int, default=9600, help='两端 UART 波特率，默认 9600')
    parser.add_argument('--frame-bytes', type=int, default=FRAME_SIZE, help='诊断包长，20–96 字节')
    parser.add_argument('--jitter', type=float, default=0, help='诊断用发送间隔随机变化比例，0–0.5')
    parser.add_argument('--no-pause', action='store_true', help='结束后直接退出')
    args = parser.parse_args(argv)

    interactive = not (args.tx and args.rx) and not args.ports
    try:
        ports = sorted(list_ports.comports())
        print('Windows 串口列表')
        for port in ports:
            print(f'  {port.device}  {port.description}')
        if args.ports:
            if not ports:
                print('未发现串口。')
            return 0
        if len(ports) < 2:
            raise ValueError(f'目前找到 {len(ports)} 个串口，请把两只 LoRa 都接到电脑后重开程序。')
        select_ports(args, ports)
        if interactive:
            count = input(f'帧数 [{args.count}]：').strip()
            hz = input(f'发送频率 Hz [{args.hz:g}]：').strip()
            args.count = int(count) if count else args.count
            args.hz = float(hz) if hz else args.hz
        if not (1 <= args.count <= 0xFFFFFFFF and 0.01 <= args.hz <= 1000 and args.baud > 0):
            raise ValueError('帧数和波特率须为正整数，频率须为 0.01–1000 Hz。')
        if not (FRAME_SIZE <= args.frame_bytes <= 96 and 0 <= args.jitter <= 0.5):
            raise ValueError('测试帧须为 20–96 字节，发送间隔变化比例须为 0–0.5。')
        print(f'{args.tx} → {args.rx}，{args.count} 帧 × {args.frame_bytes} 字节，{args.hz:g} Hz，UART {args.baud}。')
        if interactive:
            input('确认以上两个串口都接 LoRa 后按回车开始。')
        started_at = datetime.now()
        with open_port(args.rx, args.baud) as rx, open_port(args.tx, args.baud) as tx:
            result = measure(tx, rx, args.count, args.hz, args.frame_bytes, args.jitter)
        return report(*result, args, started_at)
    except (ValueError, serial.SerialException, OSError, EOFError) as exc:
        print(f'测试异常：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1
    finally:
        if interactive and not args.no_pause and sys.stdin.isatty():
            try:
                input('\n按回车退出。')
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == '__main__':
    raise SystemExit(main())
