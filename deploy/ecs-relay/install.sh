#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
test -x /usr/bin/python3
install -d -m 0755 /opt/yrt-relay
install -m 0644 mavlink_relay.py video_relay.py /opt/yrt-relay/
install -m 0644 yrt-relay@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable yrt-relay@video.service yrt-relay@mavlink.service
systemctl restart yrt-relay@video.service yrt-relay@mavlink.service
systemctl is-active yrt-relay@video.service yrt-relay@mavlink.service
