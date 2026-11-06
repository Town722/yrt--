# YRT 4G 云端中继

沿用 2026 年 7 月无人船工程的四个 TCP 端口。RK3588 和地面站各自通过 4G 主动连接 ECS。视频是 TCP 上的 MPEG-TS，数传是单车与单地面站间双向透传的 MAVLink 字节流。

| TCP 端口 | 连接方 | 用途 |
| --- | --- | --- |
| 15554 | RK3588 | 推送 MPEG-TS 视频 |
| 8554 | 地面站 | 接收 MPEG-TS 视频 |
| 14551 | RK3588 | 船端数传 |
| 14550 | 地面站 | 地面端数传 |

## 安装

将本目录复制到 Ubuntu ECS，在该目录执行 `sudo bash install.sh`。依赖系统自带的 Python 3 和 systemd。服务以动态的非特权用户运行并开机启动。

默认监听 `127.0.0.1`，可通过现有 SSH 连接做本地转发。查看服务日志可用 `sudo journalctl -u yrt-relay@video -u yrt-relay@mavlink -n 50 --no-pager`。

## 从 4G 直接接入

1. 在 ECS 安全组入方向新增 TCP 规则。把 15554、14551 的授权源限定为 RK3588 的公网出口，把 8554、14550 的授权源限定为地面端的公网出口。使用单一公网 IPv4 时填写 `/32`。4G 模组的本地或 PDP 地址不能用作授权源。
2. 如果 ECS 已启用主机防火墙，也按同样的源地址和端口放行。
3. 在 `/etc/yrt-relay.env` 中设置 `RELAY_BIND=0.0.0.0`，执行 `sudo systemctl restart yrt-relay@video yrt-relay@mavlink`。两端程序使用这台 ECS 的公网地址及表中的端口。

原始 TCP 流没有应用层鉴权和加密。请勿将四个端口授权给 `0.0.0.0/0`。运营商出口变化时需要更新安全组授权源，也可以保持默认监听并通过 SSH 转发。
