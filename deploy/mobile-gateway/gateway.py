#!/usr/bin/env python3
"""Authenticated HTTPS reverse-poll gateway for YRT mobile control."""

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from h264_gateway import PREFIX as H264_PREFIX, handle_h264


HOST = os.environ.get('YRT_GATEWAY_BIND', '127.0.0.1')
PORT = int(os.environ.get('YRT_GATEWAY_PORT', '8765'))
DB = Path(os.environ.get('YRT_GATEWAY_DB', '/var/lib/yrt-mobile-gateway/state.sqlite3'))
MAX_BODY = 32 * 1024 * 1024
RUN_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
LOCK = threading.RLock()
VIDEO_LOCK = threading.Lock()
VIDEO_VIEWERS = {}
VIDEO_FRAME = None
VIDEO_MAX_FRAME = 256 * 1024
VIDEO_LEASE = 4


def video_needed(now):
    for viewer, deadline in list(VIDEO_VIEWERS.items()):
        if deadline <= now:
            del VIDEO_VIEWERS[viewer]
    return bool(VIDEO_VIEWERS)


def token(role):
    value = os.environ.get('YRT_TOKEN_' + role.upper(), '')
    return value


@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=10)) as db, db:
        configure_database(db)
        yield db


def configure_database(db):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS state (role TEXT PRIMARY KEY, value TEXT NOT NULL, seen REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS commands (
          id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL,
          value TEXT NOT NULL, created REAL NOT NULL, acked INTEGER NOT NULL DEFAULT 0,
          lease_until REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS command_pending ON commands(target, acked, id);
        CREATE TABLE IF NOT EXISTS live (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL, value TEXT NOT NULL,
          seen REAL NOT NULL, event_key TEXT
        );
        CREATE INDEX IF NOT EXISTS live_run ON live(run, id);
        CREATE TABLE IF NOT EXISTS vehicle_data (
          run TEXT NOT NULL, ordinal INTEGER NOT NULL, value TEXT NOT NULL,
          PRIMARY KEY(run, ordinal)
        );
        CREATE TABLE IF NOT EXISTS archive_revisions (
          run TEXT PRIMARY KEY, revision INTEGER NOT NULL, source TEXT NOT NULL
        );
    ''')
    command_columns = {row['name'] for row in db.execute('PRAGMA table_info(commands)')}
    if 'lease_until' not in command_columns:
        db.execute('ALTER TABLE commands ADD COLUMN lease_until REAL NOT NULL DEFAULT 0')
    live_columns = {row['name'] for row in db.execute('PRAGMA table_info(live)')}
    if 'event_key' not in live_columns:
        db.execute('ALTER TABLE live ADD COLUMN event_key TEXT')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS live_event ON live(run,event_key)')


def archive_source(source, first_row):
    if source is not None:
        return source
    # The desktop added this field before archive sources were explicitly recorded.
    return 'pc' if 'send_time_status' in first_row else 'vehicle'


def archive_info(db, run):
    return db.execute(
        'SELECT v.value, a.revision, a.source FROM vehicle_data AS v '
        'LEFT JOIN archive_revisions AS a ON a.run=v.run WHERE v.run=? AND v.ordinal=0', (run,)).fetchone()


def store_run_rows(db, run, rows, source):
    if source == 'vehicle':
        current = archive_info(db, run)
        if current is not None and archive_source(current['source'], json.loads(current['value'])) == 'pc':
            return db.execute('SELECT COUNT(*) FROM vehicle_data WHERE run=?', (run,)).fetchone()[0]
    db.execute('DELETE FROM vehicle_data WHERE run=?', (run,))
    db.executemany('INSERT INTO vehicle_data(run,ordinal,value) VALUES(?,?,?)',
                   ((run, i, json.dumps(row, ensure_ascii=False)) for i, row in enumerate(rows)))
    db.execute('INSERT INTO archive_revisions(run,revision,source) VALUES(?,1,?) '
               'ON CONFLICT(run) DO UPDATE SET revision=revision+1,source=excluded.source', (run, source))
    return len(rows)


def authorized(handler, role):
    expected = token(role)
    supplied = handler.headers.get('Authorization', '')
    return bool(expected) and secrets.compare_digest(supplied, 'Bearer ' + expected)


class Handler(BaseHTTPRequestHandler):
    server_version = 'YRTGateway/1.0'

    def log_message(self, fmt, *args):
        print('%s %s' % (self.log_date_time_string(), fmt % args), flush=True)

    def log_request(self, code='-', size='-'):
        if not self.path.startswith('/api/v1/video/') or str(code) not in ('200', '204'):
            super().log_request(code, size)

    def send_video(self, code, frame=None):
        self.send_response(code)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(frame[2]) if frame else 0))
        if frame:
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('X-YRT-Frame', frame[0])
        self.end_headers()
        if frame:
            self.wfile.write(frame[2])

    def read_video(self, route):
        global VIDEO_FRAME
        if route.path == '/api/v1/video/demand':
            if not authorized(self, 'vehicle'):
                return self.send_video(401)
            with VIDEO_LOCK:
                needed = video_needed(time.monotonic())
            return self.send_video(200 if needed else 204)
        if route.path != '/api/v1/video/frame':
            return self.send_video(404)
        if not authorized(self, 'pc'):
            return self.send_video(401)
        params = parse_qs(route.query)
        viewer = params.get('viewer', [''])[0]
        after = params.get('after', [''])[0]
        if not RUN_RE.fullmatch(viewer):
            return self.send_video(400)
        with VIDEO_LOCK:
            now = time.monotonic()
            if not video_needed(now):
                VIDEO_FRAME = None
            if viewer not in VIDEO_VIEWERS and len(VIDEO_VIEWERS) >= 8:
                frame, code = None, 429
            else:
                VIDEO_VIEWERS[viewer] = now + VIDEO_LEASE
                frame = VIDEO_FRAME
                if frame is None or now - frame[1] > 2 or frame[0] == after:
                    frame = None
                code = 200 if frame else 204
        return self.send_video(code, frame)

    def write_video(self, route):
        global VIDEO_FRAME
        if route.path == '/api/v1/video/close':
            if not authorized(self, 'pc'):
                return self.send_video(401)
            viewer = parse_qs(route.query).get('viewer', [''])[0]
            with VIDEO_LOCK:
                VIDEO_VIEWERS.pop(viewer, None)
                if not video_needed(time.monotonic()):
                    VIDEO_FRAME = None
            return self.send_video(204)
        if route.path != '/api/v1/video/frame':
            return self.send_video(404)
        if not authorized(self, 'vehicle'):
            return self.send_video(401)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.send_video(400)
        if not 4 <= length <= VIDEO_MAX_FRAME or self.headers.get_content_type() != 'image/jpeg':
            return self.send_video(400)
        self.connection.settimeout(5)
        jpeg = self.rfile.read(length)
        if len(jpeg) != length or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
            return self.send_video(400)
        with VIDEO_LOCK:
            now = time.monotonic()
            needed = video_needed(now)
            if needed:
                VIDEO_FRAME = secrets.token_hex(8), now, jpeg
        return self.send_video(204 if needed else 409)

    def send_json(self, code, value):
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ValueError('invalid content length')
        if length < 1 or length > MAX_BODY:
            raise ValueError('request body is empty or too large')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('JSON object required')
        return value

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Max-Age', '600')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path)
        if route.path.startswith(H264_PREFIX):
            return handle_h264(self, route, authorized, 'GET')
        if route.path.startswith('/api/v1/video/'):
            return self.read_video(route)
        if route.path == '/health':
            return self.send_json(200, {'ok': True})
        if route.path == '/api/v1/state':
            if not authorized(self, 'phone'):
                return self.send_json(401, {'error': 'unauthorized'})
            return self.state()
        if route.path == '/api/v1/poll':
            role = parse_qs(route.query).get('role', [''])[0]
            if role not in ('pc', 'vehicle') or not authorized(self, role):
                return self.send_json(401, {'error': 'unauthorized'})
            return self.poll(role)
        if route.path == '/api/v1/runs':
            if not authorized(self, 'phone'):
                return self.send_json(401, {'error': 'unauthorized'})
            with LOCK, connect() as db:
                runs = []
                for row in db.execute(
                    'SELECT v.run, COUNT(*) AS records, MAX(a.source) AS source, '
                    'MAX(CASE WHEN v.ordinal=0 THEN v.value END) AS first_row '
                    'FROM vehicle_data AS v LEFT JOIN archive_revisions AS a ON a.run=v.run '
                    'GROUP BY v.run ORDER BY v.run DESC'
                ):
                    first_row = json.loads(row['first_row']) if row['first_row'] is not None else {}
                    if archive_source(row['source'], first_row) != 'pc':
                        continue
                    runs.append({'run': row['run'], 'records': row['records'],
                                 'started_utc_ms': first_row.get('run_started_utc_ms')})
            return self.send_json(200, {'runs': runs})
        if route.path == '/api/v1/data':
            desktop = authorized(self, 'pc')
            if not (desktop or authorized(self, 'phone')):
                return self.send_json(401, {'error': 'unauthorized'})
            run = parse_qs(route.query).get('run', [''])[0]
            if not RUN_RE.fullmatch(run):
                return self.send_json(400, {'error': 'invalid run id'})
            with LOCK, connect() as db:
                archive = archive_info(db, run)
                revision = (archive['revision'] or 0) if archive is not None else 0
                pending = (archive is not None and not desktop and
                           archive_source(archive['source'], json.loads(archive['value'])) != 'pc')
                rows = [] if pending else [json.loads(r['value']) for r in db.execute(
                    'SELECT value FROM vehicle_data WHERE run=? ORDER BY ordinal', (run,))]
            return self.send_json(200, {'run': run, 'rows': rows, 'complete': bool(rows),
                                        'revision': revision, 'pending_desktop': pending})
        return self.send_json(404, {'error': 'not found'})

    def do_POST(self):
        route = urlparse(self.path)
        if route.path.startswith(H264_PREFIX):
            return handle_h264(self, route, authorized, 'POST')
        if route.path.startswith('/api/v1/video/'):
            return self.write_video(route)
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            return self.send_json(400, {'error': str(error)})
        if route.path == '/api/v1/command':
            if not authorized(self, 'phone'):
                return self.send_json(401, {'error': 'unauthorized'})
            return self.enqueue_phone_command(data)
        if route.path == '/api/v1/report':
            role = data.get('role')
            if role not in ('pc', 'vehicle') or not authorized(self, role):
                return self.send_json(401, {'error': 'unauthorized'})
            with LOCK, connect() as db:
                db.execute('INSERT INTO state(role,value,seen) VALUES(?,?,?) '
                           'ON CONFLICT(role) DO UPDATE SET value=excluded.value,seen=excluded.seen',
                           (role, json.dumps(data.get('state', {}), ensure_ascii=False), time.time()))
            return self.send_json(200, {'ok': True})
        if route.path == '/api/v1/ack':
            role, command_id = data.get('role'), data.get('id')
            if role not in ('pc', 'vehicle') or not authorized(self, role) or not isinstance(command_id, int):
                return self.send_json(401, {'error': 'unauthorized'})
            with LOCK, connect() as db:
                db.execute('UPDATE commands SET acked=1,lease_until=0 WHERE id=? AND target=?', (command_id, role))
            return self.send_json(200, {'ok': True})
        if route.path == '/api/v1/device-command':
            if not authorized(self, 'pc'):
                return self.send_json(401, {'error': 'unauthorized'})
            kind, run = data.get('command'), data.get('run')
            if kind != 'upload_log' or not isinstance(run, str) or not RUN_RE.fullmatch(run):
                return self.send_json(400, {'error': 'upload_log and a valid run id are required'})
            with LOCK, connect() as db:
                stored = db.execute('SELECT 1 FROM vehicle_data WHERE run=? LIMIT 1', (run,)).fetchone()
                if stored is not None:
                    return self.send_json(200, {'ok': True, 'complete': True})
                row = db.execute('SELECT seen FROM state WHERE role=?', ('vehicle',)).fetchone()
                if row is None or time.time() - row['seen'] >= 20:
                    return self.send_json(409, {'error': 'vehicle gateway agent is offline'})
                value = json.dumps({'command': kind, 'run': run})
                existing = db.execute('SELECT id FROM commands WHERE target=? AND value=? AND acked=0',
                                      ('vehicle', value)).fetchone()
                if existing is not None:
                    command_id = existing['id']
                else:
                    cursor = db.execute('INSERT INTO commands(target,value,created) VALUES(?,?,?)',
                                        ('vehicle', value, time.time()))
                    command_id = cursor.lastrowid
            return self.send_json(202, {'ok': True, 'id': command_id})
        if route.path == '/api/v1/live':
            if not authorized(self, 'pc'):
                return self.send_json(401, {'error': 'unauthorized'})
            run, rows = data.get('run'), data.get('rows')
            if not isinstance(run, str) or not RUN_RE.fullmatch(run) or not isinstance(rows, list):
                return self.send_json(400, {'error': 'run and rows required'})
            with LOCK, connect() as db:
                for row in rows[-500:]:
                    if isinstance(row, dict) and row.get('run') == run:
                        event_key = None
                        if all(key in row for key in ('mono_ns', 'seq', 'link', 'outcome')):
                            event_key = ':'.join(str(row[key]) for key in ('mono_ns', 'seq', 'link', 'outcome'))
                        db.execute('INSERT OR IGNORE INTO live(run,value,seen,event_key) VALUES(?,?,?,?)',
                                   (run, json.dumps(row, ensure_ascii=False), time.time(), event_key))
                db.execute('DELETE FROM live WHERE id NOT IN (SELECT id FROM live ORDER BY id DESC LIMIT 2000)')
            return self.send_json(200, {'ok': True})
        if route.path == '/api/v1/vehicle-data':
            if not authorized(self, 'vehicle'):
                return self.send_json(401, {'error': 'unauthorized'})
            run, rows = data.get('run'), data.get('rows')
            if not isinstance(run, str) or not RUN_RE.fullmatch(run) or not isinstance(rows, list):
                return self.send_json(400, {'error': 'run and rows required'})
            if len(rows) > 200000 or any(not isinstance(row, dict) or row.get('run') != run for row in rows):
                return self.send_json(400, {'error': 'invalid log rows'})
            with LOCK, connect() as db:
                saved = store_run_rows(db, run, rows, 'vehicle')
            return self.send_json(200, {'ok': True, 'rows': saved})
        if route.path == '/api/v1/desktop-data':
            if not authorized(self, 'pc'):
                return self.send_json(401, {'error': 'unauthorized'})
            run, rows = data.get('run'), data.get('rows')
            if not isinstance(run, str) or not RUN_RE.fullmatch(run) or not isinstance(rows, list):
                return self.send_json(400, {'error': 'run and rows required'})
            if len(rows) > 200000 or any(not isinstance(row, dict) or row.get('run') != run for row in rows):
                return self.send_json(400, {'error': 'invalid log rows'})
            with LOCK, connect() as db:
                saved = store_run_rows(db, run, rows, 'pc')
            return self.send_json(200, {'ok': True, 'rows': saved})
        return self.send_json(404, {'error': 'not found'})

    def state(self):
        now = time.time()
        with LOCK, connect() as db:
            clients = {}
            for row in db.execute('SELECT role,value,seen FROM state'):
                clients[row['role']] = {'online': now - row['seen'] < 10,
                                        'last_seen': row['seen'], 'state': json.loads(row['value'])}
            live = [json.loads(row['value']) for row in db.execute(
                'SELECT value FROM live ORDER BY id DESC LIMIT 100')]
            revisions = {}
            pending_runs = []
            for row in db.execute(
                'SELECT v.run, v.value, a.source, a.revision FROM vehicle_data AS v '
                'LEFT JOIN archive_revisions AS a ON a.run=v.run WHERE v.ordinal=0 '
                "ORDER BY (substr(v.run,9,1)='-') DESC, v.run DESC LIMIT 20"
            ):
                if archive_source(row['source'], json.loads(row['value'])) == 'pc':
                    revisions[row['run']] = row['revision'] or 0
                else:
                    pending_runs.append(row['run'])
        pc = clients.get('pc', {'online': False, 'state': {}})
        vehicle = clients.get('vehicle', {'online': False, 'state': {}})
        return self.send_json(200, {'pc': pc, 'vehicle': vehicle, 'live': list(reversed(live)),
                                    'runs': list(revisions), 'archive_revisions': revisions,
                                    'pending_runs': pending_runs})

    def poll(self, role):
        now = time.time()
        with LOCK, connect() as db:
            rows = list(db.execute('SELECT id,value FROM commands WHERE target=? AND acked=0 '
                                   'AND created>? AND lease_until<=? ORDER BY id LIMIT 100',
                                   (role, now - 86400, now)))
            if rows:
                lease = now + (60 if role == 'vehicle' else 30)
                db.executemany('UPDATE commands SET lease_until=? WHERE id=? AND acked=0',
                               ((lease, row['id']) for row in rows))
        return self.send_json(200, {'commands': [{'id': row['id'], **json.loads(row['value'])} for row in rows]})

    def enqueue_phone_command(self, data):
        kind = data.get('command')
        if kind == 'start':
            try:
                count = int(data['count'])
                hz = float(data['hz'])
            except (KeyError, TypeError, ValueError, OverflowError):
                return self.send_json(400, {'error': 'invalid count or frequency'})
            links = data.get('links')
            if not 1 <= count <= 5000 or not 0.1 <= hz <= 10 or not isinstance(links, list) or not links:
                return self.send_json(400, {'error': 'invalid experiment parameters'})
            if any(link not in ('lora', 'wifi', '4g') for link in links):
                return self.send_json(400, {'error': 'invalid link'})
            mode = data.get('vehicle_mode', 'bridge')
            if mode not in ('bridge', 'lte'):
                return self.send_json(400, {'error': 'invalid vehicle data mode'})
            target = 'pc'
            payload = {'command': kind, 'count': count, 'hz': hz, 'links': links, 'vehicle_mode': mode}
        elif kind == 'stop':
            target, payload = 'pc', {'command': kind}
        elif kind == 'dial':
            target, payload = 'pc', {'command': kind}
        elif kind == 'fetch_vehicle':
            run = data.get('run')
            if not isinstance(run, str) or not RUN_RE.fullmatch(run):
                return self.send_json(400, {'error': 'invalid run id'})
            target = 'pc'
            payload = {'command': kind, 'run': run}
        else:
            return self.send_json(400, {'error': 'unsupported command'})
        with LOCK, connect() as db:
            row = db.execute('SELECT seen FROM state WHERE role=?', ('pc',)).fetchone()
            if row is None or time.time() - row['seen'] >= 10:
                return self.send_json(409, {'error': 'desktop station is offline'})
            cursor = db.execute('INSERT INTO commands(target,value,created) VALUES(?,?,?)',
                                (target, json.dumps(payload), time.time()))
            command_id = cursor.lastrowid
        return self.send_json(202, {'ok': True, 'id': command_id})


def main():
    missing = [role for role in ('phone', 'pc', 'vehicle') if len(token(role)) < 32]
    if missing:
        raise SystemExit('Set 32+ character YRT_TOKEN_' + ', YRT_TOKEN_'.join(r.upper() for r in missing))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f'YRT gateway listening on {HOST}:{PORT}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
