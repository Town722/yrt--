# 三链路室内数传真机部署

Windows 主动发送模拟指令，RK3588 监听并记日志。车轮和转向仍由底盘原装遥控器控制。真机发包、取日志、回放和导出可在 Windows 桌面应用里完成。

## Windows 桌面应用

双击 `YRT-ThreeLink-Station.exe`。已部署的电脑使用 `D:\YRT\three-link\windows.json` 和同目录的 `logs`。将单文件 EXE 复制到其他电脑运行时，程序会在 `%LOCALAPPDATA%\YRT\ThreeLink` 保存默认配置和日志，可以在应用的「接口设置」中修改串口、地址和接口索引。

默认自动检测 HD-M805 的 CH340 转接板（`1A86:7523`）和移远 4G 的 USB AT 口。打开 LoRa 发送口和 4G 拨号时会重新检测 COM 号。如果插了多个同类设备，在「接口设置」取消自动识别，再手动指定各自的 COM 号。Windows 4G 网卡接口索引会按「接口设置」里的本地网卡 IP 动态识别，重新插拔后索引变化也能继续使用。

1. RK3588 上电，确认两台网桥已配对。EC200A 重新上电后，可以点击「4G 拨号」，等应用报数据连接已建立。
2. 勾选要测试的链路，设置条数和频率，点击「开始发包」。需要提前结束时点击「停止发包」，程序会保存日志和结束帧。
3. 测试中，配置过免密 SSH 的 Windows 可以实时查看车端到达明细，结束后自动取回完整日志。网桥中途断开时，车端继续记日志。网桥恢复后可点击「从 RK3588 取日志」，也可以选「导入日志」打开已有的 `rx.jsonl`。
4. 从实验下拉框选实验，查看按车端到达顺序排列的接收副本，或切换到「回放」。发送和回放都默认选择三条链路，需要排除时由操作人员取消勾选。点击「导出 CSV」保存两端合并明细。

新实验按 Windows 本地时间编号，格式为 `YYYYMMDD-HHMM`，如 `20261120-2045`。同一分钟内第 2 轮加 `-02`，后续递增。发送记录、车端记录和导出文件使用同一个编号，旧编号仍可回放。升级时同时更新 Windows 应用和 RK3588 接收程序。

「车端接收」始终列出 LoRa、Wi-Fi 网桥和 4G 各自的无线测量。接收明细保留两个时差：相对首达差使用车端单调时钟；发送提交到接收差使用发送前记录的 Windows 高精度 perf_counter、车端接收单调时钟，以及每轮通过 SSH 测得的两端偏移，不依赖两端系统日期。校准 RTT 和板端启动 ID 随发送日志保存；板端在实验中重启时会标明该轮校准失效。CSV 同时保留两种时差、原始时钟值、校准偏移和到达顺序。无线质量和归一化算法保持不变，详情见[射频归一化和干扰复核](../../docs/radio-quality-2026-11-20.md)。

在 Windows 上从源码构建单文件应用：

```powershell
python -m pip install pyserial pyinstaller
python -m PyInstaller --onefile --windowed --name YRT-ThreeLink-Station --add-data 'windows.json;.' desktop.py
```

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
