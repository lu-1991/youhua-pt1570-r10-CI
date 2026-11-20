# PT1570-R10 云编译源码库

给 **友华 PT1570-R10（联通定制 10G-EPON 光猫，Realtek RTL9617C 四核 Cortex-A55）** 用的
OpenWrt 云编译仓库。源码在 GitHub Actions 上**在线拉取**，本地不需要下载 OpenWrt、
不需要 Linux、不需要工具链。

编译产物按目标 bootloader 分两条路径，CI 里用 `boot_target` 选：

| `boot_target` | 产物 | 目标 bootloader | 刷机文档 |
| --- | --- | --- | --- |
| `fit`（默认） | `sysupgrade.itb`（uImage.FIT） | 机器上现装的 **U-Boot 2026.07**（`rtk_boot`，从 UBI 的 `fit` 卷 bootm，根设备 `/dev/fit0`） | [docs/uboot2026.md](docs/uboot2026.md) |
| `oem` | `Image.lzma` + `dtb.img` + `rootfs` | **原厂 U-Boot 2020.01**（`lzmadec` + `booti`，根设备 `/dev/ubiblock0_3`） | [flash/README.md](flash/README.md) |

上游固件工程是 [PonWRT-TYFZA1](https://github.com/LeoooChen/PonWRT-TYFZA1)（天邑 TYFZA1、
同一颗 RTL9617C）。本仓库不复制上游代码，只在编译时克隆它、按目标打补丁或不打补丁、
按对应 bootloader 的启动契约重新打包。

```
本仓库(轻量, 几百 KB)                云端                         你的光猫
┌──────────────────┐   clone   ┌────────────────────┐   FIT    ┌────────────────┐
│ workflow + 补丁   │ ────────► │ OpenWrt + PonWRT   │ ───────► │ U-Boot 2026.07  │
│ 契约 + 打包/校验  │           │ clang-18 / rust     │  fit 卷  │ rtk_boot+bootm  │
└──────────────────┘           └────────────────────┘          └────────────────┘
```

## 为什么这套东西能刷进你这台机器

你的原厂备份和上游工程的分区/卷布局是**逐字节同源**的，不是"看起来差不多"：

| 项目 | 你的机器（`pt1570-r10-backup`） | 上游 PonWRT 假定 | 结论 |
| --- | --- | --- | --- |
| `ubi_device` 分区 | `251648K@0x340000` = 257,687,552 B | `0xf5c0000` | 一致 |
| UBI 内核卷 `ubi_k0` | mtd7 转储 6,348,800 B = 50 LEB | 50 × 126,976 B | 一致 |
| UBI 根卷 `ubi_r0` | mtd8 转储 33,648,640 B = 265 LEB | 265 × 126,976 B | 一致 |
| UBI DTB 卷 `ubi_DTB0` | mtd6 转储 380,928 B = 3 LEB | 3 × 126,976 B | 一致 |
| 卷 ID | `ubi_r0`=4 / `ubi_r1`=7（`root=31:8/31:11`） | 4 / 7 | 一致 |
| U-Boot | 2020.01-svn28407，`lzmadec` + `booti` | —（本仓库适配点） | 见下 |

所以**设备树不用改**：DTS 里的 `boot / env / env2 / static_conf / ubi_device` 五个分区与
`pt1570-r10-backup/mtd1-env.bin` 里的 `mtdparts` 完全相同。

真正的适配点只有一个：**你这台的原厂 U-Boot 不认 FIT**。它从 `ubi_k0` 读一段
LZMA 压缩的裸 arm64 `Image`、解压到 `0x80000`，再用 `booti` 启动；而上游发布的内核是
签名的 FIT（给上游自己的 U-Boot 1.6 用的）。直接把上游固件刷进你的机器，内核解不出来，
机器就停在 U-Boot。本仓库因此把产物改成：

* `Image.lzma` — LZMA-alone（`lc3 lp0 pb2`、8 MiB 字典，与原厂内核参数一致），**补零到 6 MiB**
* `dtb.img` — 设备树，**补零到 0x40000**（原厂 `upd` 宏按固定长度写入）
* `rootfs` — SquashFS，≤ 33,648,640 B（原厂根卷容量）
* `sysupgrade.bin` — 系统内升级包，内核同样是 `Image.lzma`，不是 FIT

补零不是讲究：原厂启动脚本读卷时用的是固定长度（`pfl_kernel1_sz=0x600000`、
`pfl_fdt1_sz=0x40000`），长度对不上就读到垃圾。`tools/verify_bundle.py` 会把这一条
当作硬性检查，编译完自动跑，不通过就红。

## 三步刷机（fit 目标默认）

```bash
# 1) 下载 Actions 产物后, 电脑网卡设 192.168.0.2/24, 接 LAN1
# 2) 上电, bootmenu 里选 Start Web Recovery, 浏览器打开 http://192.168.0.1
# 3) 上传包里的 sysupgrade.itb, 写完从 bootmenu 选 Start OpenWrt
```

串口方式（TFTP + `ubi write ... fit`）见 **[docs/uboot2026.md](docs/uboot2026.md)**。
oem 目标（原厂 U-Boot 2020.01，`run upk/upd/upr` 三件套）见 **[flash/README.md](flash/README.md)**。
恢复原厂见 **[flash/restore-stock.md](flash/restore-stock.md)**。

## 仓库结构

```
.github/workflows/
  build.yml                云编译: 拉上游 -> 打补丁 -> 编译 -> 打包 -> 契约校验 -> Release
  selfcheck.yml            仓库自检: 两个目标的打包/校验都能离线跑通
config/
  build.env                上游提交、运行时文件、默认机型(改这里换版本)
  pt1570-r10.config        .config 覆盖片段(含 CONFIG_UIMAGE_FIT_BLK / CONFIG_PACKAGE_fitblk)
patches/
  oem/0001-oem-uboot-image-layout.patch   oem 专用: 裸 LZMA 内核 + 卷 ID 按名解析
  fit/README.md                           fit 目标不需要补丁, 说明原因
uboot/
  contract.json            原厂 2020.01 的启动/存储契约(来自本机 env 转储)
  contract-uboot2026.json  U-Boot 2026.07 的 FIT 契约(来自 boot.bin 里的 BL33)
  env-pt1570-r10.txt       本机 U-Boot 关键环境变量(人工核对用)
tools/
  fitimg.py                FDT/FIT 读写(纯标准库, 不依赖 dtc/mkimage)
  pack_fit.py              打 uImage.FIT: 注入 DTB 三件套 + 4 KiB 对齐 + manifest
  verify_fit.py            按 FIT 契约校验(fit 目标)
  make_bundle.py           打包入口: --target fit 转发 pack_fit, --target oem 走老路径
  verify_bundle.py         按原厂契约校验(oem 目标)
  parse_env.py             用你的 env 转储重新核对契约
  tftp_serve.py            最小 TFTP 服务端(U-Boot 用)
  selftest.py              oem 离线自检(合成镜像 + 反例)
  selftest_fit.py          fit 离线自检(合成 FIT + 6 个反例)
  rebuild_patch.py         在干净上游树上重建 patches/oem/0001
flash/
  README.md                oem 刷机手册(原厂 U-Boot)
  restore-stock.md         恢复原厂
docs/
  uboot2026.md             fit 目标: 为什么这么做、怎么刷、验证了什么
  compatibility.md         逐字节对拍证据与适配推理
  pipeline.md              云编译流水线说明
  troubleshooting.md       常见故障
```

## 产物说明

### fit 目标（默认）

| 文件 | 用途 |
| --- | --- |
| `sysupgrade.itb` | 整机固件（uImage.FIT）。刷这一个文件就够，对应 UBI 的 `fit` 卷 |
| `flash-cmds.txt` | Web Recovery 步骤 + 串口 TFTP 命令 |
| `manifest.json` | 体积/哈希，以及 DTB 里注入了什么（`dtb_fixup`） |
| `SHA256SUMS` | 校验和 |

FIT 内部：`kernel-1`（LZMA）+ `fdt-1` + `rootfs-1`（SquashFS，4 KiB 对齐），
`config-1` 的 `loadables` 引用 rootfs —— 内核 `fitblk` 靠这条链注册 `/dev/fit0`。

### oem 目标

| 文件 | 用途 | 对应 U-Boot 宏 |
| --- | --- | --- |
| `Image.lzma` | 内核（6 MiB 补零） | `run upk` |
| `dtb.img` | 设备树（0x40000 补零） | `run upd` |
| `rootfs` | 根文件系统 | `run upr` |
| `sysupgrade.bin` | **只用于系统内升级**（LuCI / `sysupgrade`），不能刷到 U-Boot | — |
| `overlay/rootfs_data.ubifs.gz` | 空 UBIFS 种子，首启 overlay 没挂上时手工修复 | — |
| `flash-cmds.txt` | 可直接粘贴的 U-Boot 命令 | — |
| `manifest.json` / `SHA256SUMS` | 版本、体积、校验和 | — |

## CI 校验了什么 / 没校验什么

编译完成后 CI 会硬性校验（任一失败则不出包）：

**公共**：校验和与文件一致；`manifest.json` 与实际产物体积/哈希一致。

**fit 目标**（`tools/verify_fit.py`）：

* FIT 魔数、体积上限、默认配置存在、kernel/fdt/rootfs 三个子镜像齐全
* rootfs 在 `loadables` 里、`type=filesystem`、`data-position` 与 `data-size` 都是 4 KiB 整倍数
* 每个子镜像的 `hash-1`（sha256）与实际数据一致
* kernel 的 LZMA 头（`0x5d` / 8 MiB 字典）、声明长度与实际解压长度一致、解开是 arm64 `Image`
* DTB：`/chosen` 有 `u-boot,version`、`bootargs` 含 `root=/dev/fit0 rootwait rootfstype=squashfs`、
  `rootdisk` phandle 能解析到 `volname="fit"` 且该节点挂在 flash 节点的 `volumes` 下
* `tools/selftest_fit.py` 另有 6 个反例（loadables/对齐/volname/bootargs/u-boot,version/数据被改）

**oem 目标**（`tools/verify_bundle.py`）：

* LZMA 头参数、字典、结束标记；解压后是 arm64 `Image`，体积不覆盖 `0x2000000` 保留区
* `dtb.img` 长度正好 0x40000、FDT 魔数、补零干净、`tianyi,tyfza1` + `realtek,rtl9617c`、含 `ubi_device` 分区
* `rootfs` 是 SquashFS 4.0 且 ≤ 原厂卷容量
* `sysupgrade.bin` 的 `kernel` 与 `Image.lzma` 逐字节一致、带 `tyfza1_storage: factory-v1` 元数据

**没有校验**：真实硬件上的启动。没有这台机器的串口，无法替你验证 PON 注册、LAN 口
顺序、Wi-Fi 校准，也**没有**验证 2026.07 是否接受未签名 FIT、`fit` 卷实际可用容量。
这些都是上游工程在 TYFZA1 上验证过的路径，你的机器与它同源但不保证逐台一致 ——
第一次刷机请务必按 [docs/uboot2026.md](docs/uboot2026.md) / [flash/README.md](flash/README.md)
做好备份并接串口。

## 风险提示（请先读）

1. **会改动 `ubi_device` 里的内核/根卷，首启会删除厂商卷**（`ubi_apps`、`ubi_framework1/2`、
   `ubi_dpi`），并把原厂配置打包成两份只读副本存进 UBI 12/13 号卷。恢复原厂前不要格式化
   `ubi_device`。
2. **绝不写 `boot`(mtd0) / `env`(mtd1) / `ubi_Config`(id 0)**：前两者是你的 U-Boot 和它的
   环境，后者是光猫的身份（MAC/SN）与光学校准、无线校准数据。丢了就只能用 `mtd4` 全盘
   备份恢复。
3. 首启迁移期间**不要断电**。迁移失败会停在 failsafe（可用 LAN + `http://192.168.1.1`
   或串口修复），不会变砖，但需要手动处理。
4. 原厂一键恢复依赖 `pt1570-r10-backup/mtd4-ubi_device.bin`，请先把它另存一份到别处。
5. **fit 目标目前只做到"能启动"**：系统内 `sysupgrade` 仍走上游 `nand_do_upgrade`
   （写 `ubi_k<slot>`），在 2026.07 的 UBI 布局下不适用，升级请用 U-Boot Web Recovery
   重刷 `sysupgrade.itb`。
6. **fit / oem 的固件不能混刷**：2026.07 只认 FIT，2020.01 只认裸 LZMA。换目标等于换
   bootloader + UBI 布局，按 [docs/uboot2026.md](docs/uboot2026.md) 第 6 节回退。

## 常用操作

```bash
# 换上游版本: 改 config/build.env 里的 PONWRT_REF, 推送后手动触发 workflow
# 换机型拓扑: Actions 里选 four_gbe(默认) 或 2p5g
# 本地核对契约(重新 dump 了 env 之后):
python tools/parse_env.py ../pt1570-r10-backup/mtd1-env.bin --contract uboot/contract.json
# 本地校验下载回来的包:
python tools/verify_bundle.py pt1570-r10-<版本>
# 不编译也能验证仓库逻辑:
python tools/selftest.py
```

## 许可

上游 PonWRT-TYFZA1 与 OpenWrt 均为 GPL-2.0；本仓库的补丁与脚本沿用 GPL-2.0。
