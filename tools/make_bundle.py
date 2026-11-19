#!/usr/bin/env python3
"""把云端编译产物整理成 PT1570-R10 原厂 U-Boot 可直接刷写的刷机包。

    python tools/make_bundle.py --tree sources/openwrt --port ponwrt \
        --out artifacts/bundle --version 0.8.5-pt1570 --variant four_gbe

产物(文件名由原厂 U-Boot 环境宏 upk/upd/upr 决定, 不能改):
    Image.lzma   LZMA-alone 压缩的 arm64 Image, 补零到 6 MiB (upk 宏)
    dtb.img      FDT, 补零到 0x40000 (upd 宏按固定长度写入)
    rootfs       SquashFS (upr 宏)
    sysupgrade.bin  系统内升级用 tar(kernel=Image.lzma, root=rootfs)
    overlay/     空 UBIFS 种子, 仅在首启 overlay 未挂载时手工修复用
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT_DIR_NAME = "sysupgrade-tianyi_tyfza1"


def sha256(path: Path) -> str:
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


def compress_kernel(raw: bytes, params: dict) -> bytes:
    return lzma.compress(
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


def pad_to(blob: bytes, size: int) -> bytes:
    if len(blob) > size:
        raise SystemExit("镜像 %d 字节超过上限 %d 字节" % (len(blob), size))
    return blob + b"\x00" * (size - len(blob))


def pt1570_more_args(contract: dict) -> str:
    """more_args 的完整取值：root 必须指向 ubiblock 节点, 不能是 mtdblock。"""
    boot = contract["boot"]
    mtd = contract["ubi"]["mtd_index"]
    return (
        "setenv more_args ubi.mtd=%d ubi.block=0,%s root=%s "
        "rootfstype=squashfs ro tyfza1.slot=0 pt1570.oem=1"
        % (mtd, boot["root_volume"], boot["root_device"])
    )


def write_flash_commands(bundle: Path, contract: dict, variant: str) -> None:
    limits = contract["limits"]
    net = contract["network"]
    dtb_note = "四口千兆" if variant == "four_gbe" else "带 2.5G 口"
    lines = [
        "# PT1570-R10 原厂 U-Boot 刷机命令 (%s 机型)" % dtb_note,
        "# 电脑网卡: %s/24, 本目录作为 TFTP 根目录" % net["uboot_serverip"],
        "# 在 U-Boot 提示符下逐段粘贴, 每段执行完再继续下一段",
        "",
        "# --- 0. 先把当前环境留档(把下面的输出复制到文本文件) ---",
        "printenv",
        "",
        "# --- 1. 网络 ---",
        "setenv ipaddr %s" % net["uboot_ipaddr"],
        "setenv serverip %s" % net["uboot_serverip"],
        "ping ${serverip}",
        "",
        "# --- 2. 挂载 UBI 并写入 0 号槽 (内核/DTB/rootfs) ---",
        "run ubi_part",
        "ubi info",
        "run upk",
        "run upd",
        "run upr",
        "",
        "# --- 3. 固定从 0 号槽启动, 使用 UBI 块设备做根 ---",
        "# b1 每次启动都会执行 setmoreargs 重建 more_args, 所以要改这个宏本身,",
        "# 只改 more_args 会被启动流程覆盖掉。",
        "setenv sw_commit 0",
        "setenv sw_tryactive 2",
        "setenv setmoreargs '%s'" % pt1570_more_args(contract),
        "saveenv",
        "printenv setmoreargs sw_commit sw_tryactive",
        "",
        "# --- 4. 启动 ---",
        "run b1",
        "",
        "# 首次启动会自动迁移原厂数据(约 1-3 分钟, 期间不要断电), 完成后",
        "# 浏览器打开 %s , 初始无密码。" % contract["network"]["admin_url"],
        "",
        "# 参数上限: 内核 %d 字节, DTB %d 字节, rootfs %d 字节"
        % (limits["kernel_read_bytes"], limits["dtb_write_bytes"], limits["rootfs_volume_bytes"]),
    ]
    (bundle / "flash-cmds.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bundle_readme(bundle: Path, manifest: dict, contract: dict) -> None:
    limits = contract["limits"]
    text = """PT1570-R10 OpenWrt 刷机包
=========================

版本      : {version}
机型      : {device} / {variant}
上游源码  : {ponwrt}
编译提交  : {openwrt}
生成时间  : {built}

文件用途
--------
Image.lzma      内核, LZMA 压缩 + 补零到 6 MiB。对应 U-Boot 宏 upk
dtb.img         设备树, 补零到 0x40000。对应 U-Boot 宏 upd
rootfs          SquashFS 根文件系统。对应 U-Boot 宏 upr
sysupgrade.bin  OpenWrt 系统内升级包(LuCI -> 系统 -> 备份与更新)
overlay/        空 UBIFS 种子, 仅在首启后 /overlay 未挂载时使用
flash-cmds.txt  可直接粘贴到 U-Boot 的命令
SHA256SUMS      校验和

刷机步骤
--------
1. 电脑接光猫 LAN 口, 网卡设为 {server}/24
2. 本目录起 TFTP 服务(仓库内 tools/tftp_serve.py, 或任意 TFTP 服务端)
3. 串口进 U-Boot, 按 flash-cmds.txt 逐段执行
4. 首启自动迁移原厂数据并建立 overlay, 之后访问 {admin}

体积与上限
----------
内核  {kernel:>10} / {kernel_max} 字节
DTB   {dtb:>10} / {dtb_max} 字节
rootfs{rootfs:>10} / {rootfs_max} 字节

注意
----
* Image.lzma 与 dtb.img 的补零是必须的: 原厂启动脚本按固定长度读取卷。
* 不要刷 sysupgrade.bin 到 U-Boot; 它只用于系统内升级。
* 不要写 boot / env / ubi_Config 分区: 前者是本机 U-Boot, 后者是光猫身份与校准数据。
"""
    kernel = (bundle / "Image.lzma").stat().st_size
    dtb = (bundle / "dtb.img").stat().st_size
    rootfs = (bundle / "rootfs").stat().st_size
    (bundle / "README.txt").write_text(
        text.format(
            version=manifest["version"],
            device=contract["device"]["model"],
            variant={"four_gbe": "四口千兆(four_gbe)", "2p5g": "带 2.5G 口(2p5g)"}[manifest["variant"]],
            ponwrt=manifest["sources"]["ponwrt"],
            openwrt=manifest["sources"]["openwrt"][:12],
            built=manifest["built"],
            server=contract["network"]["uboot_serverip"],
            admin=contract["network"]["admin_url"],
            kernel=kernel,
            kernel_max=limits["kernel_read_bytes"],
            dtb=dtb,
            dtb_max=limits["dtb_write_bytes"],
            rootfs=rootfs,
            rootfs_max=limits["rootfs_volume_bytes"],
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", type=Path, required=True, help="OpenWrt 源码树 (sources/openwrt)")
    parser.add_argument("--port", type=Path, required=True, help="PonWRT-TYFZA1 克隆目录")
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--contract", type=Path, default=Path("uboot/contract.json"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--variant", choices=("four_gbe", "2p5g"), default="four_gbe")
    parser.add_argument("--ponwrt-ref", default="")
    parser.add_argument("--skip-seed", action="store_true", help="不生成 overlay 种子")
    args = parser.parse_args()

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    names = contract["artifacts"]
    limits = contract["limits"]
    tree = args.tree.resolve()
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "overlay").mkdir(parents=True)

    bin_dir = tree / "bin/targets/rtl9617c/generic"
    rootfs_src = find_one(["*-squashfs-rootfs.squashfs"], bin_dir, "rootfs 镜像")
    prefix = rootfs_src.name[: -len("-squashfs-rootfs.squashfs")]
    dtb_name = "ubi.dtb" if args.variant == "four_gbe" else "ubi-2p5g.dtb"
    dtb_src = bin_dir / ("%s-%s" % (prefix, dtb_name))
    if not dtb_src.is_file():
        raise SystemExit("找不到设备树 %s" % dtb_src)

    kernel_src = find_one(
        ["build_dir/target-*/linux-rtl9617c_generic/linux-*/arch/arm64/boot/Image"],
        tree,
        "arm64 Image",
    )
    print("内核 : %s (%d 字节)" % (kernel_src, kernel_src.stat().st_size))
    print("设备树: %s" % dtb_src)
    print("rootfs: %s (%d 字节)" % (rootfs_src, rootfs_src.stat().st_size))

    raw = kernel_src.read_bytes()
    if raw[0x38:0x3C] != b"ARM\x64":
        raise SystemExit("内核不是 arm64 Image (缺少 ARM\\x64 魔数)")
    kernel_lzma = compress_kernel(raw, contract["lzma"])
    if len(kernel_lzma) > limits["kernel_read_bytes"]:
        raise SystemExit(
            "内核压缩后 %d 字节, 超过 U-Boot 读取上限 %d 字节"
            % (len(kernel_lzma), limits["kernel_read_bytes"])
        )
    padded_kernel = pad_to(kernel_lzma, limits["kernel_read_bytes"])
    (out / names["kernel"]).write_bytes(padded_kernel)
    print("Image.lzma: %d 字节 (压缩) -> %d 字节 (补零)" % (len(kernel_lzma), len(padded_kernel)))

    dtb_blob = dtb_src.read_bytes()
    if struct.unpack_from(">I", dtb_blob, 0)[0] != 0xD00DFEED:
        raise SystemExit("设备树缺少 FDT 魔数")
    padded_dtb = pad_to(dtb_blob, limits["dtb_write_bytes"])
    (out / names["dtb"]).write_bytes(padded_dtb)
    print("dtb.img: %d -> %d 字节" % (len(dtb_blob), len(padded_dtb)))

    shutil.copyfile(rootfs_src, out / names["rootfs"])
    rootfs_blob = (out / names["rootfs"]).read_bytes()
    if rootfs_blob[:4] != b"hsqs":
        raise SystemExit("rootfs 不是 SquashFS 镜像")
    if len(rootfs_blob) > limits["rootfs_volume_bytes"]:
        raise SystemExit("rootfs %d 字节, 超过原厂卷容量 %d" % (len(rootfs_blob), limits["rootfs_volume_bytes"]))

    # 系统内升级包: 与原厂 U-Boot 的读取方式一致(kernel=Image.lzma, root=rootfs)。
    control = "BOARD=tianyi,tyfza1\nVERSION=%s\n" % args.version
    meta = {
        "metadata_version": 1,
        "supported_devices": ["tianyi,tyfza1", "tianyi,tyfza1-2p5g"],
        "version": args.version,
        "tyfza1_storage": "factory-v1",
    }
    sysupgrade = out / names["sysupgrade"]
    with tarfile.open(sysupgrade, "w", format=tarfile.GNU_FORMAT) as tar:
        for name, blob in (
            ("CONTROL", control.encode()),
            ("kernel", padded_kernel),
            ("root", rootfs_blob),
            # 原厂加载器还会单独读一份 256 KiB 的设备树, 一起带上才能整体升级。
            ("dtb", padded_dtb),
        ):
            info = tarfile.TarInfo("%s/%s" % (ROOT_DIR_NAME, name))
            info.size = len(blob)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(blob))

    fwtool = tree / "staging_dir/host/bin/fwtool"
    if fwtool.is_file():
        command = [str(fwtool), "-I", "-", str(sysupgrade)]
        # 便于在 Windows 上跑 tools/selftest.py: 脚本形式的 fwtool 交给解释器执行。
        if fwtool.open("rb").read(2) == b"#!":
            command.insert(0, sys.executable)
        subprocess.run(command, input=json.dumps(meta).encode(), check=True)
        print("sysupgrade.bin: %d 字节 (含 factory-v1 元数据)" % sysupgrade.stat().st_size)
    else:
        print("警告: 未找到 %s, sysupgrade.bin 缺少元数据, 系统内升级会被拒绝" % fwtool,
              file=sys.stderr)
        (out / names["sysupgrade"]).with_suffix(".metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    seed_note = "skipped"
    if not args.skip_seed:
        mkfs = tree / "staging_dir/host/bin/mkfs.ubifs"
        builder = args.port / "port/packages/tyfza1-storage/build_overlay_seed.py"
        if mkfs.is_file() and builder.is_file():
            seed_gz = out / "overlay/rootfs_data.ubifs.gz"
            result = subprocess.run(
                [sys.executable, str(builder), str(mkfs), str(seed_gz)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                seed_note = "%d 字节" % seed_gz.stat().st_size
                print("overlay 种子: %s" % seed_note)
            else:
                seed_note = "mkfs.ubifs 失败: " + result.stderr.strip().splitlines()[-1]
                print("警告: " + seed_note, file=sys.stderr)
                seed_gz.unlink(missing_ok=True)
        else:
            print("提示: 缺少 mkfs.ubifs 或种子生成脚本, 跳过 overlay 种子", file=sys.stderr)

    openwrt_commit = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    manifest = {
        "schema": 1,
        "device": contract["device"]["model"],
        "variant": args.variant,
        "version": args.version,
        "built": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                capture_output=True, text=True).stdout.strip(),
        "sources": {
            "ponwrt": args.ponwrt_ref or "unknown",
            "openwrt": openwrt_commit or "unknown",
            "contract_sha256": sha256(args.contract),
        },
        "limits": limits,
        "ubi_volumes": [
            {"id": v["id"], "name": v["name"], "bytes": v["bytes"], "touched": v["touched"]}
            for v in contract["ubi"]["volumes"]
        ],
        "files": {
            names["kernel"]: {"bytes": len(padded_kernel), "sha256": sha256(out / names["kernel"])},
            names["dtb"]: {"bytes": len(padded_dtb), "sha256": sha256(out / names["dtb"])},
            names["rootfs"]: {"bytes": len(rootfs_blob), "sha256": sha256(out / names["rootfs"])},
            names["sysupgrade"]: {"bytes": sysupgrade.stat().st_size,
                                  "sha256": sha256(out / names["sysupgrade"])},
        },
        "overlay_seed": seed_note,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    write_flash_commands(out, contract, args.variant)
    write_bundle_readme(out, manifest, contract)

    with (out / "SHA256SUMS").open("w", encoding="utf-8") as handle:
        for name in sorted(p.name for p in out.iterdir() if p.is_file() and p.name != "SHA256SUMS"):
            handle.write("%s  %s\n" % (sha256(out / name), name))
        seed = out / "overlay/rootfs_data.ubifs.gz"
        if seed.is_file():
            handle.write("%s  overlay/rootfs_data.ubifs.gz\n" % sha256(seed))
    print("刷机包已生成: %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
