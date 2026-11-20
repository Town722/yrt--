# 三链路室内数传真机部署

Windows 主动发送模拟指令，RK3588 监听并记日志。车轮和转向仍由底盘原装遥控器控制。地面站网页继续用于模拟预览。真机发包、回放和导出由下面的命令运行。

| 路径 | Windows | RK3588 |
| --- | --- | --- |
| LoRa | `COM10`，9600 8N1 | CH340 by-id 串口，9600 8N1 |
| Wi-Fi 网桥 | `192.168.137.10` | `192.168.137.246:14552`，UDP |
| 4G | EC200A 网卡，经 ECS `14550/TCP` | EC20 蜂窝网口，经 ECS `14551/TCP` |

每包 20 字节，包含帧标识、64 位实验编号、32 位序号、模拟指令值和 CRC32。同一序号的三份帧内容相同。每路单独排队发送。板端只采用高于本轮已采用序号的有效帧，仍记录重复和晚到帧。开始和结束帧用于对齐接收观察窗口。提交成功表示数据交给了本机接口，送达情况以板端日志为准。

## 安装接收服务

将此目录复制到 RK3588，确认已安装 `python3-serial`。首次安装前检查 `receive.json` 中的串口和接口名，有变动时用 `ls -l /dev/serial/by-id/` 和 `ip -br -4 addr` 核对。

```bash
sudo sh install-receiver.sh
systemctl status yrt-fusion-rx.service --no-pager
journalctl -u yrt-fusion-rx.service -n 20 --no-pager
```

接收服务开机自启。部署后的配置是 `/etc/yrt-fusion/receive.json`，重复安装会保留这份配置。日志写到 `/var/lib/yrt-fusion/rx.jsonl`。如果板端蜂窝网口或串口更名，修改配置并重启服务。

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

回放报告用车端单调时钟，按同一份记录比较三路全开与去掉任意一路后采用的指令数和最长更新空档。CSV 按实验编号、序号和链路合并两端记录。两台机器的单调时钟各自独立，CSV 中的发送与接收时间不可直接相减得到单程时延。
