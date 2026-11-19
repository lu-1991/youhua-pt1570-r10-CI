# 兼容性依据：为什么这套产物能在这台机器上启动

本仓库所有"魔法数字"都来自你自己的备份，不是猜的。下面把每个结论和它的证据写清楚，
方便你以后换了固件版本、或者换台机器时重新核对。

## 1. 证据来源

| 文件 | 用途 |
| --- | --- |
| `pt1570-r10-backup/mtd0-boot.bin` | 读出 U-Boot 版本：`U-Boot 2020.01-svn28407 (Jan 02 2024) Taurus-SoC` |
| `pt1570-r10-backup/mtd1-env.bin` | 环境变量：分区表、加载地址、启动流程、刷写宏、卷大小上限 |
| `pt1570-r10-backup/mtd4-ubi_device.bin` | 整个 UBI 区，用来核对卷容量与 ID |
| `pt1570-r10-backup/mtd6/7/8/9/10/11/12/13-*.bin` | 各 UBI 卷的裸转储，用来核对每个卷的确切字节数 |
| `pt1570-r10.boot.bin` | 2 MiB，Realtek 打包格式（`0xAA640001` 头），与 mtd0 不是同一形态；刷机不使用 |

`tools/parse_env.py` 会把这些数值和 `uboot/contract.json` 对拍，任何一项不一致就报错：

```bash
python tools/parse_env.py ../pt1570-r10-backup/mtd1-env.bin --contract uboot/contract.json
# → 与 uboot/contract.json 一致: 该 U-Boot 可以按本仓库的刷机流程引导固件
```

## 2. 分区与卷：与上游 TYFZA1 完全同源

环境里的 `mtdparts`：

```
ca_spinand_flash:2048K(boot),256K@0x280000(env),256K@0x2C0000(env2),
                  256K@0x300000(static_conf),251648K@0x340000(ubi_device)
```

上游 `rtl9617c-tyfza1-stock-ubi-readonly.dts` 里写的是同一组偏移和长度（`ubi_device`
`0x340000 0xf5c0000` = 251648K）。UBI 几何：PEB 128 KiB、LEB 126,976 B、共 1966 PEB。

卷大小（`126976 × eraseblocks`）与你的转储逐字节吻合：

| ID | 卷名 | LEB | 字节数 | 作用 |
| --- | --- | --- | --- | --- |
| 0 | `ubi_Config` | 83 | 10,539,008 | 原厂配置（身份/光学校准/无线） |
| 1 | `ubi_DTB0` | 3 | 380,928 | 0 槽设备树 |
| 2 | `ubi_k0` | 50 | 6,348,800 | 0 槽内核 |
| 3 | `ubi_r0` | 265 | 33,648,640 | 0 槽根文件系统 |
| 4 | `ubi_DTB1` | 3 | 380,928 | 1 槽设备树 |
| 5 | `ubi_k1` | 50 | 6,348,800 | 1 槽内核 |
| 6 | `ubi_r1` | 265 | 33,648,640 | 1 槽根文件系统 |
| 7 | `ubi_framework1` | 133 | 16,887,808 | 厂商框架 |
| 8 | `ubi_framework2` | 133 | 16,887,808 | 厂商框架副本 |
| 9 | `ubi_apps` | 876 | 111,230,976 | 厂商应用 |
| 10 | `ubi_dpi` | 83 | 10,539,008 | DPI/流分类库 |

（合计保留 1,944 LEB；其余 22 PEB 是 layout 卷和元数据。）

**这里有一个必须记住的差异：卷名和上游一样，卷 ID 不一样。** 上游 TYFZA1 是
`0 Config / 1 fpga / 2 DTB0 / 3 k0 / 4 r0 / 5 DTB1 / 6 k1 / 7 r1 / 8 fw1 / 9 fw2 / 10 apps`，
本机没有 `ubi_fpga`、多一个 `ubi_dpi`，所以从 `ubi_DTB0` 开始整体前移一位：
`ubi_r0` 是 **3**（不是 4），`ubi_r1` 是 **6**（不是 7）。

两条独立证据互相印证：

1. 直接解析 `mtd4` 转储的 UBI layout 卷（PEB 1774/1775 的 volume table），得到上表；
2. 原厂环境里 `b1` 设 `root_mtd=31:8`、`b2` 设 `root_mtd=31:11`。mtdblock 编号是
   `5 + 卷ID`，所以 `31:8` ⇒ 卷 3，`31:11` ⇒ 卷 6——正好是 `ubi_r0` / `ubi_r1`。
   若按上游的 4/7，原厂该写 `31:9` / `31:12`，与实测不符。

**结论：设备树不用动**（分区表、UBI 几何、每个卷的大小都与上游逐字节一致），
但**卷 ID 相关的代码必须改成按卷名解析**——这正是 `patches/0001` 做的事。

## 3. 启动流程决定了内核必须是"裸 LZMA"

环境里的关键三行：

```
b=booti ${kernel_addr_r} - ${fdt_addr_r}
process1=ubi part ${ubi_device_name}; ubi read ${unzip_src} ubi_k0 ${pfl_kernel1_sz};echo lzmadec-start; lzmadec ${unzip_src} ${kernel_addr_r}; ubi read ${fdt_addr_r} ubi_DTB0 ${pfl_fdt1_sz}
bootcmd=if itest.s ${sw_tryactive} == 2; then run boot_by_commit;else run boot_by_tryactive;fi
```

即：**读 6 MiB → `lzmadec` 解压 → `booti`**。这里没有 `bootm`、没有 FIT、没有签名校验。
上游发布的内核是签名 FIT（给上游 U-Boot 1.6 用的），放进 `ubi_k0` 后 `lzmadec` 根本解不出来。

因此本仓库的产物：

* 内核 = `Image` → LZMA-alone 压缩，参数取上游给原厂内核标定的值
  （`lc=3, lp=0, pb=2, dict=8 MiB`，见 `port/configs/tyfza1-fit-compression.json`），
  与原厂内核的压缩参数一致，Realtek 版 `lzmadec` 能解。
* **补零到 6,291,456 B（0x600000）**：`process1` 按 `pfl_kernel1_sz` 定长读取。
  如果写进去的卷只有 5.2 MB 数据，`ubi read` 读 6 MiB 就会越界/读到旧数据。
  补零后卷的 `used_bytes` 正好等于读取长度，任何读法都不会踩坑。
* DTB **补零到 262,144 B（0x40000）**：`upd` 宏是 `ubi write ${tftp_base} ubi_DTB0 ${pfl_fdt1_sz}`，
  写的是固定长度，不是 `${filesize}`。
* 校验脚本会把"长度必须等于固定值"当硬性检查，不只是"不超过"。

内核装入地址 `0x80000` 是原厂环境给的。这也解释了一个体积上限：DTS 里
`voip_buffer@0x2000000` 之类的保留区从 32 MiB 开始，所以**解压后的内核必须小于
`0x2000000 - 0x80000 = 33,030,144 B`**（`limits.kernel_raw_bytes_max`）。
6 MiB 的 LZMA 大约对应 27–30 MB 的 `Image`，正常够用；一旦超了，`verify_bundle.py
--strict-raw` 会直接失败。

## 4. 启动参数必须改成 ubiblock

原厂 `more_args` 是 `ubi.mtd=4 root=31:8 rootfstype=squashfs`——它把根当成 UBI 区里
**固定偏移的裸分区**（原厂内核的 DTS 里确实这么切）。OpenWrt 的根是 UBI 卷里的 SquashFS，
必须用 `ubiblock` 把它映射成块设备：

```
ubi.mtd=4 ubi.block=0,ubi_r0 root=/dev/ubiblock0_3 rootfstype=squashfs ro tyfza1.slot=0 pt1570.oem=1
```

`ubiblock0_3` 里的 `3` 就是本机 `ubi_r0` 的卷 ID（换槽是 `ubi_r1` → `ubiblock0_6`）。
注意它和上游不同：上游 TYFZA1 是 `ubi_r0=4` / `ubi_r1=7`，因为上游多了个 `ubi_fpga` 卷、
没有 `ubi_dpi`，槽位错开一位。**照抄上游的 `0_4` 会挂不上根。**

更要紧的是：`b1 = ... run process1 setmoreargs setbootargs b` —— 每次启动都会执行
`setmoreargs` 把 `more_args` 整个重建一遍。所以**改 `more_args` 是无效的**，必须改
`setmoreargs` 这个宏本身（`flash-cmds.txt` 里就是这么干的）。

另外两个参数是给 OpenWrt 的脚本看的：

* `tyfza1.slot=0` — 上游存储迁移/升级脚本据此判断当前槽位（缺了会拒绝升级）。
* `pt1570.oem=1` — 本仓库补丁的标记，表示"内核是裸 LZMA，别按 FIT 写"。

## 5. 首启迁移不需要在 U-Boot 里做手术

上游的存储迁移（`tyfza1-migrate-storage`，preinit 阶段、`mount_root` 之前运行）本来
就是为"原厂布局 + 空 overlay"设计的，与我们的刷机方式正好对接：

1. 把 `ubi_Config`（身份/光学校准/无线校准）和 `ubi_apps` 打成两份只读副本，写入 UBI 12/13 号卷；
2. 删除厂商卷（`ubi_framework1/2`、`ubi_apps`、`ubi_dpi`、`ubi_Config`、另一槽的 `ubi_k`/`ubi_r`、
   两个 `ubi_DTB`），腾出 1,629 LEB ≈ 198 MiB；
3. 创建 11 号 `rootfs_data`（`-m` 撑满剩余空间）并写入预格式化的 UBIFS 种子；
4. `fw_setenv sw_commit/sw_active`，然后正常挂 overlay 启动。

所以刷机动作只有"写 3 个卷 + 改 3 个环境变量"，不需要在 U-Boot 里 `ubi remove` 一堆卷
（U-Boot 2020.01 的 `ubi create` 不能指定卷 ID，很容易把 11 号卷建到别的 ID 上，反而破坏
上游脚本的假设）。

注意本机的卷名和上游**同名但不同 ID**，而 ID 11/12/13/14 恰好两边都空着——因为两种机型
都正好是 11 个 OEM 卷（0–10）。补丁里所有 OEM 卷一律按**卷名**解析（`vol_id()`），
`rootfs_data` 固定落在 11 号，并在创建后断言 `name 11 = rootfs_data`。

如果迁移失败，preinit 会进 failsafe 而不是变砖：可以串口或 LAN 进 failsafe 处理，
最后一次手段是 [恢复原厂](../flash/restore-stock.md)。

## 6. 机型拓扑：默认四口千兆

PT1570-R10 是四口千兆（无 2.5G 口），对应 DTS `rtl9617c-tyfza1-ubi.dts`（上游文档里的
`conf-1`，四个内部 GbE PHY）。带 2.5G 口的同门机型才用 `-ubi-2p5g.dts`。

工作流里 `variant` 选 `2p5g` 就会改用 `rtl9617c-tyfza1-ubi-2p5g.dtb`。选错的后果是
LAN 口/Wi-Fi 灯不对、可能有个口不通，**系统仍能起来**，改回来重刷即可。
