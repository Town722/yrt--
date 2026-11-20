#!/bin/sh
set -eu

cd "$(dirname "$0")"
/usr/bin/python3 -c 'import serial'
install -d -m 0755 /opt/yrt-fusion /etc/yrt-fusion
install -m 0644 fusion.py /opt/yrt-fusion/fusion.py
if [ ! -e /etc/yrt-fusion/receive.json ]; then
    install -m 0644 receive.json /etc/yrt-fusion/receive.json
fi
install -m 0644 yrt-fusion-rx.service /etc/systemd/system/yrt-fusion-rx.service
systemctl daemon-reload
systemctl enable yrt-fusion-rx.service
systemctl restart yrt-fusion-rx.service
