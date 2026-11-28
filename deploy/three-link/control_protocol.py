"""Small authenticated HTTP control messages, separate from video."""

import http.client
import json
import re
from urllib.parse import urlsplit


PREFIX = '/api/v1/remote-control/'
LIMIT = 512
LTE_MAX_AGE = 0.45
BRIDGE_MAX_AGE = 0.20
IDENTIFIER = re.compile(r'[0-9a-f]{32}')


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def valid_sample(sample):
    return (isinstance(sample, list) and len(sample) == 6
            and all(type(value) is int and -32768 <= value <= 32767 for value in sample[:4])
            and all(type(value) is int and 0 <= value <= 255 for value in sample[4:]))


def valid_frame(frame):
    return (isinstance(frame, dict) and identifier(frame.get('session'))
            and type(frame.get('seq')) is int and 0 < frame['seq'] <= 2**31
            and type(frame.get('connected')) is bool and type(frame.get('unlock')) is bool
            and valid_sample(frame.get('input')))


class ControlHTTPS:
    def __init__(self, base, token, connection=http.client.HTTPSConnection):
        url = urlsplit(base)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or len(token) < 32):
            raise ValueError('请先配置 HTTPS 网关地址和令牌')
        self.path = url.path.rstrip('/') + PREFIX
        self.connection = connection(url.hostname, url.port or 443, timeout=1)
        self.headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}

    def exchange(self, role, body):
        self.connection.request('POST', self.path + role, json.dumps(body), self.headers)
        with self.connection.getresponse() as response:
            payload = response.read(LIMIT + 1)
            if response.status != 200:
                raise OSError(f'遥控网关返回 HTTP {response.status}')
            if len(payload) > LIMIT:
                raise OSError('遥控网关响应过长')
            return json.loads(payload)

    def close(self):
        self.connection.close()
