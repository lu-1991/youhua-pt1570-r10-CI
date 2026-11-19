#!/usr/bin/env python3
"""把原厂 mtd4 全盘转储切块, 并生成 U-Boot 恢复原厂的命令。

RAM 只有 256 MiB, 250 MB 的 ubi_device 不能一次 tftp 进内存, 所以按块写。

    python tools/split_dump.py ../pt1570-r10-backup/mtd4-ubi_device.bin -o restore/

输出:
    restore/ubi-part-01.bin ...   每块默认 48 MiB
    restore/restore-cmds.txt      可直接粘贴进 U-Boot 的命令
    restore/SHA256SUMS
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_CHUNK = 48 * 1024 * 1024
REPO = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", type=Path, help="mtd4-ubi_device.bin")
    parser.add_argument("-o", "--out", type=Path, default=Path("restore"))
    parser.add_argument("--contract", type=Path, default=REPO / "uboot/contract.json")
    parser.add_argument("--chunk", type=lambda v: int(v, 0), default=DEFAULT_CHUNK,
                        help="每块字节数, 默认 0x3000000 (48 MiB)")
    args = parser.parse_args()

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    part = next(p for p in contract["partitions"] if p["label"] == contract["ubi"]["part_name"])
    base = part["offset"]
    size = part["bytes"]

    data = args.dump.read_bytes()
    if len(data) != size:
        print("警告: 转储 %d 字节, 契约要求 %d 字节 (%s)" % (len(data), size, args.dump),
              file=sys.stderr)
        if len(data) > size:
            print("只取前 %d 字节" % size, file=sys.stderr)
            data = data[:size]

    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("ubi-part-*.bin"):
        old.unlink()

    chunks = [data[i:i + args.chunk] for i in range(0, len(data), args.chunk)]
    lines = [
        "# 恢复 PT1570-R10 原厂 UBI 区 (整块覆盖 ubi_device)",
        "# 需要: 串口进 U-Boot, 电脑 192.168.1.7/24 跑 TFTP 并指到本目录",
        "# 警告: 会覆盖当前 openwrt 的全部 UBI 卷, 包括 12/13 号工厂副本",
        "",
        "setenv ipaddr %s" % contract["network"]["uboot_ipaddr"],
        "setenv serverip %s" % contract["network"]["uboot_serverip"],
        "ping ${serverip}",
        "",
        "# 1) 擦除整个 ubi_device 分区 (251648K)",
        "nand erase.part ubi_device",
        "",
    ]
    offset = 0
    for index, chunk in enumerate(chunks, start=1):
        name = "ubi-part-%02d.bin" % index
        (args.out / name).write_bytes(chunk)
        lines += [
            "# 2.%d) %s: 0x%x 字节 @ 0x%x" % (index, name, len(chunk), offset),
            "tftpboot 0x5000000 %s" % name,
            "nand write 0x5000000 0x%x 0x%x" % (base + offset, len(chunk)),
            "",
        ]
        offset += len(chunk)

    lines += [
        "# 3) 恢复环境变量(可选, 环境被改坏时执行)",
        "# cp ../pt1570-r10-backup/mtd1-env.bin uboot-env.bin 之后:",
        "# run upenv",
        "",
        "# 4) 恢复原厂启动参数(如果 more_args 被改过)",
        "setenv sw_commit 1",
        "setenv sw_active 1",
        "setenv more_args ubi.mtd=4 root=31:8 rootfstype=squashfs",
        "saveenv",
        "reset",
    ]
    (args.out / "restore-cmds.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (args.out / "SHA256SUMS").open("w", encoding="utf-8") as handle:
        for index in range(1, len(chunks) + 1):
            name = "ubi-part-%02d.bin" % index
            handle.write("%s  %s\n" % (sha256(args.out / name), name))
    print("已切块: %d 块, 共 %d 字节 -> %s" % (len(chunks), offset, args.out))
    print("命令: %s" % (args.out / "restore-cmds.txt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
