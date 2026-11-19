#!/usr/bin/env python3
"""按 U-Boot 启动契约校验 PT1570-R10 刷机包。

    python tools/verify_bundle.py out/pt1570-r10-<版本>
    python tools/verify_bundle.py out/xxx --contract uboot/contract.json --strict-raw

校验项(任一失败即退出码 1):
  * Image.lzma  : LZMA-alone 头(lc3/lp0/pb2, 字典 8 MiB)、长度上限、解压后是 arm64 Image
  * dtb.img     : FDT 魔数、总长、补零到 0x40000、板级 compatible
  * rootfs      : SquashFS 魔数、长度上限
  * sysupgrade  : tar 内 kernel/root 与刷机包一致, 带 CONTROL 与 factory-v1 元数据
  * SHA256SUMS  : 与实际文件一致
  * manifest    : 与 contract 的卷布局/上限一致
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import struct
import sys
import tarfile
import unicodedata
from pathlib import Path

FDT_MAGIC = 0xD00DFEED
SQUASHFS_MAGIC = b"hsqs"
IMAGE_MAGIC = b"ARM\x64"
ROOT_DIR_NAME = "sysupgrade-tianyi_tyfza1"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("PASS", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.rows.append(("FAIL", name, detail))
        self.failed += 1

    def check(self, name: str, condition: bool, detail: str) -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)
        return condition

    def render(self) -> str:
        def width(text: str) -> int:
            return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)

        pad = max((width(r[1]) for r in self.rows), default=10)
        lines = []
        for status, name, detail in self.rows:
            lines.append("%s  %s%s  %s" % (status, name, " " * (pad - width(name)), detail))
        return "\n".join(lines)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_kernel(path: Path, contract: dict, report: Report, strict_raw: bool) -> bytes | None:
    limits = contract["limits"]
    if not report.check("Image.lzma 存在", path.is_file(), str(path)):
        return None
    size = path.stat().st_size
    report.check(
        "Image.lzma 长度",
        size == limits["kernel_read_bytes"],
        "%d / %d 字节 (必须补零到启动脚本的固定读取长度)" % (size, limits["kernel_read_bytes"]),
    )
    blob = path.read_bytes()
    props = blob[0]
    lc, lp, pb = props % 9, (props // 9) % 5, props // 45
    params = contract["lzma"]
    report.check(
        "LZMA 参数",
        (lc, lp, pb) == (params["lc"], params["lp"], params["pb"]),
        "lc=%d lp=%d pb=%d (要求 %d/%d/%d)" % (lc, lp, pb, params["lc"], params["lp"], params["pb"]),
    )
    dictionary = struct.unpack_from("<I", blob, 1)[0]
    report.check(
        "LZMA 字典",
        dictionary == params["dictionary_bytes"],
        "%d 字节" % dictionary,
    )
    unknown_size = struct.unpack_from("<Q", blob, 5)[0] == 0xFFFFFFFFFFFFFFFF
    report.check("LZMA 无预置长度", unknown_size, "流内含结束标记" if unknown_size else "头部写了长度")
    try:
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        chunks: list[bytes] = []
        feed = blob
        total = 0
        limit = limits["kernel_raw_bytes_max"] * 2
        # 补零是契约的一部分: 逐块解压到流结束标记为止, 不把 6 MiB 补零当成新流。
        while not decoder.eof:
            chunk = decoder.decompress(feed, max_length=1 << 20)
            feed = b""
            total += len(chunk)
            chunks.append(chunk)
            if total > limit:
                raise lzma.LZMAError("解压结果超过 %d 字节, 不是有效内核" % limit)
            if not decoder.eof and decoder.needs_input:
                raise lzma.LZMAError("LZMA 流在补零之前就结束了")
        raw = b"".join(chunks)
    except lzma.LZMAError as error:
        report.fail("LZMA 解压", str(error))
        return None
    report.check("LZMA 解压", True, "解压后 %d 字节" % len(raw))
    if len(raw) < 0x40:
        report.fail("arm64 Image", "解压结果过短")
        return raw
    report.check("arm64 Image 魔数", raw[0x38:0x3C] == IMAGE_MAGIC, raw[0x38:0x3C].hex())
    if strict_raw:
        report.check(
            "内核装入地址",
            len(raw) <= limits["kernel_raw_bytes_max"],
            "%d / %d 字节 (0x80000 起不覆盖 0x2000000 保留区)"
            % (len(raw), limits["kernel_raw_bytes_max"]),
        )
    else:
        report.ok(
            "内核装入地址",
            "%d 字节, 上限 %d" % (len(raw), limits["kernel_raw_bytes_max"]),
        )
    return raw


def check_dtb(path: Path, contract: dict, report: Report) -> None:
    limits = contract["limits"]
    if not report.check("dtb.img 存在", path.is_file(), str(path)):
        return
    blob = path.read_bytes()
    size = len(blob)
    report.check(
        "dtb.img 长度", size == limits["dtb_write_bytes"],
        "%d / %d 字节 (upd 宏按固定长度写入)" % (size, limits["dtb_write_bytes"]),
    )
    magic, totalsize = struct.unpack_from(">II", blob, 0)
    report.check("FDT 魔数", magic == FDT_MAGIC, hex(magic))
    report.check(
        "FDT 总长",
        totalsize <= limits["dtb_volume_bytes"] and totalsize <= size,
        "%d 字节, 卷容量 %d" % (totalsize, limits["dtb_volume_bytes"]),
    )
    padding = blob[totalsize:]
    report.check("FDT 补零", not any(padding), "%d 字节补零" % len(padding))
    report.check(
        "板级 compatible",
        b"tianyi,tyfza1" in blob and b"realtek,rtl9617c" in blob,
        "tianyi,tyfza1 + realtek,rtl9617c",
    )
    report.check(
        "分区表",
        b"ubi_device" in blob and b"static_conf" in blob,
        "含 ubi_device / static_conf 分区标签",
    )


def check_rootfs(path: Path, contract: dict, report: Report) -> None:
    limits = contract["limits"]
    if not report.check("rootfs 存在", path.is_file(), str(path)):
        return
    size = path.stat().st_size
    report.check(
        "rootfs 长度", size <= limits["rootfs_volume_bytes"],
        "%d / %d 字节" % (size, limits["rootfs_volume_bytes"]),
    )
    with path.open("rb") as handle:
        head = handle.read(32)
    report.check("SquashFS 魔数", head[:4] == SQUASHFS_MAGIC, head[:4].decode("latin-1"))
    major, minor = struct.unpack_from("<HH", head, 28)
    report.check("SquashFS 版本", major == 4, "%d.%d" % (major, minor))


def check_sysupgrade(path: Path, contract: dict, report: Report, kernel: Path, rootfs: Path) -> None:
    if not report.check("sysupgrade.bin 存在", path.is_file(), str(path)):
        return
    blob = path.read_bytes()
    report.check("factory-v1 元数据", b"tyfza1_storage" in blob, "含 tyfza1_storage 标记")
    report.check("支持的设备元数据", b"tianyi,tyfza1" in blob, "supported_devices 含 tianyi,tyfza1")
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
            names = tar.getnames()
            required = [ROOT_DIR_NAME + "/CONTROL", ROOT_DIR_NAME + "/kernel", ROOT_DIR_NAME + "/root"]
            missing = [n for n in required if n not in names]
            report.check("sysupgrade tar 结构", not missing, "缺少 " + ",".join(missing) if missing else "CONTROL + kernel + root")
            if missing:
                return
            control = tar.extractfile(ROOT_DIR_NAME + "/CONTROL").read()
            report.check("CONTROL 内容", b"BOARD=" in control, control.decode("latin-1").replace("\n", " ").strip()[:60])
            inside_kernel = tar.extractfile(ROOT_DIR_NAME + "/kernel").read()
            inside_root = tar.extractfile(ROOT_DIR_NAME + "/root").read()
            dtb_member = ROOT_DIR_NAME + "/dtb"
            inside_dtb = tar.extractfile(dtb_member).read() if dtb_member in names else None
    except (tarfile.TarError, KeyError) as error:
        report.fail("sysupgrade tar 结构", str(error))
        return
    if kernel.is_file():
        disk = kernel.read_bytes()
        disk = disk[: len(inside_kernel)] if len(disk) >= len(inside_kernel) else disk
        report.check(
            "sysupgrade kernel == Image.lzma",
            inside_kernel == disk,
            "%d 字节" % len(inside_kernel),
        )
    if inside_dtb is not None:
        # 原厂加载器单独读 256 KiB 设备树, 长度和 FDT 魔数都必须对得上。
        report.check(
            "sysupgrade dtb 长度",
            len(inside_dtb) == contract["limits"]["dtb_write_bytes"],
            "%d / %d 字节" % (len(inside_dtb), contract["limits"]["dtb_write_bytes"]),
        )
        report.check(
            "sysupgrade dtb FDT 魔数",
            inside_dtb[:4] == b"\xd0\x0d\xfe\xed",
            inside_dtb[:4].hex(),
        )
    if rootfs.is_file():
        report.check(
            "sysupgrade root == rootfs",
            inside_root == rootfs.read_bytes(),
            "%d 字节" % len(inside_root),
        )


def check_manifest(bundle: Path, contract: dict, report: Report) -> None:
    path = bundle / "manifest.json"
    if not report.check("manifest.json 存在", path.is_file(), str(path)):
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    report.check("manifest 机型", manifest.get("device") == contract["device"]["model"],
                 str(manifest.get("device")))
    report.check(
        "manifest 卷布局",
        manifest.get("ubi_volumes") == [
            {"id": v["id"], "name": v["name"], "bytes": v["bytes"], "touched": v["touched"]}
            for v in contract["ubi"]["volumes"]
        ],
        "%d 个卷与契约一致" % len(contract["ubi"]["volumes"]),
    )
    limits = manifest.get("limits", {})
    for key, value in contract["limits"].items():
        report.check("manifest 上限 %s" % key, limits.get(key) == value, "%s / %s" % (limits.get(key), value))


def check_sums(bundle: Path, report: Report) -> None:
    path = bundle / "SHA256SUMS"
    if not report.check("SHA256SUMS 存在", path.is_file(), str(path)):
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        target = bundle / name
        if not target.is_file():
            report.fail("校验和 %s" % name, "文件缺失")
            continue
        report.check("校验和 %s" % name, sha256(target) == digest, digest[:16] + "...")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, help="刷机包目录")
    parser.add_argument("--contract", type=Path, default=Path("uboot/contract.json"))
    parser.add_argument("--strict-raw", action="store_true", help="内核未压缩体积超限时判为失败")
    args = parser.parse_args()

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    names = contract["artifacts"]
    bundle = args.bundle
    if not bundle.is_dir():
        print("目录不存在: %s" % bundle, file=sys.stderr)
        return 2

    report = Report()
    kernel = bundle / names["kernel"]
    dtb = bundle / names["dtb"]
    rootfs = bundle / names["rootfs"]
    sysupgrade = bundle / names["sysupgrade"]

    check_kernel(kernel, contract, report, args.strict_raw)
    check_dtb(dtb, contract, report)
    check_rootfs(rootfs, contract, report)
    check_sysupgrade(sysupgrade, contract, report, kernel, rootfs)
    check_manifest(bundle, contract, report)
    check_sums(bundle, report)

    print(report.render())
    print()
    if report.failed:
        print("结果: %d 项失败, 不要刷机" % report.failed)
        return 1
    print("结果: 全部通过, 与 U-Boot 启动契约一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
