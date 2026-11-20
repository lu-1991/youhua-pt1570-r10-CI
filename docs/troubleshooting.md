# 故障处理

原则：**只要能停在 U-Boot，就不会变砖。** 所有改动都能从 U-Boot 改回来，最坏情况用
[恢复原厂](../flash/restore-stock.md) 整块回写 `ubi_device`。

## 刷写阶段

### `run upk` 报 `Volume "ubi_k0" not found` / `ubi check` 失败

UBI 还没挂载。先 `run ubi_part`，再 `ubi info` 确认 11 个卷都在。

### TFTP 一直 `T T T T` 或 `Retry count exceeded`

* 网段不对：U-Boot 里 `printenv ipaddr serverip netmask`，应为 `192.168.1.2 / 192.168.1.7 / 255.255.255.0`。
* 防火墙拦了：Windows 允许 python 走专用网络；换 69 → 6969 试。
* 网线接错口：接 LAN1–LAN4，别接光口；有的机器只有第一个口在 U-Boot 里通。
* 文件名不对：`Image.lzma` / `dtb.img` / `rootfs` 必须原名、平铺在 TFTP 根目录。

### `Bytes transferred` 不是 6291456

拿错文件了（多半是把上游的 `kernel.itb` 或未补零的内核放进去了）。用包里的
`Image.lzma`，本地先跑一次校验：

```bash
python tools/verify_bundle.py pt1570-r10-<版本>
```

### `nand write` / `ubi write` 报错或进度条卡住

先 `ubi info` 看卷有没有被上一次失败写坏；重跑对应宏即可（`ubi write` 是覆盖写，
可以重复执行）。

## 启动阶段

### 卡在 `lzmadec-start` 不动，或 `LZMA error`

内核不是 LZMA-alone，或参数不对（例如用了 xz 容器、或字典不是 8 MiB）。
`verify_bundle.py` 的前几项检查就是抓这个。

### `booti: Bad Linux ARM64 Image magic!`

解压出来的不是 arm64 `Image`。同理：文件不对，重刷 `Image.lzma`。

### 内核起来了，但 `VFS: Cannot open root device "ubiblock0_3"`

两种可能，先分清：

**a) 参数没生效。** `printenv setmoreargs` 应该正好是：

```
setenv more_args ubi.mtd=4 ubi.block=0,ubi_r0 root=/dev/ubiblock0_3 rootfstype=squashfs ro tyfza1.slot=0 pt1570.oem=1
```

如果你只改了 `more_args`，那条改动在下次启动时会被 `setmoreargs` 覆盖——必须改这个宏本身。

**b) 编号写成了上游的 `0_4`。** 本机 `ubi_r0` 是 **3 号卷**（上游是 4 号，因为上游有
`ubi_fpga` 而本机有 `ubi_dpi`）。`ubiblock0_N` 里的 `N` 就是卷 ID，所以本机是 `0_3`。
串口里 `ubi0:` 的挂载日志会打印实际创建的设备名，对照一下就知道是哪种。

少了 `ubi.block=0,ubi_r0`，内核不会创建 `ubiblock0_3`；少了 `rootfstype=squashfs`，
会去探测别的文件系统。改完 `saveenv`。

### `Kernel panic - not syncing: VFS: Unable to mount root fs`

同上；另外确认 `rootfs` 写的是 `ubi_r0`（`run upr`），不是别的槽。

### 启动到一半卡住、或者反复重启

* 原厂 `en_wdt` 会开 60 秒看门狗，只在 `boot_by_tryactive` 分支里；正常 `b1` 路径不开。
* 若 `sw_tryactive != 2`，`bootcmd` 会走"试启动"分支并在失败时切槽。检查
  `printenv sw_tryactive` 应为 `2`，`sw_commit` 应为 `0`。

### 迁移阶段失败进 failsafe

串口会打印 `TYFZA1 storage migration failed ... entering failsafe`。常见原因：

| 原因 | 判断 | 处理 |
| --- | --- | --- |
| 空间不足 | 日志里 `Insufficient temporary space` | `ubi info` 确认厂商卷是否还在（`ubi_apps` 876 LEB 是主要来源）；若被删过，只能恢复原厂重来 |
| `ubi_Config` 不是 UBIFS | `mount -t ubifs` 失败 | 说明之前动过 0 号卷，用 `mtd4` 备份回写 |
| `fw_setenv` 失败 | 日志里 `Boot field ... did not persist` | `fw_env.config` 应指向 `/dev/mtd1`、`/dev/mtd2`；`cat /proc/mtd` 核对编号 |

failsafe 里可以用 `mount_root` 挂根、`dmesg` 看细节；不熟就直接恢复原厂。

## 运行阶段

### overlay 是 tmpfs

`df -h /overlay` 显示 `tmpfs`（约内存一半）说明首启没建出 11 号 `rootfs_data`。
两种修法：

1. **系统内**（能 SSH）：确认 11 号卷存在并已格式化：

   ```sh
   ubinfo -a | grep -A3 rootfs_data
   ubirmvol /dev/ubi0 -N rootfs_data      # 仅在卷是空的/坏的时
   ubimkvol /dev/ubi0 -N rootfs_data -m
   # 写入预格式化 UBIFS 种子(包里的 overlay/rootfs_data.ubifs.gz)
   gzip -dc rootfs_data.ubifs.gz > /tmp/seed.ubifs
   ubiupdatevol /dev/ubi0_11 /tmp/seed.ubifs
   reboot
   ```

2. **U-Boot 里**（更稳）：`ubi remove rootfs_data` 之后 `ubi create rootfs_data 0x6000000`，
   再按上面的方式写种子。U-Boot 2020.01 的 `ubi create` **不能指定卷 ID**，只能拿第一个空闲 ID；
   11 号卷被删掉后一般正好回收 11，但还是要在系统里用 `ubinfo` 确认 `rootfs_data` 就在 11 号。

### LAN 口不对 / 某个口不通 / 2.5G 口不亮

机型拓扑选错。四口千兆用 `four_gbe`（默认），带 2.5G 的机器才用 `2p5g`。
重新编译对应 `variant`，只刷 `dtb.img`（`run upd`）+ 重启即可，不用动内核和 rootfs。

### Wi-Fi 不出来 / 没有校准数据

原厂无线校准在 `ubi_Config` 里，首启迁移会把它打包进 12/13 号卷。检查：

```sh
ubinfo -a | grep -E 'tyfza1_factory|ubi_Config'
ls /tmp/log/tyfza1-factory.log
```

`tyfza1_factory` 缺失说明迁移没成功（看上一节）。若确认 `ubi_Config` 已在迁移中被删，
从 `mtd4` 备份里取回。

### PON 不注册

`网络 → PON 管理` 里选线路模式（这台是 10G-EPON），填运营商给的 LOID/密码。
MAC/SN 留空会自动用原厂值（来自 `ubi_Config` → `ELAN_MAC_ADDR` / `GPON_SN`）。
先用原厂超密确认过 SN/LOID 再排查驱动问题。

### 想看内核日志但没串口

LuCI → 状态 → 内核日志；或 SSH `dmesg | grep -iE 'ubi|squashfs|mtk|rtl'`。
`/proc/cmdline` 是最快的"我到底按什么参数启动的"检查点。

## 系统内升级失败

`sysupgrade` 在写卷之前会跑 `platform_check_image`（板名、`factory-v1` 元数据、体积）。
失败信息会说明是哪一项：

* `This unit requires factory-v1 firmware` → 用了上游 Release 的包（FIT），换本仓库的 `sysupgrade.bin`。
* `Kernel image is ... bytes; the stock loader reads 6291456` → 包的内核超过 6 MiB。
* `Unsupported TYFZA1 board` → 板名不是 `tianyi,tyfza1`，说明刷的是别的机型固件。

升级写卷失败不影响当前系统（先写 rootfs 再写内核，且失败会保留旧内核），重启后仍是旧版本。

## fit 目标（U-Boot 2026.07 / uImage.FIT）

### 刷完停在 bootmenu，选 Start OpenWrt 又回菜单

`fit` 卷里没有可启动的 FIT，或写入被截断。串口会先打印
`UBI has no volume fit` / `No system is installed now; upload a firmware again.` /
`not a FIT image: no device tree header at the start`。重新用 Web Recovery 上传，
或 `ubi check fit` 看一眼卷长度是否等于包大小。

### `VFS: Cannot open root device "fit0"`

内核没注册 `/dev/fit0`，三种原因按概率排：

1. **FIT 里的 rootfs 子镜像没 4 KiB 对齐** —— `fitblk` 会静默跳过。用
   `python tools/verify_fit.py <包目录>` 复查（这条是硬检查）。
2. **DTB 缺 `u-boot,version` 或 `rootdisk`** —— `fitblk_init()` 直接 return 0，不报错。
3. **`CONFIG_UIMAGE_FIT_BLK` 没打开** —— 这次编译的 `.config` 没带上，查
   `zcat /proc/config.gz | grep UIMAGE_FIT_BLK`。

### 首启停在 failsafe，`ubi info` 里没有 `rootfs_data`

上游的 `tyfza1-migrate-storage`（preinit）是按原厂卷布局写的，fit 布局下没有
`ubi_k0/ubi_r0/ubi_apps` 这些卷。串口里确认后手动补一个 overlay 卷：

```sh
ubi info                                    # 看现有卷
ubimkvol /dev/ubi0 -N rootfs_data -m        # 用剩余空间建卷
reboot
```

若脚本误判后直接退出（日志里有 `Unexpected TYFZA1`），临时把
`/etc/preinit.d/*migrate*` 改名再重启，能进系统后再决定去留。

### 启动进了系统，但升级提示板名/格式不对

fit 目标的系统内 `sysupgrade` 还没接上（上游 `platform.sh` 仍写 `ubi_k<slot>`）。
升级请用 U-Boot Web Recovery 重刷 `sysupgrade.itb`，见
[uboot2026.md](uboot2026.md) 第 3.1 节。

### 想知道"到底是不是按 FIT 启动的"

```sh
cat /proc/cmdline | tr ' ' '\n' | grep -E 'root=|rootwait'
dmesg | grep -E 'fit0|fitblk'      # 期望: mapped 1 uImage.FIT ... as /dev/fit0
cat /sys/firmware/devicetree/base/chosen/u-boot,version
```
