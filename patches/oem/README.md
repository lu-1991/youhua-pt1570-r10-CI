# 适配补丁

本仓库只打一个补丁：

```
0001-oem-uboot-image-layout.patch
  port/overlay/target/linux/rtl9617c/base-files/lib/upgrade/platform.sh        # 裸 LZMA 内核 + 升级分支
  port/overlay/target/linux/rtl9617c/base-files/lib/upgrade/tyfza1-storage.sh  # 卷 ID 断言改成按名
  port/packages/tyfza1-storage/files/tyfza1-migrate-storage                    # 首启迁移改成按名
```

改动可以归成两类：**① 内核是裸 LZMA，不是 FIT；② 本机 UBI 卷 ID 和上游不同。**

## ① 内核是裸 LZMA，不是 FIT

上游的 `platform_do_upgrade()` 走 `nand_do_upgrade()`，把 `sysupgrade.bin` 里的 `kernel`
写进 `ubi_k<slot>`。上游的 `kernel` 是签名 FIT——那是给它自己的 U-Boot 1.6 用的。
本机原厂 U-Boot 只会 `lzmadec` + `booti`（见 `docs/compatibility.md` 第 3 节），
FIT 写进去重启就起不来。

补丁在 `platform.sh` 新增两个函数，并在 `platform_do_upgrade()` 里加一个分支：

```sh
if pt1570_oem_boot; then          # /proc/cmdline 里有 pt1570.oem=1
    pt1570_oem_upgrade "$1" "$slot"
    return $?
fi
nand_do_upgrade "$1"              # 上游原逻辑, 未命中标记时行为不变
```

`pt1570_oem_upgrade()` 做的事：

1. 从升级包里解出 `kernel`（裸 LZMA `Image`）、`root`（SquashFS）和可选的 `dtb`；
2. 校验体积（内核 ≤ 6,291,456 B，rootfs ≤ 33,648,640 B，DTB ≤ 262,144 B）；
3. 内核**补零到 6 MiB**、DTB **补零到 256 KiB**——原厂启动脚本按 `pfl_kernel1_sz` /
   `pfl_fdt1_sz` 定长读卷，长度必须正好；
4. `nand_remove_ubiblock` 后依次 `ubiupdatevol` 写 `ubi_DTB<slot>`、`ubi_r<slot>`、
   `ubi_k<slot>`（先根后内核，中途失败仍保留旧内核）；
5. 不动环境变量、不动卷大小、不动 `ubi_Config`；overlay 也不动，所以**升级保留配置**；
6. `sync` + `umount -a` + `reboot -f`——本路径替换了 `nand_do_upgrade()`，重启要自己做。

`platform_check_image()` 没有改：板名白名单（`tianyi,tyfza1`）、`factory-v1` 元数据、
体积检查都对裸内核同样适用。原厂 `nand_do_platform_check` 只校验
`sysupgrade-tianyi_tyfza1/CONTROL` 非空 + tar 完整，我们的包完全符合。

## ② 本机 UBI 卷 ID 和上游不同

卷名一样，ID 不一样（详见 `docs/compatibility.md` 第 2 节）：本机没有 `ubi_fpga`、
多一个 `ubi_dpi`，所以 `ubi_r0` 是 **3**（上游 4）、`ubi_r1` 是 **6**（上游 7），
`ubi_apps` 是 **9**（上游 10）。上游代码里三处硬编码会因此直接失效：

| 位置 | 上游写法 | 后果 | 补丁改法 |
| --- | --- | --- | --- |
| `tyfza1-storage.sh` `tyfza1_plan_storage()` | `case ubi_r0:${ubidev}_4\|ubi_r1:${ubidev}_7` | 系统内升级报 `Unexpected TYFZA1 root volume` | 只比卷名 |
| 同上 `tyfza1_grow_data()` | `[ "$data" = "${ubidev}_11" ]` | 扩容直接 return 1 | 去掉 ID 断言，创建后回读名字确认 |
| `tyfza1-migrate-storage` | `3 + slot*3 : ubi_k` / `4 + slot*3 : ubi_r`、`0:ubi_Config 10:ubi_apps` | **首启迁移判"卷 ID 不符"→ failsafe** | 新增 `vol_id()`，全部按卷名解析 |

补丁里新增的两个小函数（在 `tyfza1-migrate-storage`）：

```sh
vol_id()      # 卷名 -> 卷 ID（扫 /sys/class/ubi/ubi0_*/name）
remove_name() # 按卷名删卷，卷不存在时静默跳过
```

OEM 卷列表也换成按名：`software="ubi_fpga ubi_DTB0 ubi_DTB1 ubi_framework1 ubi_framework2"`
——`ubi_fpga` 和 `ubi_dpi` 各自只存在于一种机型上，**缺哪个就自动跳过**，所以同一份代码
两个机型都能跑，不需要按板名分叉。

`rootfs_data` 仍然固定落在 **11** 号：两种机型都正好 11 个 OEM 卷（0–10），11/12/13/14
天然空着。创建后补了一句断言 `[ "$(name 11)" = rootfs_data ]`，防止将来卷数变化时静默跑偏。

## 为什么不在补丁里建 overlay 卷

上游的 `tyfza1-migrate-storage`（preinit）已经负责建 11 号 `rootfs_data` 并写入 UBIFS 种子。
在 U-Boot 里手工建卷会把卷 ID 建错（U-Boot 2020.01 的 `ubi create` 不能指定 ID），
反而破坏上游脚本"`rootfs_data` 必须是 11 号"的假设。详见 `docs/compatibility.md` 第 5 节。

## 怎么重建这个补丁

补丁是手写的 diff，改起来容易把锚点对错。`tools/rebuild_patch.py` 把全部改动写成一组
"原文 → 替换"的断言，任何一处锚点对不上就报错退出，不会写出半个补丁：

```bash
git clone --depth 1 https://github.com/LeoooChen/PonWRT-TYFZA1 /tmp/ponwrt
cd /tmp/ponwrt && git checkout <PONWRT_REF>
python /path/to/pt1570-r10-CI/tools/rebuild_patch.py   # 三个文件一次改完
for f in port/packages/tyfza1-storage/files/tyfza1-migrate-storage \
         port/overlay/target/linux/rtl9617c/base-files/lib/upgrade/*.sh; do sh -n "$f"; done
git diff > /path/to/pt1570-r10-CI/patches/0001-oem-uboot-image-layout.patch
```

改完记得更新 `config/build.env` 里的 `PONWRT_REF`，并跑一次 `selfcheck`。

## 上游变了、补丁打不上怎么办

CI 的自检任务（`.github/workflows/selfcheck.yml`）和构建的第 5 步都会在
`git apply` 失败时立刻报错，不会浪费时间编译。

重做补丁就用上面 `tools/rebuild_patch.py` 的流程；如果上游改了函数结构导致锚点对不上，
脚本会告诉你哪一处失配，按新的函数体调整锚点即可。

注意保持三条不变式，否则产物会起不来：

* `pt1570.oem=1` 才走裸内核路径，默认行为必须仍然是上游逻辑；
* 写内核前必须补零到 6,291,456 B（DTB 到 262,144 B）；
* 写卷顺序是先 `ubi_r` 再 `ubi_k`，且不修改环境变量；
* 所有 OEM 卷一律按**卷名**定位，不许再出现 `+ slot * 3` 这类算 ID 的写法。
