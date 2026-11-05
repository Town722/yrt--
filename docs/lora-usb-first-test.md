# LoRa USB 首轮测试

日期：2026-11-05。

## 已确认

用户确认移动端使用Ubuntu，LoRa已完成USB转接。地面端按此前方案使用拯救者电脑，系统及串口号需实际确认。此前提供HD-M805 V3.0手册，实物载板、参数和固件行为仍以读取和测试为准。

## 第一步：识别两端串口

移动端接上LoRa USB转接器，在Ubuntu终端依次执行：

```bash
lsusb
ls -l /dev/serial/by-id/
```

如果第二条显示目录不存在，再执行：

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

插拔对比可以确认新增的设备；设备名不能预先固定为ttyUSB0。如串口没有权限，先查看设备所属组与当前用户组，再处理权限，不直接全局修改串口权限。

地面端如使用Windows：在设备管理器的“端口（COM和LPT）”中记录LoRa转接器名称及COM号。不要误选4G模块的AT/诊断串口。

## 第二步：读取配置，再进行短消息互通

待获得两端串口信息后，读取或核实实际配置。手册默认串口9600/8N1、空速1.46kbps，但出厂载板可能预设不同参数，不能直接视为实测值。首次透传优先模式0（SETA=0、SETB=0）；模式脚应通过实际转接板开关/跳线设置，具体方法按板卡确认。

确认匹配天线已接好，频率和功率符合测试场地条件后，开展短编号消息双向收发。控制发送间隔，不以9600串口速率持续灌入空速较低的模块。先验证普通透传，再单独验证模式1的RSSI格式及唤醒时延；详见到货硬件记录。

尚未得到串口枚举、参数读取或收发测试结果。

## 实际检查结果（2026-11-05）

- 已通过SSH登录用户ztl，主机名RK3588；eth0为192.168.137.246/24，另一有线接口enP3p49s0为DOWN。地址为本次会话结果，后续可能变化。
- lsusb显示移远设备VID:PID=2c7c:0125，通用USB数据库将其显示为EC25；by-id描述符明确为EC20-CE-HDLG，与实物EC20一致，不据通用USB名称改判模块型号。
- by-id中ttyUSB0、ttyUSB1、ttyUSB2、ttyUSB3均属于EC20，不能当作LoRa端口使用。
- 当前USB列表未发现可归属于LoRa的转接器；不据此断定模块损坏。下一步使用sudo dmesg -w观察插拔LoRa USB的枚举日志，检查连接、供电、数据线及驱动。
- 本轮尚未进行LoRa收发测试。

## 接入LoRa后的检查结果

用户确认此前未接LoRa，因此之前的设备列表缺失并非识别故障。接入后：

- USB VID:PID为1a86:7523，系统名称QinHeng Electronics HL-340 USB-Serial adapter。
- 当前串口/dev/ttyUSB10。
- 稳定路径/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0，后续程序优先使用该路径；若接入多个同描述符转接器，需要另行区分。
- 转接器枚举成功，不等于LoRa射频收发已经通过；尚待串口权限、参数、工作模式及地面端COM号确认。

## 停电进度存档（2026-11-05）

用户因停电暂停测试。最新状态如下：

### 已确认

- 移动端Ubuntu可通过SSH登录，用户名ztl、主机名RK3588。
- 本次成功登录地址192.168.137.246，接口eth0；重启后的DHCP租约可能变化。
- LoRa已接USB；转接芯片枚举1a86:7523，当前/dev/ttyUSB10，固定路径/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0。
- /dev/ttyUSB10权限crw-rw----、属组dialout；ztl已属于dialout，无需重复添加组或全局修改权限。
- python3可运行，但import serial报ModuleNotFoundError，pyserial尚未确认安装。
- EC20对应ttyUSB0–ttyUSB3，不作为LoRa串口使用。

### 中断点与未完成内容

- SSH出现“client_loop: send disconnect: Connection reset”，终端回到Windows PowerShell。
- 随后的sudo apt-get命令误在Windows执行并报命令不存在，不能据此认为Ubuntu已更新软件源或安装串口库。
- Windows地面端LoRa的COM号尚未收到。
- 两台LoRa的实物工作模式、频点、空速及串口设置尚未核对；未执行射频收发、RSSI验证或9轮竞速。

### 来电后恢复顺序

1. 开发板恢复供电、网线和LoRa USB连接；先尝试上次地址：
   ```powershell
   ssh -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 ztl@192.168.137.246
   ```
   失败时重新检查有线口地址和DHCP租约，不将旧ARP静态记录当作在线证据。
2. 看到ztl@RK3588提示符后，才在Ubuntu运行：
   ```bash
   ip -br addr
   ls -l /dev/serial/by-id/
   sudo apt-get update
   sudo apt-get install -y python3-serial
   python3 -c "import serial; print(serial.__version__)"
   ```
   如下载报错，先排查板端联网；无需在Windows执行sudo。
3. 在单独的Windows PowerShell窗口记录地面端串口：
   ```powershell
   Get-CimInstance Win32_SerialPort | Format-Table DeviceID, Name -AutoSize
   ```
4. 核对实际模块参数和转接板模式开关，再开展短消息双向收发；随后验证RSSI格式。尚未编写或运行测试程序。

设备与网桥资料分别见hardware-arrival-2026-11-05.md和wifi-bridge-first-test.md。登录密码未存入仓库。
