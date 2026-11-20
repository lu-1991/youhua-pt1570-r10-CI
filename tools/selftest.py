#!/usr/bin/env python3
"""离线自检: 用合成镜像跑通 make_bundle.py + verify_bundle.py。

不需要编译 OpenWrt, 用于确认仓库自身的打包/校验逻辑没有被改坏(CI 的 selfcheck 任务
会跑这个)。合成镜像只满足格式约束, 不能被刷进设备。
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def build_fake_dtb(blob_strings: bytes) -> bytes:
    header = struct.pack(">10I", 0xD00DFEED, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    body = blob_strings
    total = len(header) + len(body)
    header = struct.pack(">10I", 0xD00DFEED, total, 0x38, 0x38, 0, 0, 0, 0, 0, 0)
    return header + body


def build_fake_rootfs() -> bytes:
    head = bytearray(96)
    head[0:4] = b"hsqs"
    struct.pack_into("<I", head, 4, 12)          # inode count
    struct.pack_into("<I", head, 12, 131072)     # block size
    struct.pack_into("<HH", head, 28, 4, 0)      # squashfs 4.0
    return bytes(head)


def build_fake_kernel(size: int = 65536) -> bytes:
    image = bytearray(size)
    image[0:4] = struct.pack("<I", 0x91005A4D)   # b Image 分支指令, 内容无意义
    image[0x38:0x3C] = b"ARM\x64"
    return bytes(image)


def prepare_tree(root: Path) -> None:
    bin_dir = root / "bin/targets/rtl9617c/generic"
    bin_dir.mkdir(parents=True)
    (bin_dir / "openwrt-rtl9617c-generic-tianyi_tyfza1-native_bringup-squashfs-rootfs.squashfs").write_bytes(
        build_fake_rootfs()
    )
    (bin_dir / "openwrt-rtl9617c-generic-tianyi_tyfza1-native_bringup-ubi.dtb").write_bytes(
        build_fake_dtb(b"\x00tianyi,tyfza1\x00realtek,rtl9617c\x00ubi_device\x00static_conf\x00")
    )
    kernel_dir = root / "build_dir/target-aarch64_cortex-a55_musl/linux-rtl9617c_generic/linux-6.18.55/arch/arm64/boot"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "Image").write_bytes(build_fake_kernel())
    host_bin = root / "staging_dir/host/bin"
    host_bin.mkdir(parents=True)
    fwtool = host_bin / "fwtool"
    fwtool.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "path = sys.argv[-1]\n"
        "with open(path, 'ab') as handle:\n"
        "    handle.write(data)\n",
        encoding="utf-8",
    )
    fwtool.chmod(0o755)


def run(cmd: list[str], expect: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != expect:
        print("命令失败 (%d): %s" % (result.returncode, " ".join(cmd)))
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(1)
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pt1570-selftest-") as temporary:
        work = Path(temporary)
        tree = work / "openwrt"
        prepare_tree(tree)

        def build(out: Path) -> None:
            run([
                sys.executable, str(REPO / "tools/make_bundle.py"),
                "--target", "oem",                                  # 本文件只覆盖原厂 U-Boot 路径
                "--contract", str(REPO / "uboot/contract.json"),
                "--tree", str(tree), "--port", str(work), "--out", str(out),
                "--version", "selftest", "--skip-seed",
            ])

        def verify(out: Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(REPO / "tools/verify_bundle.py"), str(out),
                 "--contract", str(REPO / "uboot/contract.json")],
                capture_output=True, text=True,
            )

        out = work / "bundle"
        build(out)
        result = verify(out)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            print("自检失败: 合法刷机包未通过校验", file=sys.stderr)
            return 1
        print(result.stdout.strip().splitlines()[-1])

        # 反例 1: DTB 补零被破坏
        dtb = out / "dtb.img"
        blob = bytearray(dtb.read_bytes())
        blob[-1] = 0xAA
        dtb.write_bytes(bytes(blob))
        result = verify(out)
        if result.returncode == 0:
            print("反例未被检出: DTB 补零被破坏却通过校验", file=sys.stderr)
            return 1
        print("反例检出正常: %s" % [l for l in result.stdout.splitlines() if l.startswith("FAIL")][0])

        # 反例 2: 内核补零被截断(用全新的包, 避免受上一个反例影响)
        out2 = work / "bundle-kernel"
        build(out2)
        kernel = out2 / "Image.lzma"
        kernel.write_bytes(kernel.read_bytes()[:-4096])
        result = verify(out2)
        if result.returncode == 0:
            print("反例未被检出: 内核长度不足却通过校验", file=sys.stderr)
            return 1
        print("反例检出正常: %s" % [l for l in result.stdout.splitlines() if l.startswith("FAIL")][0])

        # 反例 3: sysupgrade 的 kernel 与 Image.lzma 不一致
        out3 = work / "bundle-sysupgrade"
        build(out3)
        sysupgrade = out3 / "sysupgrade.bin"
        data = bytearray(sysupgrade.read_bytes())
        data[2048] ^= 0xFF
        sysupgrade.write_bytes(bytes(data))
        result = verify(out3)
        if result.returncode == 0:
            print("反例未被检出: sysupgrade 内容被改动却通过校验", file=sys.stderr)
            return 1
        print("反例检出正常: %s" % [l for l in result.stdout.splitlines() if l.startswith("FAIL")][0])
    print("自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
