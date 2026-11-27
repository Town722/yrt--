# YRT 手机控制网关

这套服务让手机 4G 控制 Windows 地面站，并读取电脑链路状态和 RK3588 车端接收记录。它与实验数传 TCP 透传分开运行。

## 链路

- 手机浏览器请求同一 HTTPS 网站的 `/yrt-mobile/api/v1`。
- Windows EXE 用 PC 令牌主动连网关，接收开始、停止、拨号、取日志命令，并上传电脑链路状态和网桥实时记录。
- RK3588 `vehicle_agent.py` 用 4G 主动连网关；只在收到本轮上传命令后读取并上传指定实验的日志。上传在后台执行，期间继续报告在线状态。
- `vehicle_mode=bridge` 时电脑经 SSH 实时读取并转发车端日志，实验结束后将完整接收日志归档到网关。`vehicle_mode=lte` 时 RK3588 在实验结束后经 4G 上传原始日志，电脑读取并计算时差，然后发布完整结果。手机通过 HTTPS 查看电脑发布的实时记录和完整归档，电脑离线后仍可查看历史。

## ECS 部署

在 ECS 上复制整个仓库（安装脚本需要 `ground-station/`），然后执行：

```sh
sudo sh deploy/mobile-gateway/install-server.sh
```

脚本生成独立的 phone、PC、vehicle 三个令牌，保存到 `/etc/yrt-mobile-gateway/server.env`，服务绑定 `127.0.0.1:8765`。把 `nginx-location.conf` 中的三个 location 合并到现有 HTTPS 网站 server block，保留域名和证书。可用 `install-site.py --site /etc/nginx/sites-enabled/yuyunchang --host 你的域名` 完成安装和语法检查。网站打开路径为：

```text
https://你的域名/yrt-mobile/
```

手机页和 API 必须同源 HTTPS。阿里云安全组只需确保网站已有的 443 可由手机、电脑和车端访问；不需要把 8765 暴露公网。

部署后可在 ECS 本机查看服务状态：

```sh
sudo systemctl status yrt-mobile-gateway.service --no-pager
curl -fsS http://127.0.0.1:8765/health
```

`/etc/yrt-mobile-gateway/server.env` 含三组秘密令牌。分别在本地查看并配置，不要把内容贴到聊天或提交 Git。

### 已运行网关的 4G 图传升级

已安装 11 月 27 日 JPEG 图传接口的服务器可以增量升级。将 `install-h264.py`、`h264_gateway.py` 和 `../three-link/video_transport.py` 复制到 ECS 的同一目录，以 root 执行 `python3 install-h264.py`。更早的版本先把同目录的 `install-video.py` 和 `gateway.py` 复制到 ECS 并执行 `python3 install-video.py`。两次升级均保留现有令牌、数据库和网站内容。

H.264 安装器能识别站点直接写入的代理段，以及 `/etc/nginx/snippets/yrt-mobile.conf` 的 include。它为连续 H.264 上传增加一个精确匹配入口，关闭该入口的请求缓冲，用现有 HTTPS 443 转发，升级前备份原文件并在语法或服务检查失败时回滚。云端和车端升级后，Windows 地面站需更新到 v28 或更新版本。

## Windows 地面站

更新并运行桌面 EXE，在「接口设置」填写：

- 手机控制网关 HTTPS 地址：`https://你的域名/yrt-mobile`
- 手机控制 PC 令牌：`server.env` 中的 `YRT_TOKEN_PC`

保存后网关连接自动重建。手机网页的「手机访问令牌」填 `YRT_TOKEN_PHONE`。在电脑或手机的实验设置中选择车端数据方式；PC 状态心跳 2 秒一次，网关 10 秒未收到即显示离线。

## RK3588 的 4G 批量回传

在板端创建 `/etc/yrt-mobile-gateway/vehicle.json`：

```json
{
  "url": "https://你的域名/yrt-mobile",
  "token": "YRT_TOKEN_VEHICLE 的实际值",
  "log": "/var/lib/yrt-fusion/rx.jsonl"
}
```

复制 `deploy/mobile-gateway/` 到板端仓库后运行：

```sh
sudo sh deploy/mobile-gateway/install-vehicle-agent.sh
sudo systemctl status yrt-mobile-vehicle.service --no-pager
```

代理每 5 秒报告在线状态，轮询间隔 1 秒。它只能上传收到的 `run` 对应记录，不提供远程 Shell 或任意文件读取。上传失败会继续重试；日志上限 30 MiB/轮。

## 安全边界和限制

- 令牌通过 HTTPS Bearer header 发送，服务端分 phone、PC、vehicle 三个独立角色；令牌至少 32 字符。
- Nginx 只允许现有 HTTPS 入口到本机回环网关。不要开放公网 8765，也不要把原始 14550/14551 数传端口复用为 Web API。
- SQLite 状态位于 `/var/lib/yrt-mobile-gateway/state.sqlite3`。命令在设备确认前会暂时租出，未确认时到期重投，服务重启后仍可恢复；PC 命令租约 30 秒，车端上传命令租约 60 秒。车端代理单实例后台上传并继续心跳，失败后可快速重试。
- 如果网站域名、HTTPS 证书或 Nginx 不在这台 ECS 上，需要把 `/yrt-mobile/api/` 反代到 ECS 的私网地址，并让三个节点使用同一个 HTTPS 入口。
- 这是初版单车、单地面站网关；服务器重装时需安全备份 `server.env` 和数据库。更换令牌时同步更新 Windows 和板端配置。
