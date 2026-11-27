#!/usr/bin/env python3
"""Install the mobile app into one existing HTTPS nginx server block."""

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


SNIPPET = Path('/etc/nginx/snippets/yrt-mobile.conf')
INCLUDE = f'    include {SNIPPET};'


def install(site, host):
    here = Path(__file__).resolve().parent
    site = site.resolve(strict=True)
    original = site.read_text(encoding='utf-8')
    blocks = re.split(r'(?m)(?=^[ \t]*server[ \t]*\{[ \t]*(?:#.*)?$)', original)
    matching = []
    for index, block in enumerate(blocks):
        declared = re.search(r'(?m)^[ \t]*server_name[ \t]+([^;]+);', block)
        listeners = re.findall(r'(?m)^[ \t]*listen[ \t]+([^;]+);', block)
        if declared and host in declared[1].split() and any(
            re.search(r'(?:^|:)443(?:\s|$)', listener) for listener in listeners
        ):
            matching.append((index, declared.end()))
    if len(matching) != 1:
        raise SystemExit('Expected exactly one existing HTTPS server block for ' + host)
    index, offset = matching[0]
    if str(SNIPPET) not in blocks[index]:
        blocks[index] = blocks[index][:offset] + '\n' + INCLUDE + blocks[index][offset:]
    new_site = ''.join(blocks)

    subprocess.run(['nginx', '-t'], check=True)
    backup = Path('/var/backups') / ('yrt-nginx-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(mode=0o700)
    shutil.copy2(site, backup / 'site.conf')
    if SNIPPET.exists():
        shutil.copy2(SNIPPET, backup / 'snippet.conf')
    changed = False
    try:
        changed = True
        shutil.copyfile(here / 'nginx-location.conf', SNIPPET)
        site.write_text(new_site, encoding='utf-8')
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['sh', str(here / 'install-server.sh')], check=True)
        subprocess.run(['systemctl', 'is-active', 'yrt-mobile-gateway.service'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    except (OSError, subprocess.CalledProcessError):
        if changed:
            shutil.copyfile(backup / 'site.conf', site)
            if (backup / 'snippet.conf').exists():
                shutil.copyfile(backup / 'snippet.conf', SNIPPET)
            else:
                SNIPPET.unlink(missing_ok=True)
        raise
    print(f'Mobile page: https://{host}/yrt-mobile/')
    print(f'Nginx backup: {backup}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    install(args.site, args.host)
