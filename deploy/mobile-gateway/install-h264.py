#!/usr/bin/env python3
"""Add the continuous video routes to the existing gateway and HTTPS site."""

from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


SERVICE = 'yrt-mobile-gateway.service'
ROOT = Path('/opt/yrt-mobile-gateway')
SITE = Path('/etc/nginx/sites-enabled/yuyunchang')
SNIPPET = Path('/etc/nginx/snippets/yrt-mobile.conf')
BACKUPS = Path('/var/backups')
PREFIX = '/yrt-mobile/api/v1/video/h264/'
UPLOAD = f'''location = {PREFIX}push {{
    proxy_pass http://127.0.0.1:8765/api/v1/video/h264/push;
    proxy_http_version 1.1;
    proxy_request_buffering off;
    proxy_buffering off;
    client_max_body_size 0;
    client_body_timeout 8s;
    proxy_send_timeout 8s;
    proxy_read_timeout 30m;
}}

'''


def edited(s, marker, content):
    if s.count(marker) != 1:
        raise ValueError(f'Unexpected existing configuration near {marker.strip()}')
    return s.replace(marker, content, 1)


def replace(path, contents, mode):
    out = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(out.name)
    try:
        with out:
            out.write(contents)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def command(*args):
    subprocess.run(args, check=True, timeout=20)


def service_ready():
    for _ in range(14):
        try:
            with urlopen('http://127.0.0.1:8765/health', timeout=2) as result:
                if result.status != 200:
                    continue
            with urlopen('http://127.0.0.1:8765/api/v1/video/h264/watch', timeout=2):
                pass
        except HTTPError as error:
            if error.code == 401:
                return
        except (URLError, OSError):
            pass
        time.sleep(.5)
    raise RuntimeError('H.264 video route failed its health check')


def main():
    server = ROOT / 'gateway.py'
    site = SITE.resolve(strict=True)
    release = Path(__file__).resolve().parent
    source = server.read_text(encoding='utf-8')
    if '    def send_video(' not in source:
        raise ValueError('Install the existing gateway video routes with install-video.py before this upgrade.')
    if 'from h264_gateway import' not in source:
        marker = '\ndef token(role):'
        source = edited(source, marker, '\nfrom h264_gateway import PREFIX as H264_PREFIX, handle_h264\n' + marker)
        for method in ('GET', 'POST'):
            marker = f'    def do_{method}(self):\n        route = urlparse(self.path)\n'
            source = edited(source, marker, marker + '        if route.path.startswith(H264_PREFIX):\n'
                            f"            return handle_h264(self, route, authorized, '{method}')\n")
    compile(source, str(server), 'exec')
    nginx = site.read_text(encoding='utf-8')
    nginx_path = site
    if re.search(r'^\s*include\s+' + re.escape(str(SNIPPET)) + r'\s*;', nginx, re.MULTILINE):
        nginx_path = SNIPPET.resolve(strict=True)
        nginx = nginx_path.read_text(encoding='utf-8')
    if f'location = {PREFIX}push' not in nginx:
        start = 'location ^~ /yrt-mobile/api/ {'
        nginx = edited(nginx, start, UPLOAD + start)
    backup = BACKUPS / f'yrt-h264-{datetime.now():%Y%m%d-%H%M%S-%f}'
    backup.mkdir(mode=0o700)
    paths = {server: source.encode(), nginx_path: nginx.encode()}
    for name in ('h264_gateway.py', 'video_transport.py'):
        paths[ROOT / name] = (release / name).read_bytes()
        compile(paths[ROOT / name], name, 'exec')
    originals = {}
    for path in paths:
        if path.exists():
            save = backup / path.name
            shutil.copy2(path, save)
            originals[path] = save
    try:
        for path, updated in paths.items():
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            replace(path, updated, mode)
        command('nginx', '-t')
        command('systemctl', 'restart', SERVICE)
        service_ready()
        command('systemctl', 'reload', 'nginx')
    except (OSError, subprocess.SubprocessError, RuntimeError):
        for path in paths:
            if path in originals:
                save = originals[path]
                replace(path, save.read_bytes(), stat.S_IMODE(save.stat().st_mode))
            else:
                path.unlink(missing_ok=True)
        command('systemctl', 'restart', SERVICE)
        command('systemctl', 'reload', 'nginx')
        raise
    print(f'YRT H.264 video active. Backup: {backup}')


if __name__ == '__main__':
    main()
