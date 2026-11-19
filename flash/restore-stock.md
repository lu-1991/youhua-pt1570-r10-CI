# 恢复原厂固件

有两种恢复方式。**优先用第一种**（U-Boot 直写，不依赖系统能不能起来）。

## 方式 A：U-Boot + TFTP 整块回写 `ubi_device`（推荐）

把原厂 `mtd4` 全盘备份切块并生成命令：

```bash
python tools/split_dump.py ../pt1570-r10-backup/mtd4-ubi_device.bin -o restore
python tools/tftp_serve.py restore
```

会得到 6 个 `ubi-part-XX.bin`（每块 48 MiB，因为 250 MB 塞不进 256 MB 内存）和
`restore-cmds.txt`。串口进 U-Boot 后按顺序粘贴：

```
setenv ipaddr 192.168.1.2
setenv serverip 192.168.1.7
ping ${serverip}

nand erase.part ubi_device
tftpboot 0x5000000 ubi-part-01.bin
nand write 0x5000000 0x340000 0x3000000
tftpboot 0x5000000 ubi-part-02.bin
nand write 0x5000000 0x3340000 0x3000000
... (照 restore-cmds.txt 全部粘完)
```

然后恢复原厂环境变量与启动参数：

```
# 环境被改过时: 先把 mtd1-env.bin 复制成 uboot-env.bin 放进 TFTP 目录
run upenv

setenv sw_commit 1
setenv sw_active 1
setenv more_args ubi.mtd=4 root=31:8 rootfstype=squashfs
saveenv
reset
```

重启后就是原厂系统（`http://192.168.1.1`，原厂账号）。

注意事项：

* `nand erase.part ubi_device` 会把 12/13 号"工厂数据副本"一起清掉 —— 这正是"回原厂"的
  目的，原厂系统用 `ubi_Config`（也是从备份里回写的）。
* 每块写完不要急着重启，全部写完再 `reset`。
* 如果 U-Boot 里 `nand write` 报 `Bad block`，说明该块是坏块；`nand write` 默认跳过，
  继续执行后面的块即可（UBI 本身能容忍，原厂备份是逐块逻辑地址转储）。
* 恢复后 `ubi info` 应该只有 11 个卷，且 `ubi_r0` 大小 33,648,640 B。

## 方式 B：原厂 Web 界面升级

如果原厂系统还能起来（比如只是刷了 OpenWrt 但没动 `ubi_Config`，且能通过原厂 web 升级），
用运营商的升级包刷回即可。注意 OpenWrt 已经把厂商卷删了，这条路通常走不通，**别指望它**。

## 方式 C：连 boot 分区一起恢复（慎用）

只有在 `tftpboot` 之前就没有任何串口输出、怀疑 boot 分区被写坏时才需要：

```
tftpboot 0x5000000 mtd0-boot.bin
nand erase 0x0 0x200000
nand write 0x5000000 0x0 0x200000
reset
```

`mtd0-boot.bin` 必须是你自己 dump 的那 2 MiB。**不要**用 `pt1570-r10.boot.bin`：
它是 Realtek 打包格式（`0xAA640001` 头 + 压缩段），按裸镜像写进 NAND 起不来。

## 恢复后确认

```
printenv sw_commit sw_active more_args     # 1 / 1 / ubi.mtd=4 root=31:8 rootfstype=squashfs
ubi part ubi_device
ubi info                                   # 11 个卷, ubi_r0 = 33648640
```

再想刷回 OpenWrt，就按 [README.md](README.md) 重新走一遍（`sw_commit` 记得再改回 0）。
