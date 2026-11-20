#!/usr/bin/env python3
"""把 OpenWrt 产物打成 U-Boot 2026.07 能直接启动的 uImage.FIT。

和 make_bundle.py(原厂 U-Boot / 裸 LZMA)是**两条并行的打包路径**:
    make_bundle.py --target oem     -> Image.lzma + dtb.img + rootfs (原厂 2020.01)
    pack_fit.py                     -> sysupgrade.itb          (U-Boot 2026.07)

U-Boot 2026.07 的行为(从 pt1570-r10.boot.bin 里解出来的 BL33 实证):
    bootcmd=bootmenu -> bootmenu_0=Start OpenWrt=rtk_boot
    rtk_boot = ubi part ubi_device; ubi read <fit 卷>; bootm
    只认 FIT；非 FIT 直接报 "not a FIT image: no device tree header at the start"
    没有 upk/upd/upr/b1/process1/setmoreargs 这些原厂宏。

内核侧要能挂根, 还差三件事, 全部在**设备树**里做, 不需要改内核源码:
    /chosen/bootargs  含 root=/dev/fit0 rootwait rootfstype=squashfs
    /chosen/u-boot,version         fitblk 驱动的门闩(有它才会 init)
    /chosen/rootdisk    指向 flash 节点下 volumes/volume-fit (volname="fit")
内核 6.18 里 fitblk 驱动 (generic/pending-6.18/510-*) 会:
    1. 用 rootdisk phandle 找到承载 FIT 的块设备(这里就是 UBI 的 fit 卷);
    2. 读 FIT 的 config.loadables, 把 type="filesystem" 的子镜像映射成 /dev/fit0。
所以 rootfs 子镜像必须 4 KiB 对齐 —— Fit.build() 已经保证。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fitimg import Dtb, Fit, FdtError, image_check, kernel_check  # noqa: E402

FIT_NAME = "sysupgrade.itb"


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_one(patterns: list[str], root: Path, what: str) -> Path:
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(root.glob(pattern)))
    if not found:
        raise SystemExit("找不到%s, 尝试过: %s" % (what, ", ".join(patterns)))
    return max(found, key=lambda p: p.stat().st_mtime)


def lzma_alone(raw: bytes, params: dict) -> bytes:
    """LZMA-alone, 并把头部里的"未压缩长度"写成真实值。

    Python 的 FORMAT_ALONE 默认写 0xFFFFFFFFFFFFFFFF(未知), U-Boot 的
    lzma 解压虽然能处理未知长度, 但写死真实长度更稳, 也方便事后校验。
    """
    blob = lzma.compress(
        raw,
        format=lzma.FORMAT_ALONE,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": params["dictionary_bytes"],
            "lc": params["lc"],
            "lp": params["lp"],
            "pb": params["pb"],
        }],
    )
    if blob[0] != 0x5D:
        raise SystemExit("LZMA 属性字节异常: 0x%02x" % blob[0])
    return blob[:5] + struct.pack("<Q", len(raw)) + blob[13:]


def _find_flash_node(dtb: Dtb):
    """找承载 partitions 的 flash 节点 —— UBI 的 volumes 子节点必须挂在它下面。

    Linux 侧依据 (drivers/mtd/ubi/vmt.c: find_volume_fwnode):
        device_get_named_child_node(vol->dev.parent->parent, "volumes")
    vol->dev.parent->parent 解析下来就是 spi-nand 的 flash 节点(mtd 主设备的 of_node),
    而不是某个分区。
    """
    candidates = [n for n in dtb.root.walk() if n.child("partitions") is not None]
    if not candidates:
        # 少数 DTS 把分区直接挂在 flash 下, 没有 partitions 容器: 用 ubi_device 分区反推
        for node in dtb.root.walk():
            if node.name.split("@")[0] != "partition":
                continue
            label = node.get_str("label") or ""
            reg = node.get("reg") or b""
            if label == "ubi_device" or (len(reg) >= 4 and
                                         struct.unpack_from(">I", reg)[0] == 0x340000):
                if node.parent is not None and node.parent.parent is not None:
                    return node.parent.parent
        raise SystemExit("设备树里找不到带 partitions 的 flash 节点")
    for node in candidates:
        compatible = (node.get_str("compatible") or "")
        if "nand" in compatible or "spi" in compatible:
            return node
    return candidates[0]


def patch_dtb(blob: bytes, contract: dict) -> tuple[bytes, dict]:
    """注入 /chosen 三件套 + fit 卷节点, 返回 (新 DTB, 概要)。"""
    boot = contract["bootloader"]
    dtb = Dtb.parse(blob)
    info: dict = {}

    chosen = dtb.ensure("chosen")
    chosen.set_str("u-boot,version", boot["version"])
    info["u_boot_version"] = boot["version"]

    args = (chosen.get_str("bootargs") or "").strip()
    tokens = args.split()
    added = []
    for token in boot["bootargs_required"]:
        if token not in tokens:
            tokens.append(token)
            added.append(token)
    chosen.set_str("bootargs", " ".join(tokens))
    info["bootargs"] = " ".join(tokens)
    info["bootargs_added"] = added

    flash = _find_flash_node(dtb)
    info["flash_node"] = flash.path()
    volumes = flash.child("volumes")
    if volumes is None:
        volumes = flash.add_child("volumes")
    vol = volumes.child("volume-fit")
    if vol is None:
        vol = volumes.add_child("volume-fit")
    vol.set_str("volname", boot["fit_volume"])

    phandle = vol.get_u32("phandle")
    if not phandle:
        phandle = dtb.next_phandle()
        vol.set_u32("phandle", phandle)
    chosen.set_u32("rootdisk", phandle)
    info["rootdisk_phandle"] = phandle
    info["fit_volume_node"] = vol.path()

    return dtb.serialize(), info


def write_flash_commands(out: Path, contract: dict, manifest: dict) -> None:
    net = contract["network"]
    vol = contract["bootloader"]["fit_volume"]
    # 建卷时按镜像大小留 8 MiB 余量, 4 MiB 对齐 —— 不指定大小的话 U-Boot 只能靠默认值,
    # 在卷已经存在但偏小的情况下 `ubi write` 会直接失败。
    create_size = ((manifest["fit"]["bytes"] + 8 * 1024 * 1024 + 0x3FFFFF) // 0x400000) * 0x400000
    lines = [
        "# PT1570-R10 刷机命令 —— U-Boot %s + uImage.FIT" % contract["bootloader"]["version"],
        "# 与 make_bundle.py(oem 目标)的流程完全不同: 不再写 Image.lzma/dtb.img/rootfs,",
        "# 只把 sysupgrade.itb 写进 UBI 的 %s 卷, 由 rtk_boot 直接 bootm。" % vol,
        "",
        "# --- 方法 A(推荐): U-Boot Web Recovery ---",
        "# 1) 断电, 电脑网卡设 %s/24, 网线接 LAN1" % net["host_ipaddr"],
        "# 2) 按住 reset 上电(或 bootmenu 里选 Start Web Recovery), 串口会打印服务地址",
        "# 3) 浏览器打开 %s , 上传 sysupgrade.itb, 等待写入完成" % net["recovery_url"],
        "",
        "# --- 方法 B: 串口 + TFTP ---",
        "# 停在上电后的 bootmenu(按 Ctrl-C), 逐段执行:",
        "setenv ipaddr %s" % net["uboot_ipaddr"],
        "setenv serverip %s" % net["uboot_serverip"],
        "ping ${serverip}",
        "tftpboot ${loadaddr} sysupgrade.itb",
        "ubi part %s" % contract["bootloader"]["ubi_part"],
        "ubi check %s || ubi create %s 0x%x" % (vol, vol, create_size),
        "# 卷已存在但比镜像小(报 no space)时, 先删再建 —— 会丢掉旧固件:",
        "# ubi remove %s && ubi create %s 0x%x" % (vol, vol, create_size),
        "ubi write ${loadaddr} %s ${filesize}" % vol,
        "ubi check %s" % vol,
        "# 回到 bootmenu 选 Start OpenWrt(或 reset)",
        "",
        "# 注意: 不要再往 ubi_k0/ubi_r0/ubi_DTB0 写任何东西 —— 2026.07 不读这些卷。",
    ]
    (out / "flash-cmds.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_readme(out: Path, contract: dict, manifest: dict) -> None:
    text = """PT1570-R10 OpenWrt 固件 (U-Boot {uboot} / uImage.FIT)
========================================================

版本      : {version}
机型      : {device} / {variant}
上游源码  : {ponwrt}
编译提交  : {openwrt}
生成时间  : {built}

与老包的区别
------------
老包给原厂 U-Boot 2020.01 用: Image.lzma + dtb.img + rootfs 三件套, 靠 upk/upd/upr。
本包给 U-Boot {uboot} 用: 只有一个 uImage.FIT, U-Boot 从 UBI 的 {vol} 卷读出来 bootm,
根文件系统是 FIT 里的 type=filesystem 子镜像, 由内核 fitblk 驱动映射成 {rootdev}。

文件
----
{fit}   整机固件(FIT), 刷这一个文件就够。对应 U-Boot 的 {vol} 卷
manifest.json  体积/哈希/注入的 DTB 属性
SHA256SUMS     校验和
flash-cmds.txt 刷机命令

体积
----
FIT 总大小 {fit_bytes} / 上限 {fit_max} 字节
  内核 {kernel_bytes} 字节(LZMA)
  设备树 {fdt_bytes} 字节
  rootfs {rootfs_bytes} 字节(SquashFS, 4 KiB 对齐)

刷机
----
推荐 U-Boot Web Recovery: 电脑 {host}/24, 浏览器打开 {recovery} 上传本包里的 {fit}。
串口方式见 flash-cmds.txt。

注意
----
* 本包**不含**原厂 U-Boot 的环境宏, 不要再用 run upk / run upd / run upr / run b1。
* rootfs 子镜像的 4 KiB 对齐不是讲究: 内核 fitblk 会跳过未对齐的子镜像, 表现为
  "VFS: Cannot open root device"。
* 刷机前请确认串口横幅就是 U-Boot {uboot}; 若仍是 2020.01-svn28407, 请改用
  make_bundle.py --target oem 产出的老包。
""".format(
        uboot=contract["bootloader"]["version"],
        version=manifest["version"],
        device=contract["device"]["model"],
        variant={"four_gbe": "四口千兆(four_gbe)", "2p5g": "带 2.5G 口(2p5g)"}.get(
            manifest["variant"], manifest["variant"]),
        ponwrt=manifest["sources"]["ponwrt"],
        openwrt=manifest["sources"]["openwrt"][:12],
        built=manifest["built"],
        vol=contract["bootloader"]["fit_volume"],
        rootdev=contract["bootloader"]["root_device"],
        fit=manifest["artifacts"]["fit"],
        fit_bytes=manifest["fit"]["bytes"],
        fit_max=contract["limits"]["fit_bytes"],
        kernel_bytes=manifest["fit"]["kernel"]["bytes"],
        fdt_bytes=manifest["fit"]["fdt"]["bytes"],
        rootfs_bytes=manifest["fit"]["rootfs"]["bytes"],
        host=contract["network"]["host_ipaddr"],
        recovery=contract["network"]["recovery_url"],
    )
    (out / "README.txt").write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", type=Path, required=True, help="OpenWrt 源码树 (sources/openwrt)")
    parser.add_argument("--port", type=Path, required=True, help="PonWRT-TYFZA1 克隆目录")
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--contract", type=Path, default=Path("uboot/contract-uboot2026.json"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--variant", choices=("four_gbe", "2p5g"), default="four_gbe")
    parser.add_argument("--ponwrt-ref", default="")
    parser.add_argument("--kernel-compression", choices=("lzma", "none"), default=None,
                        help="默认按契约(fit.kernel_compression)")
    args = parser.parse_args(argv)

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    tree = args.tree.resolve()
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    bin_dir = tree / "bin/targets/rtl9617c/generic"
    rootfs_src = find_one(["*-squashfs-rootfs.squashfs"], bin_dir, "rootfs 镜像")
    prefix = rootfs_src.name[: -len("-squashfs-rootfs.squashfs")]
    dtb_name = "ubi.dtb" if args.variant == "four_gbe" else "ubi-2p5g.dtb"
    dtb_src = bin_dir / ("%s-%s" % (prefix, dtb_name))
    if not dtb_src.is_file():
        raise SystemExit("找不到设备树 %s" % dtb_src)
    kernel_src = find_one(
        ["build_dir/target-*/linux-rtl9617c_generic/linux-*/arch/arm64/boot/Image"],
        tree, "arm64 Image")

    raw_kernel = kernel_src.read_bytes()
    kernel_check(raw_kernel)
    if len(raw_kernel) > contract["limits"]["kernel_raw_bytes"]:
        raise SystemExit("内核 %d 字节, 超过上限 %d"
                         % (len(raw_kernel), contract["limits"]["kernel_raw_bytes"]))

    compression = args.kernel_compression or contract["fit"]["kernel_compression"]
    kernel_blob = lzma_alone(raw_kernel, contract["lzma"]) if compression == "lzma" else raw_kernel
    if len(kernel_blob) > contract["limits"]["kernel_bytes"]:
        raise SystemExit("内核压缩后 %d 字节, 超过上限 %d"
                         % (len(kernel_blob), contract["limits"]["kernel_bytes"]))

    dtb_raw = dtb_src.read_bytes()
    dtb_blob, dtb_info = patch_dtb(dtb_raw, contract)

    rootfs_blob = rootfs_src.read_bytes()
    image_check(rootfs_blob)
    if len(rootfs_blob) > contract["limits"]["rootfs_bytes"]:
        raise SystemExit("rootfs %d 字节, 超过上限 %d"
                         % (len(rootfs_blob), contract["limits"]["rootfs_bytes"]))

    fit_blob = Fit.build(
        kernel=kernel_blob,
        fdt=dtb_blob,
        rootfs=rootfs_blob,
        kernel_compression=compression,
        description="PT1570-R10 OpenWrt %s" % args.version,
        config_name=contract["fit"]["default_config"],
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        kernel_load_addr=int(contract["fit"]["kernel_load_addr"], 0),
        kernel_entry_addr=int(contract["fit"]["kernel_entry_addr"], 0),
    )
    if len(fit_blob) > contract["limits"]["fit_bytes"]:
        raise SystemExit("FIT %d 字节, 超过 fit 卷上限 %d —— 缩小 rootfs 或改用压缩内核"
                         % (len(fit_blob), contract["limits"]["fit_bytes"]))

    fit_path = out / contract["artifacts"]["fit"]
    fit_path.write_bytes(fit_blob)
    print("FIT: %s (%d 字节)" % (fit_path.name, len(fit_blob)))

    openwrt_commit = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
    manifest = {
        "schema": 2,
        "target": contract["target"],
        "device": contract["device"]["model"],
        "variant": args.variant,
        "version": args.version,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "ponwrt": args.ponwrt_ref or "unknown",
            "openwrt": openwrt_commit or "unknown",
            "contract_sha256": sha256_file(args.contract),
        },
        "artifacts": {"fit": fit_path.name},
        "fit": {
            "bytes": len(fit_blob),
            "sha256": sha256_bytes(fit_blob),
            "default_config": contract["fit"]["default_config"],
            "kernel": {"bytes": len(kernel_blob), "compression": compression,
                       "raw_bytes": len(raw_kernel), "sha256": sha256_bytes(kernel_blob)},
            "fdt": {"bytes": len(dtb_blob), "sha256": sha256_bytes(dtb_blob)},
            "rootfs": {"bytes": len(rootfs_blob), "sha256": sha256_bytes(rootfs_blob)},
            "dtb_fixup": dtb_info,
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    write_flash_commands(out, contract, manifest)
    write_readme(out, contract, manifest)

    with (out / "SHA256SUMS").open("w", encoding="utf-8") as handle:
        for name in sorted(p.name for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS"):
            handle.write("%s  %s\n" % (sha256_file(out / name), name))

    print("打包完成: %s" % out)
    print("  bootargs: %s" % dtb_info["bootargs"])
    print("  rootdisk: phandle %d -> %s" % (dtb_info["rootdisk_phandle"],
                                            dtb_info["fit_volume_node"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
