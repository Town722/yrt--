"""Authenticated continuous H.264 relay with a bounded live buffer."""

from collections import deque
import threading
import time

from video_transport import KEEPALIVE, video_frames, video_packet


PREFIX = '/api/v1/video/h264/'
STREAM_TYPE = 'application/octet-stream'
CONDITION = threading.Condition()
PUBLISHER = threading.Lock()
FRAMES = deque(maxlen=16)
SEQUENCE = 0
VIEWERS = 0


class ChunkedBody:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 0
        self.finished = False

    def read(self, size):
        result = bytearray()
        while len(result) < size and not self.finished:
            if not self.remaining:
                line = self.stream.readline(64)
                if not line.endswith(b'\r\n'):
                    raise OSError('invalid video chunk header')
                self.remaining = int(line.split(b';', 1)[0], 16)
                if not 0 <= self.remaining <= 1024 * 1024:
                    raise OSError('invalid video chunk size')
                if not self.remaining:
                    self.finished = True
                    break
            wanted = min(size - len(result), self.remaining)
            part = self.stream.read(wanted)
            if len(part) != wanted:
                raise OSError('incomplete video upload')
            result.extend(part)
            self.remaining -= wanted
            if not self.remaining and self.stream.read(2) != b'\r\n':
                raise OSError('invalid video chunk boundary')
        return bytes(result)


def ingest(handler):
    global SEQUENCE
    if (handler.headers.get('Transfer-Encoding', '').lower() != 'chunked' or
            handler.headers.get_content_type() != STREAM_TYPE):
        return handler.send_video(400)
    if not PUBLISHER.acquire(blocking=False):
        return handler.send_video(409)
    try:
        handler.connection.settimeout(3)
        try:
            for payload, stamp, key in video_frames(ChunkedBody(handler.rfile)):
                if not payload:
                    continue
                packet = video_packet(payload, stamp, key)
                with CONDITION:
                    if not VIEWERS:
                        break
                    SEQUENCE += 1
                    FRAMES.append((SEQUENCE, time.monotonic(), key, packet))
                    CONDITION.notify_all()
        except (OSError, ValueError):
            pass
    finally:
        PUBLISHER.release()
    return handler.send_video(204)


def watch(handler):
    global VIEWERS
    with CONDITION:
        if VIEWERS >= 4:
            return handler.send_video(429)
        VIEWERS += 1
        cursor = SEQUENCE
    try:
        handler.connection.settimeout(1)
        handler.send_response(200)
        handler.send_header('Content-Type', STREAM_TYPE)
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        need_key = True
        while True:
            with CONDITION:
                CONDITION.wait_for(lambda: SEQUENCE > cursor, timeout=.65)
                if SEQUENCE - cursor > 6:
                    need_key = True
                now = time.monotonic()
                eligible = [f for f in FRAMES if f[0] > cursor and now - f[1] <= .28]
                if need_key:
                    candidates = [f for f in eligible if f[2]]
                    frame = candidates[-1] if candidates else None
                else:
                    frame = eligible[0] if eligible and eligible[0][0] == cursor + 1 else None
                if frame:
                    cursor, _, _, packet = frame
                    need_key = False
                else:
                    cursor, packet = SEQUENCE, KEEPALIVE
                    need_key = True
            handler.wfile.write(packet)
            handler.wfile.flush()
    except OSError:
        pass
    finally:
        with CONDITION:
            VIEWERS -= 1
            if not VIEWERS:
                FRAMES.clear()


def handle_h264(handler, route, authorized, method):
    name = route.path[len(PREFIX):]
    if name == 'watch' and method == 'GET':
        return watch(handler) if authorized(handler, 'pc') else handler.send_video(401)
    if name == 'demand' and method == 'GET':
        if not authorized(handler, 'vehicle'):
            return handler.send_video(401)
        with CONDITION:
            active = bool(VIEWERS)
        return handler.send_video(200 if active else 204)
    if name == 'push' and method == 'POST':
        return ingest(handler) if authorized(handler, 'vehicle') else handler.send_video(401)
    return handler.send_video(404)
