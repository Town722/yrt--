#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG=/etc/yrt-mobile-gateway/vehicle.json
if [ ! -s "$CONFIG" ]; then
    echo "Create $CONFIG with the HTTPS gateway URL and vehicle token before installing." >&2
    exit 2
fi
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["url"].startswith("https://") and len(d["token"]) >= 32' "$CONFIG"
install -d -m 0755 /opt/yrt-mobile-gateway
install -m 0644 "$HERE/vehicle_agent.py" /opt/yrt-mobile-gateway/vehicle_agent.py
install -m 0644 "$HERE/yrt-mobile-vehicle.service" /etc/systemd/system/yrt-mobile-vehicle.service
chown root:ztl "$CONFIG"
chmod 0640 "$CONFIG"
systemctl daemon-reload
systemctl enable yrt-mobile-vehicle.service
systemctl restart yrt-mobile-vehicle.service
