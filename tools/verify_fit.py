#!/usr/bin/env python3
"""校验 U-Boot 2026.07 的 FIT 刷机包(与 pack_fit.py 成对)。

    python tools/verify_fit.py out/pt1570-r10-<版本>
    python tools/verify_fit.py out/xxx --contract uboot/contract-uboot2026.json

检查项都来自"启动失败的实证路径", 不是形式主义:
  * U-Boot 侧: 只认 FIT; 读 ubi 的 fit 卷; 不会注入 root= (env 里没有 bootargs)
  * 内核侧: fitblk 驱动要求 /chosen/u-boot,version + rootdisk phandle ->
    volumes/volume-fit(volname="fit"), 且 rootfs 子镜像 data-position/data-size 4 KiB 对齐
  * 任一项不满足的现场表现: 停在 bootmenu、或 "not a FIT image"、
    或 "VFS: Cannot open root device"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fitimg import Dtb, Fit, FdtError  # noqa: E402
from verify_bundle import Report  # noqa: E402

IMAGE_MAGIC = b"ARM\x64"
SQUASHFS_MAGIC = b"hsqs"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_fit(report: Report, blob: bytes, contract: dict) -> dict:
    align = contract["fit"]["align_bytes"]
    limits = contract["limits"]
    boot = contract["bootloader"]

    report.check("FIT 魔数", blob[:4] == b"\xd0\x0d\xfe\xed",
                 "0x%08x" % (struct.unpack(">I", blob[:4])[0] if len(blob) >= 4 else 0))
    report.check("FIT 体积", len(blob) <= limits["fit_bytes"],
                 "%d / %d 字节" % (len(blob), limits["fit_bytes"]))
    try:
        info = Fit.parse(blob)
    except (FdtError, ValueError) as exc:
        report.fail("FIT 解析", str(exc))
        return {}

    images = info["images"]
    cfg = info["config"]
    report.check("默认配置存在", info["default_config"] == contract["fit"]["default_config"],
                 info["default_config"])
    report.check("kernel 子镜像", cfg["kernel"] == "kernel-1" and "kernel-1" in images,
                 cfg["kernel"])
    report.check("fdt 子镜像", cfg["fdt"] == "fdt-1" and "fdt-1" in images, cfg["fdt"])
    report.check("rootfs 在 loadables", "rootfs-1" in cfg["loadables"], str(cfg["loadables"]))

    # 每个子镜像: 范围、hash、类型
    for name, img in images.items():
        pos, size = img["position"], img["size"]
        inside = pos + size <= len(blob) and pos >= 0
        report.check("%s 数据范围" % name, inside, "pos=%d size=%d" % (pos, size))
        if not inside:
            continue
        payload = blob[pos:pos + size]
        hash_node = img["node"].child("hash-1")
        if hash_node is None:
            report.fail("%s hash-1" % name, "缺少 hash-1 节点")
        else:
            want = hash_node.get("value") or b""
            got = hashlib.sha256(payload).digest()
            report.check("%s sha256" % name, want == got,
                         got.hex()[:16] + "…")

    if "rootfs-1" not in images:
        report.fail("rootfs 子镜像", "不存在")
        return info
    rootfs = images["rootfs-1"]
    report.check("rootfs 类型", rootfs["type"] == "filesystem", rootfs["type"])
    report.check("rootfs 位置 4K 对齐", rootfs["position"] % align == 0,
                 "pos=%d" % rootfs["position"])
    report.check("rootfs 长度 4K 对齐", rootfs["size"] % align == 0,
                 "size=%d" % rootfs["size"])
    payload = blob[rootfs["position"]:rootfs["position"] + rootfs["size"]]
    report.check("rootfs 是 SquashFS", payload[:4] == SQUASHFS_MAGIC, payload[:4].hex())
    report.check("rootfs 体积", rootfs["size"] <= limits["rootfs_bytes"],
                 "%d / %d" % (rootfs["size"], limits["rootfs_bytes"]))

    # 内核: 解压后必须是 arm64 Image
    kernel = images.get("kernel-1")
    if kernel:
        # 现场根因: kernel 子镜像缺 load/entry, bootm 报 "Can't get image load
        # address!" 后 System boot failed。必须和契约里的 arm64 链接地址一致。
        want_load = int(contract["fit"]["kernel_load_addr"], 0)
        want_entry = int(contract["fit"]["kernel_entry_addr"], 0)
        # 新内核 text_offset=0, arm64 booting.rst 要求从 2 MiB 对齐的基址加载;
        # 用 0x80000(512 KiB 对齐)会卡在 "Starting kernel ..." 且无任何输出。
        report.check("kernel load 2MiB 对齐",
                     kernel["load"] is not None and kernel["load"] % (2 * 1024 * 1024) == 0,
                     "%s" % ("0x%08x" % kernel["load"] if kernel["load"] is not None else "缺失"))
        report.check("kernel load 属性", kernel["load"] == want_load,
                     "%s / 期望 0x%08x" % (
                         "0x%08x" % kernel["load"] if kernel["load"] is not None else "缺失",
                         want_load))
        report.check("kernel entry 属性", kernel["entry"] == want_entry,
                     "%s / 期望 0x%08x" % (
                         "0x%08x" % kernel["entry"] if kernel["entry"] is not None else "缺失",
                         want_entry))
        kpayload = blob[kernel["position"]:kernel["position"] + kernel["size"]]
        comp = kernel["compression"]
        report.check("kernel compression", comp in ("lzma", "none"), comp)
        if comp == "lzma":
            report.check("LZMA-alone 头", kpayload[:1] == b"\x5d", kpayload[:1].hex())
            (dict_size,) = struct.unpack_from("<I", kpayload, 1)
            (raw_size,) = struct.unpack_from("<Q", kpayload, 5)
            report.check("LZMA 字典", dict_size == contract["lzma"]["dictionary_bytes"],
                         "%d" % dict_size)
            try:
                raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(kpayload)
            except lzma.LZMAError as exc:
                report.fail("LZMA 解压", str(exc))
                raw = b""
            if raw:
                report.check("LZMA 声明长度", raw_size == len(raw),
                             "%d / %d" % (raw_size, len(raw)))
                report.check("解压后是 arm64 Image", raw[0x38:0x3C] == IMAGE_MAGIC,
                             raw[0x38:0x3C].hex())
                report.check("内核体积", len(raw) <= limits["kernel_raw_bytes"],
                             "%d / %d" % (len(raw), limits["kernel_raw_bytes"]))
        else:
            report.check("kernel 是 arm64 Image", kpayload[0x38:0x3C] == IMAGE_MAGIC,
                         kpayload[0x38:0x3C].hex())
        report.check("kernel 体积", kernel["size"] <= limits["kernel_bytes"],
                     "%d / %d" % (kernel["size"], limits["kernel_bytes"]))

    # DTB 三件套
    fdt_img = images.get("fdt-1")
    if fdt_img:
        dtb_blob = blob[fdt_img["position"]:fdt_img["position"] + fdt_img["size"]]
        try:
            dtb = Dtb.parse(dtb_blob)
        except FdtError as exc:
            report.fail("DTB 解析", str(exc))
            return info
        report.ok("DTB 解析", "%d 字节" % fdt_img["size"])
        chosen = dtb.find("/chosen")
        if chosen is None:
            report.fail("/chosen", "不存在 —— 内核拿不到 root= 与 rootdisk")
            return info
        bootargs = chosen.get_str("bootargs") or ""
        report.check("bootargs root=", boot["root_device"] in bootargs,
                     bootargs[:80] + ("…" if len(bootargs) > 80 else ""))
        report.check("bootargs rootwait", "rootwait" in bootargs.split(), "")
        report.check("bootargs rootfstype", ("rootfstype=%s" % boot["rootfstype"]) in bootargs.split(), "")
        for token in boot["bootargs_required"]:
            report.check("bootargs 含 %s" % token, token in bootargs.split(), "")
        report.check("u-boot,version", chosen.get("u-boot,version") is not None,
                     chosen.get_str("u-boot,version") or "")

        phandle = chosen.get_u32("rootdisk")
        report.check("rootdisk phandle", phandle is not None, str(phandle))
        if phandle:
            target = None
            for node in dtb.root.walk():
                if node.get_u32("phandle") == phandle or node.get_u32("linux,phandle") == phandle:
                    target = node
                    break
            report.check("rootdisk 可解析", target is not None,
                         target.path() if target else "phandle %d 无对应节点" % phandle)
            if target is not None:
                volname = target.get_str("volname")
                report.check("rootdisk -> volname=fit", volname == boot["fit_volume"],
                             "%s (volname=%r)" % (target.path(), volname))
                ancestor = target.parent
                flash_ok = False
                while ancestor is not None:
                    if ancestor.child("partitions") is not None:
                        flash_ok = True
                        break
                    ancestor = ancestor.parent
                report.check("volume 节点挂在 flash 节点下", flash_ok,
                             "UBI 的 volumes 必须在 flash 节点下(vmt.c: vol->dev.parent->parent)")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--contract", type=Path, default=Path("uboot/contract-uboot2026.json"))
    args = parser.parse_args(argv)

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    names = contract["artifacts"]
    bundle = args.bundle.resolve()
    report = Report()

    report.check("目录存在", bundle.is_dir(), str(bundle))
    fit_path = bundle / names["fit"]
    report.check("FIT 文件存在", fit_path.is_file(), str(fit_path))
    manifest_path = bundle / "manifest.json"
    report.check("manifest.json", manifest_path.is_file(), "")
    report.check("SHA256SUMS", (bundle / "SHA256SUMS").is_file(), "")

    if fit_path.is_file():
        blob = fit_path.read_bytes()
        info = check_fit(report, blob, contract)

        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report.check("manifest 版本", manifest.get("version") is not None,
                         str(manifest.get("version")))
            report.check("manifest 指向本目标", manifest.get("target") == contract["target"],
                         str(manifest.get("target")))
            report.check("manifest FIT 大小", manifest["fit"]["bytes"] == len(blob),
                         "%d / %d" % (manifest["fit"]["bytes"], len(blob)))
            report.check("manifest FIT 哈希", manifest["fit"]["sha256"] == hashlib.sha256(blob).hexdigest(), "")
            if info:
                fixup = manifest["fit"].get("dtb_fixup", {})
                report.check("manifest 记录 DTB 注入", bool(fixup.get("bootargs")),
                             str(fixup.get("rootdisk_phandle")))

    sums = bundle / "SHA256SUMS"
    if sums.is_file():
        listed = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(None, 1)
                listed[name.strip()] = digest
        for name, digest in listed.items():
            path = bundle / name
            report.check("SHA256SUMS %s" % name, path.is_file() and sha256_file(path) == digest, "")
        report.check("SHA256SUMS 覆盖 FIT", names["fit"] in listed, "")

    print(report.render())
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
