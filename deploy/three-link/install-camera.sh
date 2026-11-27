#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root." >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CAMERA=/home/ztl/.local/lib/yrt-camera
test -r /etc/yrt-mobile-gateway/vehicle.json
command -v v4l2-ctl >/dev/null
runuser -u ztl -- gst-inspect-1.0 mpph264enc >/dev/null
runuser -u ztl -- /usr/bin/python3 -c 'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstApp", "1.0"); from gi.repository import Gst, GstApp'

runuser -u ztl -- mkdir -p "$CAMERA"
install -m 0644 "$HERE/vehicle_camera.py" "$HERE/h264_capture.py" "$HERE/video_transport.py" "$CAMERA/"
install -m 0644 "$HERE/yrt-camera.service" /etc/systemd/system/yrt-camera.service
systemctl daemon-reload
systemctl enable yrt-camera.service
systemctl restart yrt-camera.service
systemctl is-active yrt-camera.service
