#!/usr/bin/env python3
"""读取并校验 Realtek TYFZA1/PT1570-R10 的 U-Boot 环境分区转储。

用法:
    python tools/parse_env.py uboot.bin/env.bin                 # 打印关键项
    python tools/parse_env.py uboot/env.bin --contract uboot/contract.json
    python tools/parse_env.py uboot/env.bin --write uboot/env-pt1570-r10.txt

环境分区是 U-Boot 的冗余环境: 前 4 字节 CRC32, 随后是连续的 "key=value\\0"。
本工具不做 CRC 校验(CRC 覆盖 'value\\0' 之后可能被裁剪), 只按边界提取字符串,
因为这里的目标是核对启动契约, 而不是校验环境完整性。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

KEY_FIELDS = (
    "bootcmd",
    "bootdelay",
    "baudrate",
    "mtdids",
    "mtdparts",
    "kernel_addr_r",
    "fdt_addr_r",
    "initrd_addr_r",
    "unzip_src",
    "tftp_base",
    "ipaddr",
    "serverip",
    "netmask",
    "gatewayip",
    "ethact",
    "sw_commit",
    "sw_active",
    "sw_tryactive",
    "sw_version0",
    "sw_version1",
    "pfl_boot_sz",
    "pfl_env_size",
    "pfl_cfg_size",
    "pfl_fdt1_sz",
    "pfl_fdt2_sz",
    "pfl_kernel1_sz",
    "pfl_kernel2_sz",
    "pfl_rootfs1_sz",
    "pfl_rootfs2_sz",
    "ubi_mtd_num",
    "ubi_device_name",
    "ubi_device_img_name",
    "uboot_img_name",
    "rootfs_img_name",
    "basicargs",
    "more_args",
)


def extract(data: bytes) -> dict[str, str]:
    """从原始分区数据里提取 key=value 对(容忍 0xFF 填充与两套冗余环境)。"""
    text = data.split(b"\x00\x00", 1)[0]
    raw = re.findall(rb"[A-Za-z_][A-Za-z0-9_]*=[\x20-\x7e]*", data)
    env: dict[str, str] = {}
    for item in raw:
        key, _, value = item.partition(b"=")
        try:
            name = key.decode("ascii")
            env[name] = value.decode("ascii", "replace")
        except UnicodeDecodeError:
            continue
    del text
    return env


def contract_expectations(contract: dict) -> dict[str, str]:
    boot = contract["boot"]
    net = contract["network"]
    ubi = contract["ubi"]
    limits = contract["limits"]
    return {
        "kernel_addr_r": boot["kernel_load_addr"],
        "fdt_addr_r": boot["fdt_load_addr"],
        "tftp_base": boot["tftp_base"],
        "ubi_mtd_num": str(ubi["mtd_index"]),
        "ubi_device_name": ubi["part_name"],
        "pfl_kernel1_sz": hex(limits["kernel_read_bytes"]),
        "pfl_fdt1_sz": hex(limits["dtb_write_bytes"]),
        "ipaddr": net["uboot_ipaddr"],
        "serverip": net["uboot_serverip"],
        "netmask": net["netmask"],
    }


def numeric(value: str) -> int | None:
    """U-Boot 的地址/尺寸是十六进制且常省略 0x 前缀, 例如 00080000。"""
    value = value.strip()
    if not value:
        return None
    try:
        if value.lower().startswith("0x"):
            return int(value, 16)
        if re.fullmatch(r"[0-9a-fA-F]+", value) and (
            re.search(r"[a-fA-F]", value) or len(value) > 1 and value.startswith("0")
        ):
            return int(value, 16)
        return int(value, 10)
    except ValueError:
        return None


def check(env: dict[str, str], contract: dict, verbose: bool = True) -> list[str]:
    problems: list[str] = []

    mtdparts = env.get("mtdparts", "")
    if contract["mtdparts"] not in mtdparts:
        problems.append(
            "mtdparts 与契约不一致:\n  环境: %s\n  契约: %s"
            % (mtdparts or "<缺失>", contract["mtdparts"])
        )

    for key, expected in contract_expectations(contract).items():
        actual = env.get(key)
        if actual is None:
            problems.append("环境缺少 %s (契约要求 %s)" % (key, expected))
            continue
        if numeric(actual) is not None and numeric(expected) is not None:
            if numeric(actual) != numeric(expected):
                problems.append("%s=%s, 契约要求 %s" % (key, actual, expected))
        elif actual != expected:
            problems.append("%s=%s, 契约要求 %s" % (key, actual, expected))

    bootcmd = env.get("bootcmd", "")
    if "boot_by_commit" not in bootcmd and "boot_by_tryactive" not in bootcmd:
        problems.append("bootcmd 缺少 boot_by_commit/boot_by_tryactive: %r" % bootcmd)

    process = " ".join(env.get(k, "") for k in ("process1", "process2"))
    if "lzmadec" not in process:
        problems.append("process1/process2 未使用 lzmadec: 该 U-Boot 不是本仓库假设的启动方式")
    if "booti" not in env.get("b", "") + process:
        problems.append("启动命令不是 booti: 无法直接引导 arm64 Image")

    for macro in ("upk", "upd", "upr"):
        if macro not in env:
            problems.append("环境缺少 %s 宏, 刷机脚本将无法直接使用" % macro)

    for item in contract["boot"]["bootargs_required"]:
        if not re.fullmatch(r"[A-Za-z0-9_.\-=/,.]+", item):
            problems.append("契约 bootargs 项格式异常: %r" % item)
        elif item.startswith("root=") and "ubiblock" not in item:
            problems.append("契约要求从 UBI 块设备启动, 但写成 %r" % item)

    if verbose:
        print("检查项: %d, 问题: %d" % (len(contract_expectations(contract)) + 6, len(problems)))
    return problems


def render(env: dict[str, str]) -> str:
    lines = ["# PT1570-R10 U-Boot 环境关键项 (由 tools/parse_env.py 从分区转储生成)", ""]
    for key in KEY_FIELDS:
        if key in env:
            lines.append("%s=%s" % (key, env[key]))
    lines.append("")
    lines.append("# 其它宏 (刷机时直接调用)")
    for key in sorted(env):
        if key in KEY_FIELDS or key.startswith(("_", "create_", "erase_", "chk_", "set")):
            continue
        value = env[key]
        if key in {"upk", "upd", "upr", "upk1", "upd1", "upr1", "upb", "updev", "upenv",
                   "upframework", "upt", "upv", "ubi_part", "check_vol"}:
            lines.append("%s=%s" % (key, value))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("env", type=Path, help="U-Boot 环境分区转储, 例如 mtd1-env.bin")
    parser.add_argument("--contract", type=Path, help="uboot/contract.json")
    parser.add_argument("--write", type=Path, help="把关键项写到文本文件")
    args = parser.parse_args()

    data = args.env.read_bytes()
    env = extract(data)
    if not env:
        print("未能从 %s 提取到任何环境变量" % args.env, file=sys.stderr)
        return 2

    print("%s: %d 字节, %d 个变量" % (args.env, len(data), len(env)))
    for key in ("bootcmd", "mtdparts", "sw_commit", "sw_active", "pfl_kernel1_sz", "pfl_rootfs1_sz"):
        if key in env:
            print("  %-16s %s" % (key, env[key]))

    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(render(env), encoding="utf-8")
        print("关键项已写入 %s" % args.write)

    if args.contract:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        problems = check(env, contract)
        if problems:
            print("\n与契约不一致:")
            for item in problems:
                print("  - " + item)
            return 1
        print("\n与 %s 一致: 该 U-Boot 可以按本仓库的刷机流程引导固件" % args.contract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
