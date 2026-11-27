# 三链路室内数传真机部署

Windows 主动发送模拟指令，RK3588 监听并记日志。车轮和转向仍由底盘原装遥控器控制。真机发包、取日志、回放和导出可在 Windows 桌面应用里完成。

## Windows 桌面应用

双击 `YRT-ThreeLink-Station.exe`。已部署的电脑使用 `D:\YRT\three-link\windows.json` 和同目录的 `logs`。将单文件 EXE 复制到其他电脑运行时，程序会在 `%LOCALAPPDATA%\YRT\ThreeLink` 保存默认配置和日志，可以在应用的「接口设置」中修改串口、地址和接口索引。

默认自动检测 HD-M805 的 CH340 转接板（`1A86:7523`）和移远 4G 的 USB AT 口。打开 LoRa 发送口和 4G 拨号时会重新检测 COM 号。如果插了多个同类设备，在「接口设置」取消自动识别，再手动指定各自的 COM 号。Windows 4G 网卡接口索引会按「接口设置」里的本地网卡 IP 动态识别，重新插拔后索引变化也能继续使用。

### 手机网页控制和车端取数

`ground-station/mobile-control-preview.html` 现在是 API 客户端，不再伪造实验结果。完整链路由 `deploy/mobile-gateway/` 提供：手机网页通过网站 HTTPS 调 `/api/v1`；Windows EXE 用单独 PC 令牌主动轮询命令并回传链路状态；RK3588 代理用车端令牌主动轮询 4G 批量取日志命令。网关只监听 `127.0.0.1:8765`，由现有 Nginx HTTPS 网站反向代理，不占用 ECS 的 14550/14551 实验数传端口。

电脑端「车端数据」选择支持两种模式：

- **网桥实时传输**：电脑经桥接网 SSH 实时读取 RK3588 接收日志；电脑和手机可以边测边看，实验结束后从网桥补齐完整日志并归档到网关，电脑离线后手机仍可查看。
- **4G 实验后回传**：电脑停止实时 SSH 读取；发包完成后向网关请求本轮数据，RK3588 经 LTE 上传本轮 `rx.jsonl` 记录，电脑和手机收到同一份结果。

手机的「历史实验」列表显示网关中已归档的实验。选择编号可翻查接收记录和回放统计。首次部署后可在电脑配置 HTTPS 地址和 PC 令牌，再运行 `python deploy/three-link/publish_history.py --station-dir D:\YRT\three-link` 导入这台电脑已有的历史接收记录。

部署步骤见 `../mobile-gateway/README.md`。需要一个可用的 HTTPS 网站域名；分别将 PC 令牌填入电脑应用「接口设置」中的手机控制网关字段，将手机令牌填入网页，将车端令牌写入板端 `/etc/yrt-mobile-gateway/vehicle.json`。令牌只保存在本地配置和服务端环境文件中，不能提交仓库。

1. RK3588 上电，确认两台网桥已配对。EC200A 重新上电后，可以点击「4G 拨号」，等应用报数据连接已建立。
2. 勾选要测试的链路，设置条数和频率，点击「开始发包」。需要提前结束时点击「停止发包」，程序会保存日志和结束帧。
3. 测试中，配置过免密 SSH 的 Windows 可以实时查看车端到达明细，结束后自动取回完整日志。网桥中途断开时，车端继续记日志。网桥恢复后可点击「从 RK3588 取日志」，也可以选「导入日志」打开已有的 `rx.jsonl`。
4. 从实验下拉框选实验，查看按车端到达顺序排列的接收副本，或切换到「回放」。发送和回放都默认选择三条链路，需要排除时由操作人员取消勾选。点击「导出 CSV」保存两端合并明细。

新实验按 Windows 本地时间编号，格式为 `YYYYMMDD-HHMM`，如 `20261120-2045`。同一分钟内第 2 轮加 `-02`，后续递增。发送记录、车端记录和导出文件使用同一个编号，旧编号仍可回放。升级时同时更新 Windows 应用和 RK3588 接收程序。

「车端接收」显示各链路接收数量。接收明细展示到达顺序、指令与处理结果，并保留两个时差：相对首达差使用车端单调时钟；发送提交到接收差使用发送前记录的 Windows 高精度 perf_counter、车端接收单调时钟，以及每轮通过 SSH 测得的两端偏移，不依赖两端系统日期。界面不显示无线测量或归一化质量。校准 RTT 和板端启动 ID 随发送日志保存；板端在实验中重启时会标明该轮校准失效。由于极短的单程时差可能落在校时不确定度内，负估计或低于校时精度的值会在界面标成校时误差，不作为有效负时延；CSV 保留原始估计、校准 RTT 和状态供复核。

### 车载图传

在地面站点击「图传」，选择「Wi-Fi 网桥」或「4G 低延迟」，再点击「开始预览」。在预览中切换链路会自动重新连接。

| 模式 | 车端传输 | 视频配置 |
| --- | --- | --- |
| Wi-Fi 网桥 | 网桥 SSH 直连 | MJPEG，640×480，15 帧/秒 |
| 4G 低延迟 | RK3588 经绑定的 LTE 网口上传到 ECS，电脑经自己的互联网连接查看 | RK3588 MPP 硬编码 H.264，1280×720，20 帧/秒，目标 1.2 Mbit/s，GOP 10 |

4G 使用持续 HTTPS 上传和接收。ECS 保留短时帧队列，落后过多时从新的关键帧继续。窗口断流时标明等待新画面。配置和现场验收见[复盘](../../docs/2026-11-27-三链路与图传复盘.md)。

RK3588 安装摄像头服务前，先按[网关部署](../mobile-gateway/README.md)安装车端代理。板端需要 USB 摄像头、`v4l2-ctl`、可用的 GStreamer/GI 和 Rockchip `mpph264enc` 插件。本项目板端用户为 `ztl`，应可以打开摄像头。程序优先通过 `/dev/v4l/by-id/*-video-index0` 找 Astra Pro；其他机器可通过环境变量 `YRT_CAMERA_DEVICE` 指定设备路径。

```sh
sudo sh deploy/three-link/install-camera.sh
systemctl status yrt-camera.service --no-pager
journalctl -u yrt-camera.service -n 20 --no-pager
```

### 构建 Windows 应用和 LoRa 对测工具

从仓库根目录使用 Python 3.12 构建。打包使用仓库里的配置模板，安装时用应用界面配置私有参数。

```powershell
python -m pip install pyserial==3.5 Pillow==10.3.0 av==16.1.0 pyinstaller==6.22.3
python deploy/three-link/build_windows.py
```

产物在 `dist/`。LoRa 对测工具在同一台 Windows 电脑上测两个 USB 转串口之间的收发时差，支持 `--frame-bytes`、`--jitter`，逐帧 CSV 写到「下载」下的 `YRT-LoRa-Bench-Logs`。板端和地面站的实测对照见[LoRa 对照数据](../../docs/2026-11-27-LoRa对照数据.md)。

| 路径 | Windows | RK3588 |
| --- | --- | --- |
| LoRa | `COM10`，9600 8N1 | CH340 by-id 串口，9600 8N1 |
| Wi-Fi 网桥 | `192.168.137.10` | `192.168.137.246:14552`，UDP |
| 4G | EC200A 网卡，经 ECS `14550/TCP` | EC20 蜂窝网口，经 ECS `14551/TCP` |

每包 20 字节，包含帧标识、64 位实验编号、32 位序号、模拟指令值和 CRC32。同一序号的三份帧内容相同。每路单独排队发送。板端只采用高于本轮已采用序号的有效帧，仍记录重复和晚到帧。开始和结束帧用于对齐接收观察窗口。提交成功表示数据交给了本机接口，送达情况以板端日志为准。

## 安装接收服务

将此目录复制到 RK3588，确认已安装 `python3-serial`。首次安装前检查 `receive.json` 中的串口和接口名，有变动时用 `ls -l /dev/serial/by-id/` 和 `ip -br -4 addr` 核对。HD-M805 模式 0 不追加 RSSI 字节，`lora_rssi_suffix` 应设为 `false`；模式 1 实际追加 RSSI 字节时才设为 `true`。模式 0 下若仍按有 RSSI 尾字节解析，接收器会等串口 200 ms 读超时才交付完整帧。

```bash
sudo sh install-receiver.sh
systemctl status yrt-fusion-rx.service --no-pager
journalctl -u yrt-fusion-rx.service -n 20 --no-pager
```

接收服务开机自启。部署后的配置是 `/etc/yrt-fusion/receive.json`，重复安装会保留这份配置。日志写到 `/var/lib/yrt-fusion/rx.jsonl`。如果板端蜂窝网口或串口更名，修改配置并重启服务。

当前车端网桥地址为 `192.168.255.201`，RK3588 的 `eth0` 增加 `192.168.255.11/24` 作为管理网段地址。网桥采样程序每 2 秒只读 GET `/Main_WStatus_Content.asp`。单独准备 `/etc/yrt-fusion/bridge-auth.json`，写入 `host`、`source`、`username` 和 `password`，授权接收服务用户读该文件。凭据保存在板端本地，切勿提交仓库。关闭采样可移走该文件后重启接收服务。Wi-Fi 副本关联车端同一时钟上 6 秒以内最近一次读数。

## 运行 Windows 发送器

Windows 部署目录为 `D:\YRT\three-link`。另一台电脑运行前安装 Python 3 和 `pyserial`，并按实际连线更新 `windows.json`。可用 `Get-NetAdapter` 和 `Get-NetIPAddress -AddressFamily IPv4` 检查 EC200A 的接口索引、地址和有线网桥地址。EC200A 重新上电后若已注册 LTE 但 RNDIS 未联网，在它的 AT 口手动执行 `AT+QNETDEVCTL=1,1,1`，等 `+QNETDEVSTATUS: 1`。

```powershell
cd D:\YRT\three-link
python fusion.py send windows.json --count 20 --hz 1
```

命令打印本轮实验编号，并在 `logs` 保存发送明细。`--omit wifi` 可在一次实验中只发 LoRa 和 4G。`--sequences 1,3,2,4` 可检查晚到序号是否会回退状态。串口被其他软件占用时，先退出串口工具。

## 回放和 CSV 导出

```powershell
cd D:\YRT\three-link
scp ztl@192.168.137.246:/var/lib/yrt-fusion/rx.jsonl .\logs\rx.jsonl
$run = '填入发送器打印的实验编号'
python fusion.py report .\logs\rx.jsonl $run
python fusion.py merge ".\logs\tx-$run.jsonl" .\logs\rx.jsonl ".\logs\merged-$run.csv"
```

回放报告用车端单调时钟，默认计算三路全开的采用指令数和最长更新空档。需要排除链路时显式传入 `--omit wifi` 等选项。CSV 按实验编号、序号和链路合并两端记录。新实验开始时，Windows 发送器会通过 SSH 对 RK3588 和 Windows 的高精度单调时钟做四时间戳校准；CSV 记录发送提交时刻、车端接收时刻、校准偏移、校准往返时间以及发送提交到接收的时差。此时差不依赖两台设备的系统日期。旧实验没有保存校准偏移，因此仍显示“历史记录无校时”，不能从旧日志补算单程时差。
