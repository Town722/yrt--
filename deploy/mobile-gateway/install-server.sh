#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /opt/yrt-mobile-gateway /etc/yrt-mobile-gateway /var/www/yrt-mobile
if ! id yrt-mobile-gateway >/dev/null 2>&1; then
    useradd --system --home /nonexistent --shell /usr/sbin/nologin yrt-mobile-gateway
fi
install -m 0644 "$HERE/gateway.py" /opt/yrt-mobile-gateway/gateway.py
install -m 0644 "$HERE/h264_gateway.py" "$HERE/../three-link/video_transport.py" /opt/yrt-mobile-gateway/
install -m 0644 "$HERE/control_gateway.py" "$HERE/../three-link/control_protocol.py" /opt/yrt-mobile-gateway/
install -m 0644 "$HERE/yrt-mobile-gateway.service" /etc/systemd/system/yrt-mobile-gateway.service
install -m 0644 "$HERE/../../ground-station/mobile-control-preview.html" /var/www/yrt-mobile/index.html
install -m 0644 "$HERE/../../ground-station/brand-logo.png" /var/www/yrt-mobile/brand-logo.png
chown -R root:root /var/www/yrt-mobile

if [ ! -e /etc/yrt-mobile-gateway/server.env ]; then
    umask 077
    {
        printf 'YRT_TOKEN_PHONE=%s\n' "$(openssl rand -hex 32)"
        printf 'YRT_TOKEN_PC=%s\n' "$(openssl rand -hex 32)"
        printf 'YRT_TOKEN_VEHICLE=%s\n' "$(openssl rand -hex 32)"
    } > /etc/yrt-mobile-gateway/server.env
fi
chmod 0600 /etc/yrt-mobile-gateway/server.env
systemctl daemon-reload
systemctl enable --now yrt-mobile-gateway.service
systemctl restart yrt-mobile-gateway.service
printf '%s\n' 'Gateway installed. Add nginx-location.conf to the existing HTTPS site, then reload nginx.'
printf '%s\n' 'Read tokens locally from /etc/yrt-mobile-gateway/server.env; do not paste them into chat or commit them.'
