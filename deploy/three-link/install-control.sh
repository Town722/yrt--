#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run as root.' >&2
    exit 1
fi
if [ ! -d /sys/class/udc/fc000000.usb ]; then
    printf '%s\n' 'The RK3588 OTG port must be in peripheral mode first.' >&2
    exit 1
fi

install -d -m 0755 /opt/yrt-remote-control
install -m 0644 "$here/control_receiver.py" "$here/control_protocol.py" \
    "$here/usb_receiver_probe.py" /opt/yrt-remote-control/
install -m 0644 "$here/yrt-remote-control.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart yrt-remote-control.service
systemctl is-active yrt-remote-control.service
