#!/usr/bin/env python3
"""Install only the video routes into the live YRT gateway."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


SERVICE = 'yrt-mobile-gateway.service'
DESTINATION = Path('/opt/yrt-mobile-gateway/gateway.py')
LOCAL = 'http://127.0.0.1:8765'


def add_routes(original, release):
    if 'VIDEO_MAX_FRAME = ' in original:
        if 'return self.read_video(route)' not in original or 'return self.write_video(route)' not in original:
            raise ValueError('The installed video patch is incomplete')
        return original
    edits = (
        ('\ndef token(role):', release[release.index('VIDEO_LOCK = '):release.index('\ndef token(role):')] + '\n'),
        ('    def send_json(self, code, value):', release[release.index('    def log_request('):
                                                      release.index('    def send_json(')]),
    )
    for marker, addition in edits:
        if original.count(marker) != 1:
            raise ValueError(f'Unexpected installed gateway source near {marker.strip()}')
        original = original.replace(marker, addition + marker, 1)
    for method, callback in (('GET', 'read_video'), ('POST', 'write_video')):
        marker = f'    def do_{method}(self):\n        route = urlparse(self.path)\n'
        if original.count(marker) != 1:
            raise ValueError(f'Unexpected {method} handler')
        original = original.replace(marker, marker + "        if route.path.startswith('/api/v1/video/'):\n"
                                    f'            return self.{callback}(route)\n', 1)
    return original


def replace_source(source, mode):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DESTINATION.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(source)
            out.flush()
            os.chmod(temp, mode)
            os.replace(temp, DESTINATION)
        finally:
            temp.unlink(missing_ok=True)


def route_ready():
    with urlopen(LOCAL + '/health', timeout=2) as response:
        if response.status != 200:
            return False
    try:
        with urlopen(LOCAL + '/api/v1/video/demand', timeout=2):
            return False
    except HTTPError as error:
        return error.code == 401


def main():
    original = DESTINATION.read_text(encoding='utf-8')
    release = Path(__file__).with_name('gateway.py').read_text(encoding='utf-8')
    updated = add_routes(original, release)
    compile(updated, str(DESTINATION), 'exec')
    if updated == original and route_ready():
        print('YRT video routes already active')
        return
    mode = stat.S_IMODE(DESTINATION.stat().st_mode)
    backup = Path('/var/backups') / f'yrt-video-gateway-{datetime.now():%Y%m%d-%H%M%S-%f}.py'
    shutil.copy2(DESTINATION, backup)
    replace_source(updated, mode)
    try:
        subprocess.run(['systemctl', 'restart', SERVICE], check=True, timeout=20)
        for _ in range(10):
            try:
                if route_ready():
                    print(f'YRT video routes active. Gateway backup: {backup}')
                    return
            except (URLError, OSError):
                pass
            time.sleep(.5)
        raise RuntimeError('Gateway health or video authorization check failed')
    except (subprocess.SubprocessError, OSError, RuntimeError):
        replace_source(original, mode)
        subprocess.run(['systemctl', 'restart', SERVICE], check=True, timeout=20)
        raise


if __name__ == '__main__':
    main()
