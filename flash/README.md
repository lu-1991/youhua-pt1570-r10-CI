# PT1570-R10 刷机手册（原厂 U-Boot + TFTP）

本流程**不换 U-Boot、不写 boot 分区**，只往 `ubi_device` 里写 3 个卷、改两个环境变量。
全程可回退：环境变量能改回来，原厂固件能用 `mtd4` 全盘备份恢复。

> 需要的东西：USB-TTL 串口线（3.3V）、网线、电脑（Windows 即可，仓库脚本只要 Python 3.9+）、
> `pt1570-r10-backup/mtd4-ubi_device.bin`（原厂全盘备份，务必先另存一份到别的硬盘）。

---

## 0. 先备份（已做过可跳过，但请确认文件还在）

你已有下面三个转储，刷机前确认它们是完好的，并复制到另一台机器/移动硬盘：

| 文件 | 内容 | 恢复时用 |
| --- | --- | --- |
| `mtd4-ubi_device.bin` | 整个 UBI 区 257,687,552 B | 恢复原厂系统、工厂数据 |
| `mtd0-boot.bin` | 原厂 U-Boot 2 MiB | 万一把 boot 分区写坏 |
| `mtd1-env.bin` / `mtd2-env2.bin` | 原厂环境变量 | 恢复启动参数 |

`pt1570-r10.boot.bin`（2 MiB，Realtek 打包格式）和 `mtd0-boot.bin`（NAND 裸镜像）**不是同一个文件**，
刷机也用不到它们 —— 本流程不碰 mtd0。

## 1. 接串口，确认能停在 U-Boot

1. 断电，USB-TTL 接板上调试口：**GND 对 GND，TTL-RX 接板子 TX，TTL-TX 接板子 RX**，
   电平 3.3V，**不要接 5V**。板子丝印不统一，用"开机时哪个脚有输出"来认 TX 最稳。
2. 串口工具（PuTTY / MobaXterm / `python -m serial.tools.miniterm COM3 115200`）：
   **115200 8N1，无流控**。
3. 上电，看到 `U-Boot 2020.01-svn28407 ... Taurus-SoC` 时**狂按回车/任意键**，停在
   `Taurus # ` 提示符（`bootdelay=1`，只有 1 秒，动作要快；停不下来就多重启几次）。

停住后先看清现状，把输出贴到文本里留档：

```
printenv bootcmd sw_commit sw_active sw_tryactive
printenv mtdparts
ubi part ubi_device
ubi info
```

`ubi info` 应该列出 11 个卷，**顺序和 ID 都要是这样**：

```
0 ubi_Config   1 ubi_DTB0   2 ubi_k0     3 ubi_r0
4 ubi_DTB1     5 ubi_k1     6 ubi_r1
7 ubi_framework1  8 ubi_framework2  9 ubi_apps  10 ubi_dpi
```

注意没有 `ubi_fpga`（那是上游 TYFZA1 的卷），而多一个 `ubi_dpi`。如果对不上，**停下**，
先把 `ubi info` 的输出发出来核对，不要继续。

## 2. 电脑起 TFTP

产物解压后是一个目录（`pt1570-r10-<版本>/`），里面就有 `Image.lzma`、`dtb.img`、`rootfs`。
U-Boot 的宏里写死了文件名，**不要改名、不要放进子目录**。

网卡设置：**192.168.1.7 / 255.255.255.0**（U-Boot 侧 `serverip=192.168.1.7`、`ipaddr=192.168.1.2`
是原厂环境里就有的），网线接光猫任意 LAN 口。

```bash
# 在产物目录的上一级执行(仓库里的 tools/tftp_serve.py)
python tools/tftp_serve.py pt1570-r10-<版本>
```

看到 `监听 0.0.0.0:69` 即可。Windows 第一次运行会弹防火墙，选"允许专用网络"。
`69` 端口被占用时换一个端口（`--port 6969`），U-Boot 侧用 `tftpboot 0x5000000 端口@文件` 形式，
不推荐，能空出 69 就空出来。

> 不想用这个脚本：TFTPd64 / Tftpd32 / `tftpd-hpa` 都行，根目录指到产物目录，模式 octet。

## 3. 逐段执行（包里的 `flash-cmds.txt` 就是这些）

### 3.1 网络

```
setenv ipaddr 192.168.1.2
setenv serverip 192.168.1.7
ping ${serverip}
```

期望：`host 192.168.1.7 is alive`。不通就查网线/防火墙/网段（不要接到 WAN 或光口）。

### 3.2 写内核（`ubi_k0`）

```
run ubi_part
run upk
```

`run upk` 展开后是：`ubi check ubi_k0` → `tftpboot 0x5000000 Image.lzma` → `ubi write 0x5000000 ubi_k0 ${filesize}`。

期望输出依次是：

```
Taurus # run upk
Taurus-SoC: 100Mbps full-duplex
TFTP from server 192.168.1.7; our IP address is 192.168.1.2
Filename 'Image.lzma'.
Load address: 0x5000000
Loading: ##################################################  6 MiB
done
Bytes transferred = 6291456 (600000 hex)
Volume "ubi_k0" found
```

**必须看到 `Bytes transferred = 6291456`**（= 0x600000）。不是这个数说明你拿错了文件
（比如刷成了 `kernel.itb`，或文件没补零）。此时 `ubi write` 会写到半个卷，重新 `run upk` 刷一次即可，
不要往下走。

### 3.3 写设备树（`ubi_DTB0`）

```
run upd
```

`upd` 固定写 `0x40000` 字节，所以 `dtb.img` 必须正好 262144 字节（包里的文件已经是）。
期望 `Bytes transferred = 262144 (40000 hex)`。

### 3.4 写根文件系统（`ubi_r0`）

```
run upr
```

期望 `Bytes transferred = <rootfs 实际大小>`（约 15–25 MiB），比 `pfl_rootfs1_sz` 小就行。

### 3.5 固定从 0 号槽启动，换掉启动参数

原厂 `more_args` 是 `ubi.mtd=4 root=31:8 rootfstype=squashfs`（把根当成裸 NAND 分区，
`31:8` 是 `mtdblock8`，也就是 3 号卷 `ubi_r0`）。OpenWrt 的根是 UBI 卷里的 SquashFS，
必须走 `ubiblock`。

**关键：要改的是 `setmoreargs` 宏，不是 `more_args`。** 启动链是
`b1 = setenv ubi_mtd 4; setenv root_mtd 31:8; run process1 setmoreargs setbootargs b`，
其中 `setmoreargs` 会把 `more_args` 整个重建一遍。直接 `setenv more_args ...` 会在
下一次启动时被覆盖掉。

```
setenv sw_commit 0
setenv sw_tryactive 2
setenv setmoreargs 'setenv more_args ubi.mtd=4 ubi.block=0,ubi_r0 root=/dev/ubiblock0_3 rootfstype=squashfs ro tyfza1.slot=0 pt1570.oem=1'
saveenv
printenv setmoreargs sw_commit sw_tryactive
```

要点：
* `root=/dev/ubiblock0_3`：`3` 是本机 `ubi_r0` 的卷 ID。**不是上游的 `0_4`。**
* `setmoreargs` 的整个值要用单引号括起来（里面有空格外加 `setenv` 前缀）。
* `sw_commit=0` 让 `boot_by_commit` 永远走 `b1`（0 号槽），不再在两个槽之间跳。
* `sw_tryactive=2` 让 `bootcmd` 走 `boot_by_commit` 分支，否则会进"试启动"逻辑。
* `tyfza1.slot=0` 是上游存储迁移脚本判断"当前槽"的依据，**不能省**。
* `pt1570.oem=1` 是给系统内升级用的标记，表明"内核是裸 LZMA，不是 FIT"。
* `saveenv` 会同时写 `env` 和 `env2` 两份（原厂环境是冗余的）。

### 3.6 启动

```
run b1
```

期望（关键行）：

```
lzmadec-start
... Uncompressed size: ...
Starting kernel ...
[    0.000000] Booting Linux on physical CPU 0x0000000000
[    0.xxx] ubi0: attaching mtd4
[    0.xxx] ubi0: attached mtd4 (name "ubi_device", size 245 MiB)
[    0.xxx] block ubiblock0_3: created from ubi0:ubi_r0
[    0.xxx] VFS: Mounted root (squashfs filesystem) readonly
```

接着是 OpenWrt 的 preinit 日志，其中会出现存储迁移：

```
TYFZA1 storage: ...
TYFZA1 storage: factory-v1 migration complete
```

**这一段是关键**：它把原厂 `ubi_Config`（身份/校准）和 `ubi_apps` 打包成两份只读副本写入
UBI 12/13 号卷、删掉厂商卷（`ubi_framework1/2`、`ubi_apps`、`ubi_dpi`、`ubi_Config`、
另一槽的 `ubi_k`/`ubi_r`、两个 `ubi_DTB`）、创建 11 号 `rootfs_data` 并写入 UBIFS 种子，
最后通过 `fw_setenv` 把 `sw_commit/sw_active` 固化成 0。
耗时 1–3 分钟（要读 apps 卷做快照），**期间绝对不要断电**。

完成后系统起来，网线口 DHCP 拿地址，浏览器开 **http://192.168.1.1**（首次无密码）。

## 4. 刷完自检

SSH 或串口进系统后逐条确认：

```sh
cat /proc/cmdline                 # 应含 root=/dev/ubiblock0_3 tyfza1.slot=0 pt1570.oem=1
df -h /overlay                    # 应该是 150 MB 以上的 ubifs, 不是 tmpfs
mount | grep overlay              # overlayfs ... upperdir=/overlay/upper
fw_printenv sw_commit sw_active   # 0 / 0
cat /etc/openwrt_release          # DISTRIB_DESCRIPTION 里带 PT1570-R10-CI
ubinfo -a | head -40              # 卷列表: 12/13 是 tyfza1_factory*
```

如果 `df -h /overlay` 显示 `tmpfs`（容量≈内存一半），说明首启没建出 overlay：见
[故障处理](../docs/troubleshooting.md#overlay-是-tmpfs)。

## 5. 之后怎么升级

* **系统内升级（推荐）**：LuCI → 系统 → 备份与更新 → 上传 `sysupgrade.bin`；
  或 SSH：`sysupgrade /tmp/sysupgrade.bin`。本仓库的补丁会让它写裸 LZMA 内核 + 256 KiB 设备树，
  重启后仍是原厂 U-Boot 能启动的布局。

  两点和行为有关：
  * **配置会保留**（overlay 卷不动），所以升级不需要"保留配置"那个勾。
  * **`sysupgrade -n` 不会清配置**：本机升级路径不重建 overlay 卷。要恢复出厂，
    刷完进系统执行 `firstboot && reboot`。
  * 升级会在 `stage2` 里 pivot 到内存根再解挂 `/rom`，所以能安全覆写正在运行的根卷；
    写的是**同一个槽**（两次写同一组卷，不切槽），中途断电需要 U-Boot 重刷。
* **U-Boot 重刷**：重复第 3.2–3.4 步（`sw_commit` / `setmoreargs` 已经是改好的，不用再动环境），
  然后 `run b1`。注意重刷 `rootfs` 会覆盖掉一路升级上来的 `/overlay`，配置请先备份。

> 不要用上游 Releases 里的 `sysupgrade.bin`（FIT 内核），刷进去这台机器起不来。

## 6. 出问题怎么办

先按 [docs/troubleshooting.md](../docs/troubleshooting.md) 对照症状。能进 U-Boot 就几乎不会变砖：

* 启动参数错了 → `setenv more_args <原厂值>` + `saveenv` 即可回到原厂根（原厂根卷 `ubi_r1` 未动时）。
* 想彻底回原厂 → [restore-stock.md](restore-stock.md)。
* 迁移失败进 failsafe → 串口里 `mount_root` 后检查 `dmesg`，或直接恢复原厂分区再重来。
