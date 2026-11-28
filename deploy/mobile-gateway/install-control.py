#!/usr/bin/env python3
"""Install remote control on the deployed gateway, preserving its local changes."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
TARGET = Path('/opt/yrt-mobile-gateway')
UNIT = 'yrt-mobile-gateway.service'
FILES = ('gateway.py', 'control_gateway.py', 'control_protocol.py')
IMPORT = 'from control_gateway import PREFIX as CONTROL_PREFIX, handle_control'
POST = '    def do_POST(self):\n        route = urlparse(self.path)\n'
ROUTE = '        if route.path.startswith(CONTROL_PREFIX):\n            return handle_control(self, route, authorized)\n'


def publish(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        stream.write(data)
    try:
        tmp.chmod(0o644)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def check():
    request = Request('http://127.0.0.1:8765/api/v1/remote-control/pc',
                      method='POST', data=b'{}', headers={'Content-Type': 'application/json'})
    for _ in range(15):
        try:
            with urlopen(request, timeout=1):
                pass
        except HTTPError as error:
            if error.code == 401:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError('The relay did not return the expected authentication response.')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root.')
    old = (TARGET / 'gateway.py').read_text(encoding='utf-8')
    updated = old
    if IMPORT not in updated:
        anchor = 'from urllib.parse import parse_qs, urlparse\n'
        if updated.count(anchor) != 1:
            raise SystemExit('Cannot locate the existing gateway imports.')
        updated = updated.replace(anchor, anchor + '\n' + IMPORT + '\n', 1)
    if ROUTE not in updated:
        if updated.count(POST) != 1:
            raise SystemExit('Cannot locate the existing gateway POST dispatcher.')
        updated = updated.replace(POST, POST + ROUTE, 1)
    replacements = {
        'gateway.py': updated.encode('utf-8'),
        'control_gateway.py': (HERE / 'control_gateway.py').read_bytes(),
        'control_protocol.py': (HERE / '../three-link/control_protocol.py').read_bytes(),
    }
    for name, data in replacements.items():
        compile(data, name, 'exec')
    backup = Path(tempfile.mkdtemp(prefix='yrt-remote-control-', dir='/var/backups'))
    for name in FILES:
        if (TARGET / name).exists():
            shutil.copy2(TARGET / name, backup / name)
    try:
        for name, data in replacements.items():
            publish(TARGET / name, data)
        subprocess.run(['systemctl', 'restart', UNIT], check=True)
        check()
    except BaseException:
        for name in FILES:
            if (backup / name).exists():
                shutil.copy2(backup / name, TARGET / name)
            else:
                (TARGET / name).unlink(missing_ok=True)
        subprocess.run(['systemctl', 'restart', UNIT], check=False)
        raise
    print(f'4G remote-control API active. Backup: {backup}')


if __name__ == '__main__':
    main()
