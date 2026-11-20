# 适配补丁（按 bootloader 目标分目录）

```
patches/
  oem/   原厂 U-Boot 2020.01 用：改上游源码，让裸 LZMA 内核 + ubi_r0/ubi_k0 这套能升级
  fit/   U-Boot 2026.07 用：**没有补丁**（原因见 patches/fit/README.md）
```

## 为什么要分

两个目标的差异不在"要不要兼容"，而在**差异落在哪一层**：

| | oem（原厂 2020.01） | fit（2026.07） |
| --- | --- | --- |
| 内核形态 | 裸 LZMA `Image` | uImage.FIT 里的 kernel 子镜像 |
| 根设备 | `/dev/ubiblock0_3`（卷 ID 固定） | `/dev/fit0`（fitblk，按卷名） |
| 需要改上游源码吗 | 要：卷 ID 硬编码、升级路径写死 `ubi_k<slot>` | 不要：全部在打包阶段写进 DTB + 一个 `.config` 开关 |

fit 目标之所以一个补丁都不用打，是因为三件事都能在**产物侧**解决：

1. `root=/dev/fit0 rootwait`、`u-boot,version`、`rootdisk` 三个 DTB 属性 →
   `tools/pack_fit.py::patch_dtb()` 在打包时注入；
2. `volumes/volume-fit { volname = "fit"; }` → 同上；
3. 内核 `fitblk` 驱动 → `config/pt1570-r10.config` 里的 `CONFIG_UIMAGE_FIT_BLK=y`。

这样上游源码保持原样，上游发新版时 fit 目标不会因为补丁锚点失配而红。

## 用哪个

CI 的 `boot_target` 输入决定应用哪个目录（见 `.github/workflows/build.yml` 的「打适配补丁」步骤）。
本地复现：

```bash
git -C ponwrt apply patches/oem/0001-oem-uboot-image-layout.patch      # oem
# fit: 什么都不用打
```

oem 补丁的逐条说明见 [oem/README.md](oem/README.md)。
