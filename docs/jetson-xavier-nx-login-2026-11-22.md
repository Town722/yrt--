# Jetson Xavier NX 登录信息

2026 年 11 月 22 日已在 Windows 上通过 Micro-USB 验证 SSH 登录。

- 用户名：`nx36680001`
- 密码：`123456`
- 主机名：`jetson-nx`
- USB 网络地址：`192.168.55.1`
- 系统：Ubuntu 18.04，NVIDIA L4T R32.6.1
- 根分区：`/dev/nvme0n1p1`（NVMe SSD）

在 Windows PowerShell 中连接：

```powershell
ssh -o HostKeyAlias=jetson-xavier-nx-20261122 nx36680001@192.168.55.1
```

首次连接时核对 ED25519 主机指纹：`SHA256:WYbiP+BH62iAr4c+ExhKiBlE4TZqxlWPVoQ90P/F1Ow`。
