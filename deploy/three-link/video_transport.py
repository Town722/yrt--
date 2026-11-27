"""Bounded MJPEG framing and the YRT HTTPS image transport."""

import http.client
import struct
from urllib.parse import urlsplit


MAX_FRAME = 256 * 1024
START, END = b'\xff\xd8', b'\xff\xd9'
FRAME_HEADER = struct.Struct('!IQB')
KEEPALIVE = FRAME_HEADER.pack(0, 0, 0)


def video_packet(payload, captured_ns, keyframe):
    if not payload or len(payload) > MAX_FRAME:
        raise ValueError('invalid video frame size')
    return FRAME_HEADER.pack(len(payload), captured_ns, int(keyframe)) + payload


def video_frames(stream):
    while header := stream.read(FRAME_HEADER.size):
        if len(header) != FRAME_HEADER.size:
            raise OSError('incomplete video header')
        length, captured_ns, keyframe = FRAME_HEADER.unpack(header)
        if length > MAX_FRAME or keyframe not in (0, 1):
            raise OSError('invalid video header')
        payload = stream.read(length) if length else b''
        if len(payload) != length:
            raise OSError('incomplete video frame')
        yield payload, captured_ns, bool(keyframe)


def jpeg_frames(stream):
    pending = bytearray()
    read = getattr(stream, 'read1', stream.read)
    while chunk := read(16 * 1024):
        pending.extend(chunk)
        while pending:
            start = pending.find(START)
            if start < 0:
                del pending[:-1]
                break
            del pending[:start]
            end = pending.find(END, len(START))
            if end < 0:
                if len(pending) > MAX_FRAME:
                    del pending[:len(START)]
                    continue
                break
            end += len(END)
            if end <= MAX_FRAME:
                yield bytes(pending[:end])
            del pending[:end]


class VideoHTTPS:
    def __init__(self, base, token, connection=http.client.HTTPSConnection):
        url = urlsplit(base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('请配置有效的 HTTPS 网关地址')
        if len(token) < 32:
            raise ValueError('请配置网关令牌')
        self.path = url.path.rstrip('/') + '/api/v1/video/'
        self.conn = connection(url.hostname, url.port or 443, timeout=4)
        self.authorization = 'Bearer ' + token

    def request(self, method, route, body=None, content_type=None):
        headers = {'Authorization': self.authorization}
        if content_type:
            headers['Content-Type'] = content_type
        self.conn.request(method, self.path + route, body=body, headers=headers)
        with self.conn.getresponse() as response:
            payload = response.read(MAX_FRAME + 1)
            if len(payload) > MAX_FRAME:
                self.conn.close()
                raise OSError('收到的图传数据超过大小限制')
            if response.status == 404:
                raise OSError('云网关尚未安装图传接口（HTTP 404）')
            if response.status == 401:
                raise OSError('图传网关令牌校验失败（HTTP 401）')
            if response.status not in (200, 204, 409):
                raise OSError(f'图传网关返回 HTTP {response.status}')
            return response.status, response.getheader('X-YRT-Frame'), payload

    def close(self):
        self.conn.close()
