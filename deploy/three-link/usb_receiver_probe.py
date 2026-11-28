#!/usr/bin/env python3
"""Temporarily present a neutral Flydigi receiver to the WHEELTEC USB host."""

import argparse
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time


CONFIGFS = Path('/sys/kernel/config/usb_gadget')
CONTROLLER = Path('/sys/class/udc/fc000000.usb')
NEUTRAL = b'\x00\x14' + bytes(18)
HID_REPORT = bytes.fromhex('06 00 ff 09 01 a1 01 09 02 15 00 26 ff 00 '
                           '75 08 95 14 81 02 09 03 95 08 91 02 c0')
TIMEOUT_ERRORS = {errno.EAGAIN, errno.EIDRM, errno.ESHUTDOWN, errno.ENODEV}


# Each step takes one deliberate movement from neutral and caps its duration.
STAGES = (
    ('FORWARD', 1, 0), ('REVERSE', -1, 0),
    ('LEFT', 0, -1), ('RIGHT', 0, 1),
    ('FORWARD_RIGHT', 1, 1),
    ('INPUT_LOSS', 1, 0), ('POST_REARM_FORWARD', 1, 0),
)
REPORT_AGE = 0.2
PULSE_SECONDS = 0.7


class DrivePulse:
    """Run the bench checks in order using fresh controller input only."""

    def __init__(self):
        self.lock = threading.Lock()
        self.report = NEUTRAL
        self.valid_until = 0
        self.stage = 0
        self.pressed_at = None
        self.armed = False
        self.centered = False
        self.ready_at = 0
        self.pulse_at = None
        self.loss_requested = False
        self.verify_locked = False
        self.finished = False

    def get(self):
        with self.lock:
            now = time.monotonic()
            if now >= self.valid_until or (self.pulse_at is not None and now >= self.pulse_at + PULSE_SECONDS):
                return NEUTRAL
            return self.report

    def disable(self):
        with self.lock:
            self.finished = True
            self.report = NEUTRAL

    def expire(self, simulated=False):
        with self.lock:
            self.report = NEUTRAL
            self.armed = self.centered = False
            self.pressed_at = self.pulse_at = None
            if simulated:
                self.stage += 1
                self.verify_locked = True
                print('PASS: missing input forced neutral. Test forward once before rearming.', flush=True)

    def update(self, connected, axes, unlock, when):
        if not connected:
            self.expire()
            return False
        with self.lock:
            if self.finished:
                return False
            if when >= self.valid_until:
                self.armed = self.centered = False
                self.pressed_at = self.pulse_at = None
            self.valid_until = when + REPORT_AGE
            neutral = all(abs(axis) <= 4096 for axis in axes)
            report = bytearray(NEUTRAL)
            name, forward, turn = STAGES[self.stage]
            if self.verify_locked:
                if axes[1] >= 8192 and not unlock:
                    self.verify_locked = False
                    print('PASS: forward blocked before rearm. Center, then press and release Start.', flush=True)
            elif not self.armed:
                if neutral and not unlock:
                    self.centered = True
                    if self.pressed_at is not None and when - self.pressed_at >= 0.1:
                        self.armed = True
                        self.ready_at = when + 0.6
                        print(f'START accepted. Ready for {name}.', flush=True)
                    self.pressed_at = None
                elif neutral and unlock and self.centered:
                    if self.pressed_at is None:
                        self.pressed_at = when
                    report[2] = 0x10
                else:
                    self.pressed_at = None
                    self.centered = False
            else:
                match = (not unlock and self.axis_matches(axes[1], forward)
                         and self.axis_matches(axes[2], turn))
                if self.pulse_at is None:
                    if neutral and not unlock:
                        self.centered = True
                    elif self.centered and when >= self.ready_at and match:
                        self.pulse_at = when
                        self.centered = False
                        print(f'{name}: started.', flush=True)
                if self.pulse_at is not None:
                    elapsed = when - self.pulse_at
                    if match and elapsed < PULSE_SECONDS:
                        report[9] = (forward * 40) & 0xff
                        report[11] = (turn * 40) & 0xff
                        if name == 'INPUT_LOSS' and elapsed >= 0.28 and not self.loss_requested:
                            self.loss_requested = True
                            self.report = bytes(report)
                            return True
                    else:
                        self.pulse_at = None
                        self.centered = neutral and not unlock
                        self.ready_at = when + 0.6
                        print(f'{name}: neutral ({elapsed:.2f}s).', flush=True)
                        if name == 'INPUT_LOSS':
                            self.loss_requested = False
                            print('Ready for INPUT_LOSS. Hold the stick until the wheels stop.', flush=True)
                        else:
                            self.stage += 1
                            self.finished = self.stage == len(STAGES)
                            if self.finished:
                                print('All bench input steps completed.', flush=True)
                            else:
                                print(f'Ready for {STAGES[self.stage][0]}.', flush=True)
            self.report = bytes(report)
            return False

    @staticmethod
    def axis_matches(axis, sign):
        return axis * sign >= 8192 if sign else abs(axis) <= 4096


class FreeDrive:
    """Allow continuous bench input after a fresh Start press at neutral."""

    def __init__(self, raw_input=False):
        self.lock = threading.Lock()
        self.raw_input = raw_input
        self.report = NEUTRAL
        self.valid_until = 0
        self.armed = False
        self.centered = False
        self.pressed_at = None
        self.ready_at = 0
        self.disabled = False

    def get(self):
        with self.lock:
            return self.report if time.monotonic() < self.valid_until else NEUTRAL

    def expire(self, simulated=False):
        with self.lock:
            self.report = NEUTRAL
            self.valid_until = 0
            self.armed = self.centered = False
            self.pressed_at = None

    def disable(self):
        self.expire()
        with self.lock:
            self.disabled = True

    def update(self, connected, values, unlock, when, deadline=None):
        if not connected:
            self.expire()
            return False
        with self.lock:
            if self.disabled:
                return False
            if when >= self.valid_until:
                self.armed = self.centered = False
                self.pressed_at = None
            self.valid_until = when + REPORT_AGE if deadline is None else deadline
            axes, triggers = values[:4], values[4:]
            neutral = all(abs(axis) <= 4096 for axis in axes) and max(triggers) <= 8
            report = bytearray(NEUTRAL)
            if not self.armed:
                if neutral and not unlock:
                    self.centered = True
                    if self.pressed_at is not None and when - self.pressed_at >= 0.1:
                        self.armed = True
                        self.ready_at = when + 0.6
                        print('START accepted. Free bench control armed.', flush=True)
                    self.pressed_at = None
                elif neutral and unlock and self.centered:
                    if self.pressed_at is None:
                        self.pressed_at = when
                    report[2] = 0x10
                else:
                    self.pressed_at = None
                    self.centered = False
            elif when >= self.ready_at and not unlock:
                if self.raw_input:
                    report[4:6] = bytes(triggers)
                    struct.pack_into('<4h', report, 6, *axes)
                else:
                    for offset, axis in zip((7, 9, 11, 13), axes):
                        if abs(axis) > 4096:
                            report[offset] = max(-40, min(40, int(axis / 256))) & 0xff
                    if abs(axes[1]) <= 4096 and sum(value > 8 for value in triggers) == 1:
                        for offset, value in zip((4, 5), triggers):
                            report[offset] = min(80, value) if value > 8 else 0
            self.report = bytes(report)
            return False


def request_input(drive, stop):
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    simulate_loss = False
    for seq in range(1, sys.maxsize):
        if stop.is_set():
            return
        sent = time.monotonic()
        print(f'POLL {seq}', flush=True)
        if not poll.poll(180):
            drive.expire(simulated=simulate_loss)
            if simulate_loss:
                simulate_loss = False
                continue
            print('Input timeout. All controls neutral.', flush=True)
            return
        line = sys.stdin.readline()
        if not line:
            drive.update(False, (), False, time.monotonic())
            return
        received = time.monotonic()
        try:
            index, connected, axes, unlock = json.loads(line)
            expected = 6 if isinstance(drive, FreeDrive) else 4
            if (index != seq or type(connected) is not bool or type(unlock) is not bool
                    or len(axes) != expected
                    or any(type(axis) is not int or not -32768 <= axis <= 32767 for axis in axes[:4])
                    or any(type(value) is not int or not 0 <= value <= 255 for value in axes[4:])):
                raise ValueError('Unexpected input report')
        except (ValueError, TypeError):
            drive.update(False, (), False, received)
            print('Invalid input. All controls neutral.', flush=True)
            return
        if seq == 1:
            print(f'First controller sample received. connected={connected}', flush=True)
        simulate_loss = drive.update(connected and received - sent < 0.18, axes, unlock, received)
        if simulate_loss:
            print('DROP_NEXT_SAMPLE', flush=True)
        stop.wait(0.02)


def descriptor_block(hid):
    # This FunctionFS kernel cannot accept the receiver's vendor descriptor 0x21.
    # The chassis also supports standard HID class, but decodes the same
    # Xbox reports for this VID/PID. Present a valid HID descriptor for its probe.
    if hid:
        interface = bytes.fromhex('09 04 00 00 02 03 00 00 00')
        interface += struct.pack('<BBHBBBH', 9, 0x21, 0x0111, 0, 1, 0x22, len(HID_REPORT))
    else:
        interface = bytes.fromhex('09 04 00 00 02 ff 5d 01 00')
    full_speed = interface + bytes.fromhex('07 05 81 03 40 00 02 07 05 02 03 40 00 08')
    high_speed = interface + bytes.fromhex('07 05 81 03 40 00 05 07 05 02 03 40 00 07')
    count = 4 if hid else 3
    return (struct.pack('<5I', 3, 20 + len(full_speed + high_speed), 3, count, count)
            + full_speed + high_speed)


def write(path, value):
    path.write_text(value + '\n', encoding='ascii')


def subdir(stack, path):
    path.mkdir()
    stack.callback(path.rmdir)
    return path


def send_reports(ep_in, enabled, stop, get_report, log_reports):
    sent = 0
    previous = NEUTRAL
    while not stop.is_set():
        if not enabled.wait(0.1) or stop.is_set():
            continue
        try:
            report = get_report()
            os.write(ep_in, report)
        except OSError as error:
            if stop.is_set():
                return
            if error.errno not in TIMEOUT_ERRORS:
                raise
        else:
            if log_reports and report != previous:
                print(f'USB report delivered: Start={bool(report[2] & 0x10)} '
                      f'LY={struct.unpack_from("<b", report, 9)[0]} '
                      f'RX={struct.unpack_from("<b", report, 11)[0]} '
                      f'LT={report[4]} RT={report[5]}', flush=True)
                previous = report
            sent += 1
            if log_reports and (sent == 1 or sent % 200 == 0):
                print(f'Interrupt reports sent: {sent}', flush=True)
        time.sleep(0.02)


def serve(ep0, ep_in, stop, drive, input_handler, log_reports):
    poll = select.poll()
    poll.register(ep0, select.POLLIN)
    state = None
    enabled = threading.Event()
    input_started = False
    get_report = drive.get if drive else lambda: NEUTRAL
    sender = threading.Thread(target=send_reports, args=(ep_in, enabled, stop, get_report, log_reports),
                              daemon=True)
    sender.start()
    while True:
        if not sender.is_alive():
            raise OSError('Neutral report sender stopped.')
        current = (CONTROLLER / 'state').read_text(encoding='ascii').strip()
        if current != state:
            print(f'USB state: {current}', flush=True)
            state = current
        if poll.poll(20):
            request = os.read(ep0, 12)
            if len(request) != 12:
                raise OSError('Incomplete FunctionFS event')
            event = request[8]
            if event == 2:
                enabled.set()
                print('Host configured the receiver.', flush=True)
                if drive and hasattr(drive, 'usb_connected'):
                    drive.usb_connected()
                if drive and input_handler and not input_started:
                    input_started = True
                    threading.Thread(target=input_handler, args=(drive, stop), daemon=True).start()
            elif event in (1, 3, 5):
                enabled.clear()
                if drive:
                    drive.disable()
            elif event == 6:
                enabled.set()
            elif event == 4:
                request_type, code, value, _, length = struct.unpack('<BBHHH', request[:8])
                print(f'USB control request type=0x{request_type:02x} '
                      f'code=0x{code:02x} value=0x{value:04x} length={length}', flush=True)
                try:
                    if request_type == 0xa1 and code == 1:
                        os.write(ep0, NEUTRAL[:length])
                    elif request_type == 0x81 and code == 6 and value >> 8 == 0x22:
                        os.write(ep0, HID_REPORT[:length])
                    elif request_type == 0x21 and code in (10, 11):
                        os.read(ep0, length)
                    elif request_type & 0x80:
                        os.read(ep0, 0)  # Wrong direction stalls an unsupported request.
                    else:
                        os.write(ep0, b'')
                except OSError as error:
                    if error.errno not in TIMEOUT_ERRORS | {errno.EL2HLT}:
                        raise


def end_probe(*_):
    raise KeyboardInterrupt


def run(seconds, hid, drive, input_handler=request_input, log_reports=True):
    if not CONTROLLER.is_dir():
        raise OSError('The RK3588 USB controller is not in peripheral mode.')
    owner = [path for path in CONFIGFS.glob('*/UDC')
             if path.read_text(encoding='ascii').strip() == CONTROLLER.name]
    stock = CONFIGFS / 'rockchip' / 'UDC'
    if owner and owner != [stock]:
        raise OSError('A different gadget already owns the RK3588 USB controller.')

    with ExitStack() as stack:
        if owner:
            write(stock, '')
            stack.callback(write, stock, CONTROLLER.name)
        instance = f'yrt_probe_{os.getpid()}'
        device = subdir(stack, CONFIGFS / instance)
        for name, value in {
            'idVendor': '0x045e', 'idProduct': '0x028e', 'bcdUSB': '0x0200',
            'bcdDevice': '0x0110', 'bDeviceClass': '0xff',
            'bDeviceSubClass': '0xff', 'bDeviceProtocol': '0xff',
            'bMaxPacketSize0': '64', 'max_speed': 'high-speed',
        }.items():
            write(device / name, value)
        strings = subdir(stack, device / 'strings' / '0x409')
        write(strings / 'manufacturer', 'Flydigi')
        write(strings / 'product', 'Flydigi Dune Fox')
        write(strings / 'serialnumber', 'YRT-Neutral-Probe')
        config = subdir(stack, device / 'configs' / 'c.1')
        write(config / 'bmAttributes', '0x80')
        write(config / 'MaxPower', '500')
        function = subdir(stack, device / 'functions' / f'ffs.{instance}')
        mount = stack.enter_context(tempfile.TemporaryDirectory(prefix='yrt-usb-'))
        subprocess.run(['mount', '-t', 'functionfs', instance, mount], check=True)
        stack.callback(subprocess.run, ['umount', '-l', mount], check=True)
        ep0 = os.open(Path(mount) / 'ep0', os.O_RDWR)
        stack.callback(os.close, ep0)
        os.write(ep0, descriptor_block(hid))
        os.write(ep0, struct.pack('<4I', 2, 16, 0, 0))
        ep_in = os.open(Path(mount) / 'ep1', os.O_WRONLY | os.O_NONBLOCK)
        stack.callback(os.close, ep_in)
        link = config / function.name
        link.symlink_to(function)
        stack.callback(link.unlink)
        write(device / 'UDC', CONTROLLER.name)
        stack.callback(write, device / 'UDC', '')
        print('Neutral receiver is ready.', flush=True)
        signal.signal(signal.SIGALRM, end_probe)
        signal.signal(signal.SIGTERM, end_probe)
        signal.alarm(seconds)
        stop = threading.Event()
        try:
            serve(ep0, ep_in, stop, drive, input_handler, log_reports)
        except KeyboardInterrupt:
            print('Neutral probe ended.', flush=True)
        finally:
            signal.alarm(0)
            stop.set()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, choices=range(1, 301), metavar='1-300', default=60)
    parser.add_argument('--vendor-interface', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--drive-pulse', action='store_true', help='Run the ordered control bench checks over stdin.')
    mode.add_argument('--free-drive', action='store_true', help='Accept continuous low-speed input over stdin.')
    args = parser.parse_args()
    drive = FreeDrive() if args.free_drive else DrivePulse() if args.drive_pulse else None
    run(args.seconds, hid=not args.vendor_interface, drive=drive)
