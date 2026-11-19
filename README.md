# PT1570-R10 云编译源码库

给 **友华 PT1570-R10（联通定制 10G-EPON 光猫，Realtek RTL9617C 四核 Cortex-A55）** 用的
OpenWrt 云编译仓库。源码在 GitHub Actions 上**在线拉取**，本地不需要下载 OpenWrt、
不需要 Linux、不需要工具链；编译产物是能直接用**机器现有原厂 U-Boot** 刷进去的固件。

上游固件工程是 [PonWRT-TYFZA1](https://github.com/LeoooChen/PonWRT-TYFZA1)（天邑 TYFZA1、
同一颗 RTL9617C）。本仓库不复制上游代码，只在编译时克隆它、打一个适配补丁、按本机
U-Boot 的启动契约重新打包。

```
本仓库(轻量, 几百 KB)                云端                         你的光猫
┌──────────────────┐   clone   ┌────────────────────┐   TFTP   ┌────────────┐
│ workflow + 补丁   │ ────────► │ OpenWrt + PonWRT   │ ───────► │ 原厂 U-Boot │
│ 契约 + 打包/校验  │           │ clang-18 / rust     │  刷 3 个卷 │ lzmadec+booti│
└──────────────────┘           └────────────────────┘           └────────────┘
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

## 三步刷机

```bash
# 1) 下载 Actions 产物后, 进入包目录并起 TFTP(电脑网卡设 192.168.1.7/24, 接 LAN 口)
python tools/tftp_serve.py pt1570-r10-<版本>

# 2) 串口进 U-Boot, 按包里的 flash-cmds.txt 逐段粘贴
#    run ubi_part -> run upk -> run upd -> run upr -> setenv more_args ... -> run b1

# 3) 首启自动迁移原厂数据(1-3 分钟), 之后浏览器开 http://192.168.1.1
```

完整步骤、期望回显、失败恢复见 **[flash/README.md](flash/README.md)**。
恢复原厂见 **[flash/restore-stock.md](flash/restore-stock.md)**。

## 仓库结构

```
.github/workflows/
  build.yml                云编译: 拉上游 -> 打补丁 -> 编译 -> 打包 -> 契约校验 -> Release
  selfcheck.yml            仓库自检: 合成镜像跑通打包/校验, 补丁是否还能应用
config/
  build.env                上游提交、运行时文件、默认机型(改这里换版本)
  pt1570-r10.config        .config 覆盖片段(追加到上游最小配置之后)
patches/
  0001-oem-uboot-image-layout.patch   唯一补丁: 裸 LZMA 内核 + 卷 ID 按名解析(3 个文件)
uboot/
  contract.json            机器可读的启动/存储契约(全部来自本机 env 转储)
  env-pt1570-r10.txt       本机 U-Boot 关键环境变量(人工核对用)
tools/
  parse_env.py             用你的 env 转储重新核对契约
  verify_bundle.py         按契约校验刷机包(CI 与本地同一份逻辑)
  make_bundle.py           CI 内打包(本地跑需要已编译的 OpenWrt 树)
  tftp_serve.py            最小 TFTP 服务端(U-Boot 用)
  selftest.py              离线自检(合成镜像 + 反例)
  rebuild_patch.py         在干净上游树上重建 patches/0001(改补丁后用)
flash/
  README.md                刷机手册
  restore-stock.md         恢复原厂
docs/
  compatibility.md         逐字节对拍证据与适配推理
  pipeline.md              云编译流水线说明
  troubleshooting.md       常见故障
```

## 产物说明

包里每个文件都有用，不要只拿 `sysupgrade.bin`：

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

* LZMA 头参数、字典、结束标记；解压后是 arm64 `Image`，体积不覆盖 `0x2000000` 保留区
* `dtb.img` 长度正好 0x40000、FDT 魔数、补零干净、`tianyi,tyfza1` + `realtek,rtl9617c`、含 `ubi_device` 分区
* `rootfs` 是 SquashFS 4.0 且 ≤ 原厂卷容量
* `sysupgrade.bin` 的 `kernel` 与 `Image.lzma` 逐字节一致、带 `tyfza1_storage: factory-v1` 元数据
* 校验和与文件一致

**没有校验**：真实硬件上的启动。我没有这台机器的串口，无法替你验证 PON 注册、LAN 口
顺序、Wi-Fi 校准。这些都是上游工程在 TYFZA1 上验证过的路径，你的机器与它同源但不保证
逐台一致 —— 第一次刷机请务必按 [flash/README.md](flash/README.md) 做好备份并接串口。

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
