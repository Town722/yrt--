#!/usr/bin/env python3
"""Read one Windows XInput controller and observe its state on the RK3588 via SSH."""

import argparse
import base64
import ctypes
import errno
import getpass
import json
import re
import subprocess
import sys
import time


UNLOCK = 0x0010


class Gamepad(ctypes.Structure):
    _fields_ = [
        ('buttons', ctypes.c_uint16),
        ('lt', ctypes.c_uint8),
        ('rt', ctypes.c_uint8),
        ('lx', ctypes.c_int16),
        ('ly', ctypes.c_int16),
        ('rx', ctypes.c_int16),
        ('ry', ctypes.c_int16),
    ]


class State(ctypes.Structure):
    _fields_ = [('packet', ctypes.c_uint32), ('pad', Gamepad)]


def open_xinput():
    get_state = ctypes.WinDLL('xinput1_4.dll').XInputGetState
    get_state.argtypes = [ctypes.c_uint32, ctypes.POINTER(State)]
    get_state.restype = ctypes.c_uint32
    return get_state


def read_gamepad(get_state, state, slot, include_triggers=False):
    connected = get_state(slot, ctypes.byref(state)) == 0
    pad = state.pad
    axes = [pad.lx, pad.ly, pad.rx, pad.ry] if connected else [0, 0, 0, 0]
    if include_triggers:
        axes.extend((pad.lt, pad.rt) if connected else (0, 0))
    return connected, axes, connected and bool(pad.buttons & UNLOCK)


def sample(get_state, state, slot, seq, include_triggers=False):
    return json.dumps([seq, *read_gamepad(get_state, state, slot, include_triggers)]) + '\n'


# This SSH process only reports what arrived. It has no USB or motor output.
RECEIVE = """
import json
import socket
import sys
import time

print('Receiver ready on ' + socket.gethostname(), flush=True)
count = 0
max_axes = [0, 0, 0, 0]
unlock_presses = 0
previous = None
last_report = time.monotonic()
for line in sys.stdin:
    seq, connected, axes, unlock = json.loads(line)
    if seq != count + 1:
        raise ValueError('Unexpected sequence number')
    count += 1
    if connected:
        max_axes = [max(before, abs(axis)) for before, axis in zip(max_axes, axes)]
    if unlock and (previous is None or not previous[1]):
        unlock_presses += 1
    now = time.monotonic()
    status = (connected, unlock)
    if status != previous or now - last_report >= 5:
        print(f'seq={seq} connected={connected} axes={axes} unlock={unlock} '
              f'axis_peak={max_axes} unlock_presses={unlock_presses}', flush=True)
        last_report = now
    previous = status
print(f'Received {count} frames. Axis peaks LX/LY/RX/RY={max_axes}. '
      f'Unlock presses={unlock_presses}.', flush=True)
"""


def probe(host, slot, seconds):
    get_state = open_xinput()
    state = State()
    source = base64.b64encode(RECEIVE.encode('ascii')).decode('ascii')
    remote = f"""python3 -u -c "import base64;exec(base64.b64decode('{source}'))" """
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host, remote]
    with subprocess.Popen(command, stdin=subprocess.PIPE, text=True, encoding='ascii') as conn:
        end = time.monotonic() + seconds
        seq = 0
        try:
            while time.monotonic() < end:
                seq += 1
                conn.stdin.write(sample(get_state, state, slot, seq))
                conn.stdin.flush()
                time.sleep(0.02)
        except OSError as error:
            # Windows OpenSSH can report EINVAL instead of EPIPE after a reset.
            if error.errno not in (errno.EINVAL, errno.EPIPE):
                raise
        finally:
            try:
                conn.stdin.close()
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.EPIPE):
                    raise
        try:
            return conn.wait(timeout=10)
        except subprocess.TimeoutExpired:
            conn.kill()
            return conn.wait()


def drive_pulse(host, slot, seconds, script, password, free):
    if not re.fullmatch(r'/tmp/yrt-usb-receiver-[A-Za-z0-9]+\.py', script):
        raise ValueError('Expected the temporary USB receiver probe path.')
    get_state = open_xinput()
    state = State()
    mode = '--free-drive' if free else '--drive-pulse'
    remote = f"sudo -k -S -p '' python3 {script} --seconds {seconds} {mode}"
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2', host, remote]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True, encoding='utf-8') as conn:
        conn.stdin.reconfigure(newline='\n')
        try:
            conn.stdin.write(password + '\n')
            conn.stdin.flush()
            drop_next = False
            for line in conn.stdout:
                if line.startswith('POLL '):
                    if drop_next:
                        drop_next = False
                    else:
                        conn.stdin.write(sample(get_state, state, slot, int(line.split()[1]), include_triggers=free))
                        conn.stdin.flush()
                elif line.strip() == 'DROP_NEXT_SAMPLE':
                    drop_next = True
                    print('Simulating missing input on the next sample.', flush=True)
                else:
                    print(line.rstrip(), flush=True)
        except (BrokenPipeError, KeyboardInterrupt):
            conn.terminate()
        return conn.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host', help='SSH target such as ztl@192.168.137.246')
    parser.add_argument('--slot', type=int, choices=range(4), default=0)
    parser.add_argument('--seconds', type=int, choices=range(1, 301), metavar='1-300', default=60)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--drive-pulse', metavar='REMOTE_SCRIPT')
    mode.add_argument('--free-drive', metavar='REMOTE_SCRIPT')
    parser.add_argument('--sudo-stdin', action='store_true')
    args = parser.parse_args()
    if args.drive_pulse or args.free_drive:
        password = sys.stdin.readline().rstrip('\r\n') if args.sudo_stdin else getpass.getpass('RK3588 sudo: ')
        raise SystemExit(drive_pulse(args.host, args.slot, args.seconds, args.drive_pulse or args.free_drive,
                                     password, free=bool(args.free_drive)))
    raise SystemExit(probe(args.host, args.slot, args.seconds))
