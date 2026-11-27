"""Small HTTPS poll client used by the Windows station to serve phone control."""

import json
import queue
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GatewayLink:
    def __init__(self, config):
        self.base = str(config.get('gateway_url', '')).rstrip('/')
        self.token = str(config.get('gateway_token', ''))
        self.enabled = self.base.startswith('https://') and len(self.token) >= 32
        self.incoming = queue.Queue()
        self.outgoing = queue.Queue()
        self.pending_ids = set()
        self.downloads = set()
        self.downloads_lock = threading.Lock()
        self.lock = threading.Lock()
        self.state = {}
        self.last_error = ''
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='mobile-gateway', daemon=True)
        self.thread.start()

    def set_state(self, state):
        with self.lock:
            self.state = dict(state)

    def post_live(self, run, rows):
        if rows:
            self.outgoing.put(('live', {'run': run, 'rows': rows}))

    def upload_station_log(self, run, rows):
        if rows:
            self.outgoing.put(('desktop_data', {'run': run, 'rows': rows}))

    def upload_vehicle_log(self, run):
        self.outgoing.put(('upload', run))

    def acknowledge(self, command_id):
        self.outgoing.put(('ack', command_id))

    def close(self):
        self.stopped.set()

    def _request(self, method, path, payload=None, timeout=5):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        request = Request(self.base + path, data=body, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json'})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(32 * 1024 * 1024)
        return json.loads(raw) if raw else {}

    def _run(self):
        if not self.enabled:
            self.last_error = '手机控制未配置：需要 HTTPS 网关地址及 PC 令牌'
            return
        next_report = 0
        while not self.stopped.is_set():
            try:
                now = time.monotonic()
                if now >= next_report:
                    with self.lock:
                        snapshot = dict(self.state)
                    self._request('POST', '/api/v1/report', {'role': 'pc', 'state': snapshot})
                    next_report = now + 2
                for _ in range(20):
                    try:
                        kind, value = self.outgoing.get_nowait()
                    except queue.Empty:
                        break
                    if kind == 'live':
                        try:
                            self._request('POST', '/api/v1/live', value)
                        except (OSError, ValueError, HTTPError, URLError):
                            self.outgoing.put((kind, value))
                            raise
                    elif kind == 'desktop_data':
                        try:
                            self._request('POST', '/api/v1/desktop-data', value, timeout=180)
                        except (OSError, ValueError, HTTPError, URLError):
                            self.outgoing.put((kind, value))
                            raise
                    elif kind == 'upload':
                        try:
                            self._request('POST', '/api/v1/device-command', {'command': 'upload_log', 'run': value})
                        except (OSError, ValueError, HTTPError, URLError):
                            self.outgoing.put((kind, value))
                            raise
                        self.incoming.put({'command': 'vehicle_upload_requested', 'run': value})
                        with self.downloads_lock:
                            if value not in self.downloads:
                                self.downloads.add(value)
                                threading.Thread(target=self._download_when_ready, args=(value,), daemon=True).start()
                    elif kind == 'ack':
                        try:
                            self._request('POST', '/api/v1/ack', {'role': 'pc', 'id': value})
                        except (OSError, ValueError, HTTPError, URLError):
                            self.outgoing.put((kind, value))
                            raise
                        self.pending_ids.discard(value)
                commands = self._request('GET', '/api/v1/poll?role=pc').get('commands', [])
                for command in commands:
                    command_id = command.get('id')
                    if not isinstance(command_id, int) or command_id not in self.pending_ids:
                        self.incoming.put(command)
                        if isinstance(command_id, int):
                            self.pending_ids.add(command_id)
                self.last_error = ''
                self.stopped.wait(0.8)
            except (OSError, ValueError, HTTPError, URLError) as error:
                self.last_error = str(error)[:300]
                self.stopped.wait(3)

    def _download_when_ready(self, run):
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline and not self.stopped.is_set():
                try:
                    result = self._request('GET', '/api/v1/data?' + urlencode({'run': run}), timeout=180)
                    if result.get('complete'):
                        self.incoming.put({'command': 'vehicle_data', 'run': run, 'rows': result['rows']})
                        return
                except (OSError, ValueError, HTTPError, URLError):
                    pass
                self.stopped.wait(3)
            self.incoming.put({'command': 'vehicle_data_timeout', 'run': run})
        finally:
            with self.downloads_lock:
                self.downloads.discard(run)
