# fit 目标的补丁

给 U-Boot 2026.07 打包 uImage.FIT 时，产物侧的三个适配（根设备、fitblk 门闩、fit 卷节点）
**不需要改上游源码**，全部在打包时写进 DTB 或由 `.config` 打开，理由见下面第 1–3 节。

唯一的例外是 `0001-spinand-xtx-g02c.patch`：它给内核补上 **XT26G02C（0b:12）** 这颗 flash 的
识别。这不是启动适配，而是本机硬件必需——没有它，内核即使从 FIT 正常启动，也会因为没有
MTD 分区而失去 UBI / overlay / 无线校准 / PON 身份（根一直停在 tmpfs，配置改了就丢，
`iw dev` 为空，无线驱动 probe 报 -12）。`oem` 目标也带同一份补丁（编号 0002）。

## 0. 唯一源码补丁：XT26G02C flash（fit 0001 / oem 0002）

实机证据（2026-11-20，SSH 登录 192.168.1.1）：

```
spi-nand spi0.0: unknown raw ID 0b12001200
spi-nand spi0.0: probe with driver spi-nand failed with error -95
/proc/mtd                       → 空
/sys/class/ubi/                 → 只有 version，无任何 ubi0_*
flash-calibration.bin -> /tmp/tyfza1-factory/...  → /tmp 目录不存在
rtl_ax_ap: probe failed -12 ; iw dev → 空
```

`0x0b` 是 XTX（芯天下），`0x12` 是 XT26G02C（256 MiB）。主线 6.18 的
`drivers/mtd/nand/spi/xtx.c` 只认得 G02D（`0b:32`），没有 G02C。补丁照 XTX C 系列
参考驱动补一条表项（2K/64B/128K/2048 → 256 MiB，`xt26g0xa_ooblayout`，QE 位），
外加 `xt26g01c_ecc_get_status()`（ECC 状态在 C0[7:4]）。读 ID 同时登记
`OPCODE_ADDR` 和 `OPCODE_DUMMY` 两种方式，覆盖实测的 raw ID。

补丁是一级 diff：CI 把 `232-spinand-xtx-g02c.patch` **作为新文件加进上游内核补丁目录**
`port/overlay/target/linux/rtl9617c/patches-6.18/`，OpenWrt 编译时由 build system 自动应用。


## 1. 根设备与 fitblk 门闩 → 打包时改 DTB

内核 6.18 的 `fitblk` 驱动（`target/linux/generic/pending-6.18/510-block-add-uImage.FIT-subimage-block-driver.patch`）
要求 `/chosen` 里同时具备：

| 属性 | 缺了会怎样 |
| --- | --- |
| `u-boot,version` | `fitblk_init()` 直接 `return 0`，不注册 `/dev/fit0`（静默） |
| `rootdisk` phandle | 同上，找不到承载 FIT 的块设备 |
| `bootargs` 含 `root=/dev/fit0 rootwait` | `VFS: Cannot open root device` |

`rootdisk` 指向的节点必须是 `volname = "fit"`，并且挂在 **flash 节点**的 `volumes` 下——
`drivers/mtd/ubi/vmt.c::find_volume_fwnode()` 是从
`vol->dev.parent->parent`（即 spi-nand 的 flash 节点）往下找 `volumes` 的，
挂在分区节点下是找不到的。

这三条由 `tools/pack_fit.py::patch_dtb()` 在打包时注入，`tools/verify_fit.py` 逐条回查。
好处：上游 DTS 怎么改都不影响，也不需要维护 diff 锚点。

## 2. 内核要能识别 FIT 子镜像 → 一个 .config 符号

```
CONFIG_UIMAGE_FIT_BLK=y
```

对应 `drivers/block/Kconfig` 里由上面那个补丁引入的符号。OpenWrt 通用配置默认是
`# CONFIG_UIMAGE_FIT_BLK is not set`，所以必须在 `config/pt1570-r10.config` 里显式打开。

## 3. 用户态要能释放块设备 → 一个包

```
CONFIG_PACKAGE_fitblk=y
```

提供 `/usr/sbin/fitblk`（ioctl `FITBLK_RELEASE`）和 `/lib/upgrade/fit.sh`。
**注意**：本仓库目前只做到"能启动"，系统内 `sysupgrade` 仍走上游的
`nand_do_upgrade`（写 `ubi_k<slot>`），在 2026.07 的 UBI 布局下不适用。
升级请用 U-Boot Web Recovery 重刷 `sysupgrade.itb`，见 `docs/uboot2026.md`。
