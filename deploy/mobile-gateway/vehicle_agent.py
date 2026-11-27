#!/usr/bin/env python3
"""RK3588 outbound agent: health reports and post-run log upload over LTE."""

import json
import http.client
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


CONFIG = Path(os.environ.get('YRT_VEHICLE_CONFIG', '/etc/yrt-mobile-gateway/vehicle.json'))
MAX_UPLOAD = 30 * 1024 * 1024
LTE_INTERFACE = ''


class BoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection pinned to the receiver's configured LTE interface."""

    def connect(self):
        if not LTE_INTERFACE:
            return super().connect()
        addresses = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not addresses:
            raise OSError('Gateway hostname has no IPv4 address')
        destination = addresses[0][4][0]
        result = subprocess.run(['ip', '-j', '-4', 'route', 'get', destination, 'oif', LTE_INTERFACE],
                                capture_output=True, text=True, timeout=4)
        if result.returncode:
            raise OSError('No LTE route to gateway: ' + result.stderr.strip())
        route = json.loads(result.stdout)[0]
        if route.get('dev') != LTE_INTERFACE or not route.get('prefsrc'):
            raise OSError('No source address on LTE interface ' + LTE_INTERFACE)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, LTE_INTERFACE.encode() + b'\0')
            sock.bind((route['prefsrc'], 0))
            sock.connect((destination, self.port))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class BoundHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(BoundHTTPSConnection, request, context=self._context)


class Agent:
    def __init__(self):
        self.config = json.loads(CONFIG.read_text(encoding='utf-8'))
        self.base = self.config['url'].rstrip('/')
        self.token = self.config['token']
        self.log = Path(self.config.get('log', '/var/lib/yrt-fusion/rx.jsonl'))
        global LTE_INTERFACE
        LTE_INTERFACE = self.config.get('interface', '')
        if not LTE_INTERFACE:
            receiver_config = Path('/etc/yrt-fusion/receive.json')
            if receiver_config.is_file():
                LTE_INTERFACE = json.loads(receiver_config.read_text(encoding='utf-8')).get('lte_interface', '')
        self.opener = build_opener(ProxyHandler({}), BoundHTTPSHandler())
        if not self.base.startswith('https://') or len(self.token) < 32:
            raise ValueError('gateway URL must use HTTPS and token must be 32+ characters')
        self.last_upload = None
        self.last_error = None
        self.uploading = {}
        self.upload_lock = threading.Lock()

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        request = Request(self.base + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=8) as response:
            payload = response.read(2 * 1024 * 1024)
        return json.loads(payload) if payload else {}

    def report(self):
        with self.upload_lock:
            uploading_run = next(iter(self.uploading.values()), None)
        state = {'service': 'yrt-mobile-vehicle', 'online': True,
                 'logs_available': self.log.is_file(), 'last_upload': self.last_upload,
                 'last_error': self.last_error,
                 'uploading_run': uploading_run}
        self.request('POST', '/api/v1/report', {'role': 'vehicle', 'state': state})

    def upload(self, run):
        # Give the serial and LTE listener threads time to flush the end marker.
        time.sleep(2)
        rows = []
        if self.log.is_file():
            with self.log.open(encoding='utf-8') as source:
                for line in source:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get('run') == run:
                        rows.append(row)
        if not rows:
            raise RuntimeError('no received rows for ' + run)
        body = json.dumps({'run': run, 'rows': rows}, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > MAX_UPLOAD:
            raise RuntimeError('selected run exceeds 30 MiB upload limit')
        request = Request(self.base + '/api/v1/vehicle-data', data=body, method='POST',
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=180) as response:
            result = json.loads(response.read(1024 * 1024))
        self.last_upload = {'run': run, 'rows': result.get('rows', 0), 'utc': int(time.time())}

    def upload_command(self, command):
        command_id = command.get('id')
        try:
            run = command['run']
            self.upload(run)
            self.request('POST', '/api/v1/ack', {'role': 'vehicle', 'id': command_id})
            self.last_error = None
        except (KeyError, OSError, RuntimeError, ValueError, HTTPError, URLError) as error:
            self.last_error = str(error)[:300]
            print('upload failed:', self.last_error, flush=True)
        finally:
            if isinstance(command_id, int):
                with self.upload_lock:
                    self.uploading.pop(command_id, None)

    def run(self):
        next_report = 0
        while True:
            try:
                now = time.monotonic()
                if now >= next_report:
                    self.report()
                    next_report = now + 5
                commands = self.request('GET', '/api/v1/poll?role=vehicle').get('commands', [])
                for command in commands:
                    if command.get('command') != 'upload_log':
                        continue
                    command_id = command.get('id')
                    if not isinstance(command_id, int):
                        continue
                    with self.upload_lock:
                        if command_id in self.uploading:
                            continue
                        self.uploading[command_id] = command.get('run', '')
                    threading.Thread(target=self.upload_command, args=(command,), daemon=True).start()
                time.sleep(1)
            except (OSError, ValueError, HTTPError, URLError) as error:
                self.last_error = str(error)[:300]
                print('gateway unavailable:', self.last_error, flush=True)
                time.sleep(5)


if __name__ == '__main__':
    Agent().run()
