# fit 目标：没有源码补丁

给 U-Boot 2026.07 打包 uImage.FIT 时，本仓库**不改上游一行代码**。需要改的三个地方
全部落在产物侧，理由如下。

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
