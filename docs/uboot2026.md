# 给 U-Boot 2026.07 打包（fit 目标）

这份文档对应 `boot_target=fit`：产物是一个 `sysupgrade.itb`，由机器上现装的
**U-Boot 2026.07** 从 UBI 的 `fit` 卷读出来 `bootm`，根文件系统是 FIT 里的
`type="filesystem"` 子镜像，由内核 `fitblk` 驱动映射成 `/dev/fit0`。

原厂 U-Boot 2020.01 的路径（`Image.lzma`/`dtb.img`/`rootfs` + `upk/upd/upr`）仍然保留，
见 `boot_target=oem` 与 `flash/README.md`。

---

## 1. 为什么不能把老包直接刷进去

老包和 2026.07 是两套自洽但不兼容的契约：

| | 老包（oem） | 2026.07（fit） |
| --- | --- | --- |
| 内核形态 | 裸 LZMA `Image`，补零到 6 MiB | FIT 的 `kernel` 子镜像（LZMA） |
| 设备树 | 单独的 `dtb.img`，补零到 0x40000 | FIT 的 `fdt` 子镜像 |
| 根文件系统 | 单独 `rootfs`，写 `ubi_r0` | FIT 的 `rootfs` 子镜像（4 KiB 对齐） |
| 启动方式 | `run upk/upd/upr` + `run b1` → `lzmadec` + `booti` | `rtk_boot` → `ubi read fit` + `bootm` |
| 根设备 | `/dev/ubiblock0_3`（卷 ID 写死） | `/dev/fit0`（按卷名定位，不依赖 ID） |
| TFTP 网段 | 192.168.1.2 / 192.168.1.7 | 192.168.0.1 / 192.168.0.2 |

2026.07 的 BL33 里 `upk/upd/upr/b1/process1/setmoreargs/sw_tryactive/pfl_kernel1_sz`
**一个都没有**，所以老 `flash-cmds.txt` 粘进去只会 unknown command；反过来它只认 FIT，
非 FIT 直接打印 `not a FIT image: no device tree header at the start`。

证据（从 `pt1570-r10.boot.bin` 里解出来的，见 `uboot/contract-uboot2026.json`
的 `evidence` 段）：

```
BL33 版本 : U-Boot 2026.07-gbb07c993b94f (Nov 16 2026 - 03:48:11 +0000) PT1570-R10
默认环境  : bootcmd=bootmenu / bootmenu_0=Start OpenWrt=rtk_boot
            bootmenu_1=Start Web Recovery=rtk_recovery
            ipaddr=192.168.0.1  serverip=192.168.0.2  loadaddr=0x10000000
字符串    : "boot the system from the firmware volume"
            "UBI has no volume %s" / "No system is installed now; upload a firmware again."
            "not a FIT image: no device tree header at the start"
```

---

## 2. fit 目标改了什么

只动"打包层 + 一个内核开关"，上游源码一行不改（见 `patches/fit/README.md`）。

```
tools/fitimg.py     FDT 解析/序列化 + FIT 构建/解析（纯标准库，不依赖 dtc/mkimage）
tools/pack_fit.py   找产物 -> 改 DTB -> 打 FIT -> 写 manifest/说明/校验和
tools/verify_fit.py 按契约回查 FIT（结构 / 对齐 / hash / DTB 三件套 / 体积）
tools/selftest_fit.py  合成镜像跑通 + 6 个反例（离线，Windows 也能跑）
uboot/contract-uboot2026.json  2026.07 的启动契约
config/pt1570-r10.config       +CONFIG_UIMAGE_FIT_BLK / +CONFIG_PACKAGE_fitblk
```

打包时对 DTB 做的三件事（`pack_fit.py::patch_dtb`）：

1. `/chosen` 补 `u-boot,version = "2026.07"`（fitblk 驱动的门闩，值不参与判断）；
2. `/chosen/bootargs` 追加 `root=/dev/fit0 rootwait rootfstype=squashfs pt1570.oem=1 tyfza1.slot=0`
   （原有的 `earlycon/console/loglevel` 保留，不覆盖）；
3. `/chosen/rootdisk = <phandle>`，phandle 指向新加的
   `<flash>/volumes/volume-fit { volname = "fit"; }`。

第 3 条的位置有讲究：`drivers/mtd/ubi/vmt.c::find_volume_fwnode()` 是从
`vol->dev.parent->parent` 往下找 `volumes` 的，解析下来是 **spi-nand 的 flash 节点**，
不是 `partition@340000`。挂在分区节点下会一直 `-EPROBE_DEFER`，表现为根设备永远不出现。

FIT 排布（`fitimg.Fit.build`）：

```
[ FIT 头(FDT) ][pad→4K][ kernel ][pad→4K][ fdt ][pad→4K][ rootfs ]
```

每个子镜像用 U-Boot 标准的 `data-position`（**从 FIT 起始算的绝对字节偏移**）+
`data-size`（**精确数据长度**）定位，起始位置都是 4 KiB 对齐。kernel/fdt 的
`data-size` 取精确长度（LZMA/DTB 自带结束标记，尾部补零没有意义）；只有 `rootfs`
的 `data-size` 补到 4 KiB——`fitblk` 会**直接跳过**未对齐的 filesystem 子镜像。

> **踩过的坑**：早期版本把偏移写进了自定义属性 `data`（8 字节 `<pos,size>`）。
> U-Boot 的 `fit_image_get_data()` 只认 `data-position` / `data-offset`，找不到就
> 回落到内嵌 `data` 属性——于是它把那 8 字节当成整个内核加载，必然起不来。
> 更糟的是打包器和校验器当时共用这套自定义解析，自测全绿却一直骗着自己。
> 现在 `Fit.parse()` 会**主动拒绝**带 `data` 属性的头，`selftest_fit.py` 有专门的
> `legacy-data` 反例锁住这个回归。上游那个真机验证过的 `.itb` 用的也是
> `data-position` / `data-size`，可以对照。

---

## 3. 刷机

### 3.1 方法 A：U-Boot Web Recovery（推荐）

1. 电脑网卡设 `192.168.0.2/24`，网线接 LAN1；
2. 上电，在 bootmenu 里选 `Start Web Recovery`（或按板子的 reset 组合键进 failsafe）；
3. 浏览器打开 `http://192.168.0.1`，上传包里的 `sysupgrade.itb`；
4. 写入完成后从 bootmenu 选 `Start OpenWrt`。

U-Boot 自己负责建/扩 `fit` 卷，所以不用担心卷大小；它也会在写坏时保留备份
（串口会打印 `Backup of %s ready`）。

### 3.2 方法 B：串口 + TFTP

停在 bootmenu（按 Ctrl-C）后：

```
setenv ipaddr 192.168.0.1
setenv serverip 192.168.0.2
ping ${serverip}
tftpboot ${loadaddr} sysupgrade.itb
ubi part ubi_device
ubi check fit || ubi create fit
ubi write ${loadaddr} fit ${filesize}
ubi check fit
```

卷比镜像小时先删再建（会丢旧固件，确认要换才做）：

```
ubi remove fit
ubi create fit 0x3000000
```

### 3.3 不要做的事

* 不要往 `ubi_k0` / `ubi_r0` / `ubi_DTB0` 写任何东西——2026.07 不读这些卷；
* 不要用老包的 `flash-cmds.txt`；
* 不要写 `boot`(mtd0) / `env`(mtd1)：万一把 boot 写坏就得靠 `mtd0-boot.bin` 救。

---

## 4. 刷完怎么确认

串口应依次看到：

```
U-Boot 2026.07-gbb07c993b94f ...
mapped 1 uImage.FIT filesystem sub-image as /dev/fit0
VFS: Mounted root (squashfs filesystem) readonly on device 254:0.
```

内核里能查到：

```sh
cat /proc/cmdline | tr ' ' '\n' | grep -E 'root=|rootwait|pt1570'
cat /sys/firmware/devicetree/base/chosen/u-boot,version
cat /sys/firmware/devicetree/base/chosen/rootdisk | xxd | head -1
dmesg | grep -E 'fit0|fitblk'
```

---

## 5. 已验证 / 未验证

**已验证（离线，可复现）**：FIT 结构、标准 `data-position`/`data-size` 定位、
子镜像 4 KiB 对齐、hash、DTB 三件套、LZMA 解开是 arm64 `Image`、体积上限，
并按 U-Boot `fit_image_get_data()` 的规则（只走 `data-position`，拒绝内嵌 `data`）
复验过实际生成的 `.itb`。`python tools/selftest_fit.py` 全绿，
并会用 7 个反例证明这些检查真的能拦住问题。

**未验证（需要真机）**：

1. 2026.07 的 `rtk_boot` 是否接受**未签名**的 FIT —— 从 BL33 里没有
   `FIT_SIGNATURE` / RSA 相关字符串看应该不校验，但没在硬件上确认；
2. `fit` 卷的实际可用容量（U-Boot 建卷时按剩余空间算，`limits.fit_bytes` 暂按 64 MiB 卡）；
3. 内核 `fitblk` 在本板 UBI 上的 probe 时序（`rootwait` 已加，理论上够）；
4. 上游首启迁移脚本 `tyfza1-migrate-storage`（preinit）是按**原厂卷布局**写的，
   fit 布局下没有 `ubi_k0/ubi_r0/ubi_apps` 这些卷，它是否安全跳过**没有验证**。
   若首启停在 failsafe，串口进去先看 `ubi info`：
   * 没有 `rootfs_data` → `ubimkvol /dev/ubi0 -N rootfs_data -m` 后重启；
   * 迁移脚本误判退出 → 临时改名 `/etc/preinit.d/*migrate*` 再重启。
5. 系统内 `sysupgrade`：**目前不支持**，仍走上游 `nand_do_upgrade`（写 `ubi_k<slot>`），
   在 2026.07 布局下不适用。升级请走 3.1。要做系统内升级，需要让 `platform.sh`
   改调 `/lib/upgrade/fit.sh` 的 `fit_do_upgrade`（fitblk 包已随固件带进 rootfs）。

---

## 6. 回退

`fit` 与 `oem` 的 UBI 卷布局不同，回退等于换回原厂/老方案：

* 回到原厂固件：用 `pt1570-r10-backup/mtd4-ubi_device.bin` 整盘恢复（`flash/restore-stock.md`）；
* 回到 oem 目标的自编译固件：先按 `flash/restore-stock.md` 恢复 U-Boot 2020.01 + UBI，
  再刷 `boot_target=oem` 的包。

> 只换固件不换 U-Boot 是不行的：2026.07 只认 FIT，2020.01 只认裸 LZMA。
