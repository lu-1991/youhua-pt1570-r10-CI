#!/usr/bin/env python3
"""FIT 打包/校验的离线自测（不需要 OpenWrt 源码树、不需要 Linux 工具）。

    python tools/selftest_fit.py

做两件事:
  1. 用合成内核/设备树/rootfs 跑一遍 pack_fit.py -> verify_fit.py, 必须全绿;
  2. 对 5 个"真机会起不来"的坏包各跑一次校验, 必须都被抓出来:
        - rootfs 不在 loadables           -> 内核不会注册 /dev/fit0
        - rootfs 子镜像未 4 KiB 对齐       -> fitblk 直接跳过该子镜像
        - volname 不是 "fit"              -> UBI 侧找不到卷, fitblk 一直 defer
        - bootargs 没有 root=/dev/fit0    -> "VFS: Cannot open root device"
        - rootfs 数据被改过               -> hash-1 不匹配(镜像损坏)
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fitimg  # noqa: E402
import pack_fit  # noqa: E402
import verify_fit  # noqa: E402

CONTRACT = Path(__file__).resolve().parent.parent / "uboot" / "contract-uboot2026.json"


def make_tree(base: Path) -> Path:
    tree = base / "tree"
    (tree / "bin/targets/rtl9617c/generic").mkdir(parents=True)
    (tree / "build_dir/target-x/linux-rtl9617c_generic/linux-6.18/arch/arm64/boot").mkdir(parents=True)

    root = fitimg.Node("")
    root.set_str("model", "Tianyi TYFZA1 (RTL9617C UBI)")
    root.set_str("compatible", "tianyi,tyfza1")
    chosen = root.add_child("chosen")
    chosen.set_str("bootargs", "earlycon console=ttyS0,115200n8 loglevel=7")
    flash = root.add_child("spi@4f4324000").add_child("flash@0")
    flash.set_str("compatible", "spi-nand")
    part = flash.add_child("partitions").add_child("partition@340000")
    part.set_bytes("reg", struct.pack(">II", 0x340000, 0x0F5C0000))
    (tree / "bin/targets/rtl9617c/generic/t-ubi.dtb").write_bytes(fitimg.Dtb(root).serialize())

    kernel = b"\x00" * 0x38 + b"ARM\x64" + bytes(range(256)) * 300
    (tree / "build_dir/target-x/linux-rtl9617c_generic/linux-6.18/arch/arm64/boot/Image").write_bytes(kernel)
    (tree / "bin/targets/rtl9617c/generic/t-squashfs-rootfs.squashfs").write_bytes(
        b"hsqs" + b"\x00" * 64 + bytes(range(256)) * 700)
    return tree


def run_verify(bundle: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = verify_fit.main([str(bundle), "--contract", str(CONTRACT)])
    return code, buf.getvalue()


def rebuild(bundle: Path, mutate) -> None:
    """改 FIT 头里的描述(不动数据区), 用来构造反例。"""
    path = bundle / "sysupgrade.itb"
    blob = path.read_bytes()
    header_len = struct.unpack_from(">I", blob, 4)[0]
    dtb = fitimg.Dtb.parse(blob[:header_len])
    mutate(dtb)
    new_header = dtb.serialize()
    if len(new_header) != header_len:
        raise SystemExit("反例构造失败: FDT 长度变了 %d -> %d" % (header_len, len(new_header)))
    path.write_bytes(new_header + blob[header_len:])


def build_bundle(out: Path, tree: Path, dtb_mutate=None) -> None:
    """跑一遍 pack_fit; dtb_mutate 用来在打包时篡改设备树(构造反例)。"""
    original = pack_fit.patch_dtb
    if dtb_mutate is not None:
        def patched(blob: bytes, contract: dict):
            new_blob, info = original(blob, contract)
            dtb = fitimg.Dtb.parse(new_blob)
            dtb_mutate(dtb)
            return dtb.serialize(), info
        pack_fit.patch_dtb = patched
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pack_fit.main(["--tree", str(tree), "--port", str(tree), "--out", str(out),
                           "--version", "selftest", "--contract", str(CONTRACT)])
    finally:
        pack_fit.patch_dtb = original


def rebuild(bundle: Path, mutate) -> None:
    """改 FIT 头里的描述(不动数据区), 用来构造反例。"""
    path = bundle / "sysupgrade.itb"
    blob = path.read_bytes()
    header_len = struct.unpack_from(">I", blob, 4)[0]
    dtb = fitimg.Dtb.parse(blob[:header_len])
    mutate(dtb)
    new_header = dtb.serialize()
    if len(new_header) != header_len:
        raise SystemExit("反例构造失败: FDT 长度变了 %d -> %d" % (header_len, len(new_header)))
    path.write_bytes(new_header + blob[header_len:])


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="pt1570-selftest-"))
    failures = 0
    try:
        tree = make_tree(base)
        bundle = base / "bundle"
        build_bundle(bundle, tree)
        code, out = run_verify(bundle)
        if code == 0 and "FAIL" not in out:
            print("PASS 正常包: 打包 -> 校验全绿")
        else:
            failures += 1
            print("FAIL 正常包校验失败:\n" + out)

        def expect(name: str, probe: Path, needle: str) -> None:
            nonlocal failures
            (probe / "SHA256SUMS").unlink(missing_ok=True)  # 反例只看 FIT 本身
            code, out = run_verify(probe)
            if code != 0 and needle in out:
                print("PASS 反例 %-12s 被抓住: %s" % (name, needle))
            else:
                failures += 1
                print("FAIL 反例 %-12s 没被抓住(code=%d, 期望含 %r)\n%s" % (name, code, needle, out))

        # --- 头里的描述错 ---
        for name, mutate, needle in (
            ("loadables",
             lambda d: d.find("/configurations/config-1").set_bytes(
                 "loadables", b"nosuch-1\x00"),
             "rootfs 在 loadables"),
            ("align",
             lambda d: d.find("/images/rootfs-1").set_bytes(
                 "data", struct.pack(">II", struct.unpack(">II", d.find("/images/rootfs-1").get("data"))[0] + 64,
                                     struct.unpack(">II", d.find("/images/rootfs-1").get("data"))[1])),
             "rootfs 位置 4K 对齐"),
        ):
            probe = base / ("case-" + name)
            shutil.copytree(bundle, probe)
            rebuild(probe, mutate)
            expect(name, probe, needle)

        # --- 设备树错(在打包阶段篡改, 长度可变) ---
        for name, mutate, needle in (
            ("volname",
             lambda dtb: dtb.find("/spi@4f4324000/flash@0/volumes/volume-fit").set_str("volname", "abc"),
             "rootdisk -> volname=fit"),
            ("bootargs",
             lambda dtb: dtb.find("/chosen").set_str(
                 "bootargs", (dtb.find("/chosen").get_str("bootargs") or "").replace("/dev/fit0", "/dev/fitx")),
             "bootargs root="),
            ("nouvver",
             lambda dtb: dtb.find("/chosen").del_prop("u-boot,version"),
             "u-boot,version"),
        ):
            probe = base / ("case-" + name)
            build_bundle(probe, tree, mutate)
            expect(name, probe, needle)

        # --- 数据区被改(镜像损坏) ---
        probe = base / "case-corrupt"
        shutil.copytree(bundle, probe)
        path = probe / "sysupgrade.itb"
        blob = bytearray(path.read_bytes())
        blob[-1] ^= 0xFF
        path.write_bytes(bytes(blob))
        expect("corrupt", probe, "rootfs-1 sha256")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n%s" % ("全部通过" if failures == 0 else "%d 项失败" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
