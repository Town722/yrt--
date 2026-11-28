#!/usr/bin/env python3
"""Receive desktop control over the Wi-Fi bridge or the LTE gateway."""

import argparse
from collections import deque
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sys
import threading
import time

from control_protocol import BRIDGE_MAX_AGE, LIMIT, LTE_MAX_AGE, ControlHTTPS, valid_frame
from usb_receiver_probe import FreeDrive, NEUTRAL, run


SOCKET = Path('/run/yrt-remote-control/operator.sock')


class Control:
    def __init__(self):
        self.lock = threading.Lock()
        self.drive = FreeDrive(raw_input=True)
        self.usb_ready = False
        self.owner = None
        self.deadline = 0
        self.sequence = 0

    def usb_connected(self):
        with self.lock:
            self.usb_ready = True

    def disable(self):
        with self.lock:
            self.usb_ready = False
            self.drive.expire()
            self.owner = None

    def drop_bridge(self, session):
        with self.lock:
            if self.owner == ('bridge', session):
                self.drive.expire()
                self.owner = None

    def accept(self, route, frame, deadline):
        now = time.monotonic()
        if deadline <= now or not valid_frame(frame):
            return
        identity = route, frame['session']
        with self.lock:
            if not self.usb_ready:
                return
            if identity != self.owner:
                if not frame['connected'] or (self.owner and now < self.deadline):
                    return
                self.drive.expire()
                self.owner, self.sequence = identity, 0
            if frame['seq'] <= self.sequence:
                return
            self.sequence = frame['seq']
            self.deadline = deadline
            self.drive.update(frame['connected'], frame['input'], frame['unlock'], now, deadline=deadline)

    def get(self):
        with self.lock:
            return self.drive.get() if self.usb_ready else NEUTRAL

    def state(self, route):
        with self.lock:
            if not self.usb_ready:
                return 'usb_offline'
            fresh = time.monotonic() < self.deadline
            if fresh and self.owner and self.owner[0] != route:
                return 'busy'
            return 'armed' if fresh and self.drive.armed else 'locked'


class Operator(socketserver.StreamRequestHandler):
    def handle(self):
        control = self.server.control
        session = None
        self.request.settimeout(BRIDGE_MAX_AGE)
        try:
            for seq in range(1, 2**31):
                deadline = time.monotonic() + BRIDGE_MAX_AGE
                prompt = {'seq': seq, 'state': control.state('bridge')}
                self.wfile.write(json.dumps(prompt).encode('ascii') + b'\n')
                line = self.rfile.readline(LIMIT + 1)
                if not line or len(line) > LIMIT:
                    return
                frame = json.loads(line)
                if not valid_frame(frame) or frame['seq'] != seq:
                    return
                if session is not None and session != frame['session']:
                    return
                session = frame['session']
                control.accept('bridge', frame, deadline)
                time.sleep(0.025)
        except (OSError, ValueError):
            return
        finally:
            if session is not None:
                control.drop_bridge(session)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def bridge():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(SOCKET))

        def send():
            try:
                for line in sys.stdin.buffer:
                    conn.sendall(line)
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=send, daemon=True).start()
        for line in conn.makefile('rb'):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()


def lte(control, stop, recent, challenge_lock, delay):
    sys.path.insert(0, '/opt/yrt-mobile-gateway')
    import vehicle_agent

    if stop.wait(delay):
        return
    while not stop.is_set():
        link = None
        try:
            agent = vehicle_agent.Agent()
            if not vehicle_agent.LTE_INTERFACE:
                raise OSError('车端 4G 网口未设置')
            link = ControlHTTPS(agent.base, agent.token, vehicle_agent.BoundHTTPSConnection)
            while not stop.is_set():
                nonce, sent = secrets.token_hex(16), time.monotonic()
                with challenge_lock:
                    recent.append((nonce, sent))
                reply = link.exchange('vehicle', {'nonce': nonce, 'state': control.state('lte')})
                if isinstance(reply, dict) and reply.get('nonce'):
                    with challenge_lock:
                        when = next((when for candidate, when in recent if candidate == reply['nonce']), None)
                    if when is not None:
                        control.accept('lte', reply.get('frame'), when + LTE_MAX_AGE)
                stop.wait(0.015 if isinstance(reply, dict) and reply.get('active') else 0.3)
        except (OSError, ValueError, http.client.HTTPException) as error:
            print(f'4G control: {error}', flush=True)
            stop.wait(2)
        finally:
            if link:
                link.close()


def serve():
    control = Control()
    stop = threading.Event()
    SOCKET.unlink(missing_ok=True)
    with Server(str(SOCKET), Operator) as server:
        server.control = control
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        recent = deque(maxlen=32)
        challenge_lock = threading.Lock()
        for delay in (0, 0.07, 0.14):
            threading.Thread(target=lte, args=(control, stop, recent, challenge_lock, delay), daemon=True).start()
        try:
            run(0, hid=True, drive=control, input_handler=None, log_reports=False)
        finally:
            stop.set()
            server.shutdown()
            SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('serve', 'bridge'))
    args = parser.parse_args()
    if args.mode == 'bridge':
        bridge()
    else:
        serve()
