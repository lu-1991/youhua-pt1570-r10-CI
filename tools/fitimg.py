#!/usr/bin/env python3
"""FDT(DTB) 与 uImage.FIT 的最小实现, 纯标准库, 不依赖 dtc/mkimage。

为什么自己写:
  * CI runner 上有 mkimage, 但本地(Windows)没有, 打包层必须能在两边跑出同一个字节;
  * 需要"按 U-Boot 2026.07 + 内核 fitblk 驱动的约定"精确控制子镜像的
    data-position / data-size 4 KiB 对齐 —— mkimage 不保证, 只能自己排布。

对外接口:
    dtb = Dtb.parse(blob)                 # 解析
    node = dtb.find("/chosen")            # 按路径找
    node.set_str("bootargs", "...")       # 改属性
    blob = dtb.serialize()                # 重新序列化

    fit = Fit.build(kernel=..., fdt=..., rootfs=..., ...)
    info = Fit.parse(blob)                # 校验用
"""
from __future__ import annotations

import struct
import sys

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 0x1
FDT_END_NODE = 0x2
FDT_PROP = 0x3
FDT_NOP = 0x4
FDT_END = 0x9

# arm64 Image 头的魔数位置
IMAGE_MAGIC = b"ARM\x64"
SQUASHFS_MAGIC = b"hsqs"


class FdtError(Exception):
    pass


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


class Node:
    """一个 FDT 节点。属性值一律是 bytes, 字符串属性带结尾 NUL。"""

    __slots__ = ("name", "props", "children", "parent")

    def __init__(self, name: str, parent: "Node | None" = None) -> None:
        self.name = name
        self.props: dict[str, bytes] = {}
        self.children: list[Node] = []
        self.parent = parent

    # ---- 读 ----
    def get(self, key: str) -> bytes | None:
        return self.props.get(key)

    def get_str(self, key: str, default: str | None = None) -> str | None:
        raw = self.props.get(key)
        if raw is None:
            return default
        return raw.rstrip(b"\x00").decode("utf-8", "replace")

    def get_u32(self, key: str) -> int | None:
        raw = self.props.get(key)
        if raw is None or len(raw) < 4:
            return None
        return struct.unpack_from(">I", raw)[0]

    def child(self, name: str) -> "Node | None":
        for node in self.children:
            if node.name == name:
                return node
        return None

    def walk(self):
        yield self
        for node in self.children:
            yield from node.walk()

    def path(self) -> str:
        parts: list[str] = []
        node: Node | None = self
        while node is not None and node.name != "":
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    # ---- 写 ----
    def set_bytes(self, key: str, value: bytes) -> None:
        self.props[key] = value

    def set_str(self, key: str, value: str) -> None:
        self.props[key] = value.encode("utf-8") + b"\x00"

    def set_u32(self, key: str, value: int) -> None:
        self.props[key] = struct.pack(">I", value)

    def set_strlist(self, key: str, values: list[str]) -> None:
        self.props[key] = b"".join(v.encode("utf-8") + b"\x00" for v in values)

    def del_prop(self, key: str) -> None:
        self.props.pop(key, None)

    def add_child(self, name: str) -> "Node":
        node = Node(name, self)
        self.children.append(node)
        return node

    def remove_child(self, name: str) -> None:
        self.children = [c for c in self.children if c.name != name]


class Dtb:
    def __init__(self, root: Node) -> None:
        self.root = root

    # ---- 解析 ----
    @classmethod
    def parse(cls, blob: bytes, offset: int = 0) -> "Dtb":
        if len(blob) < offset + 40:
            raise FdtError("数据太短, 不足 FDT 头")
        (magic, total, off_struct, off_strings, off_rsvmap, version,
         last_comp, _cpu, size_strings, size_struct) = struct.unpack_from(">10I", blob, offset)
        if magic != FDT_MAGIC:
            raise FdtError("FDT 魔数不对: 0x%08x" % magic)
        if total > len(blob) - offset:
            raise FdtError("FDT 声明长度 %d 超出可用数据 %d" % (total, len(blob) - offset))
        if version < 16 or last_comp > 17:
            raise FdtError("不支持的 FDT 版本 %d/%d" % (version, last_comp))

        strings = blob[offset + off_strings: offset + off_strings + size_strings]
        pos = offset + off_struct
        end = pos + size_struct
        root: Node | None = None
        cur: Node | None = None
        while pos < end:
            (token,) = struct.unpack_from(">I", blob, pos)
            pos += 4
            if token == FDT_BEGIN_NODE:
                nul = blob.index(b"\x00", pos)
                name = blob[pos:nul].decode("utf-8", "replace")
                pos = _align(nul + 1, 4)
                if root is None:
                    root = Node(name)
                    cur = root
                else:
                    if cur is None:
                        raise FdtError("节点嵌套错乱")
                    cur = cur.add_child(name)
            elif token == FDT_END_NODE:
                if cur is not None and cur.parent is not None:
                    cur = cur.parent
            elif token == FDT_PROP:
                length, nameoff = struct.unpack_from(">II", blob, pos)
                pos += 8
                value = blob[pos:pos + length]
                pos = _align(pos + length, 4)
                name = strings[nameoff:strings.index(b"\x00", nameoff)].decode("utf-8", "replace")
                if cur is None:
                    raise FdtError("属性出现在根节点之前")
                cur.props[name] = value
            elif token == FDT_NOP:
                continue
            elif token == FDT_END:
                break
            else:
                raise FdtError("非法 FDT token 0x%x @ %d" % (token, pos))
        if root is None:
            raise FdtError("没有找到根节点")
        return cls(root)

    # ---- 序列化 ----
    def serialize(self) -> bytes:
        strings: dict[str, int] = {}
        str_blob = bytearray()
        struct_blob = bytearray()

        def intern(name: str) -> int:
            if name not in strings:
                strings[name] = len(str_blob)
                str_blob.extend(name.encode("utf-8") + b"\x00")
            return strings[name]

        def emit(node: Node) -> None:
            struct_blob.extend(struct.pack(">I", FDT_BEGIN_NODE))
            struct_blob.extend(node.name.encode("utf-8") + b"\x00")
            struct_blob.extend(b"\x00" * (_align(len(struct_blob), 4) - len(struct_blob)))
            for key, value in node.props.items():
                struct_blob.extend(struct.pack(">III", FDT_PROP, len(value), intern(key)))
                struct_blob.extend(value)
                struct_blob.extend(b"\x00" * (_align(len(struct_blob), 4) - len(struct_blob)))
            for child in node.children:
                emit(child)
            struct_blob.extend(struct.pack(">I", FDT_END_NODE))

        emit(self.root)
        struct_blob.extend(struct.pack(">I", FDT_END))

        header_size = 40
        off_rsvmap = _align(header_size, 8)
        rsvmap = b"\x00" * 16
        off_struct = _align(off_rsvmap + len(rsvmap), 4)
        off_strings = off_struct + len(struct_blob)
        total = off_strings + len(str_blob)
        header = struct.pack(
            ">10I", FDT_MAGIC, total, off_struct, off_strings, off_rsvmap,
            17, 16, 0, len(str_blob), len(struct_blob),
        )
        blob = bytearray(header)
        blob.extend(b"\x00" * (off_rsvmap - len(blob)))
        blob.extend(rsvmap)
        blob.extend(b"\x00" * (off_struct - len(blob)))
        blob.extend(struct_blob)
        blob.extend(str_blob)
        assert len(blob) == total
        return bytes(blob)

    # ---- 查找/工具 ----
    def find(self, path: str) -> Node | None:
        node = self.root
        for part in [p for p in path.split("/") if p]:
            nxt = None
            for child in node.children:
                if child.name == part or child.name.split("@")[0] == part:
                    nxt = child
                    break
            if nxt is None:
                return None
            node = nxt
        return node

    def ensure(self, path: str) -> Node:
        node = self.root
        for part in [p for p in path.split("/") if p]:
            nxt = node.child(part)
            if nxt is None:
                nxt = node.add_child(part)
            node = nxt
        return node

    def next_phandle(self) -> int:
        used = 0
        for node in self.root.walk():
            for key in ("phandle", "linux,phandle"):
                value = node.get_u32(key)
                if value and value > used:
                    used = value
        return used + 1


class FitImage(dict):
    """解析出来的子镜像描述。"""


class Fit:
    """uImage.FIT 构建器。

    布局:  [FDT] [pad→4K] [img0] [pad→4K] [img1] ... 每段 4 KiB 对齐,
    rootfs 子镜像的 data-size 也补到 4 KiB 的整数倍 —— 内核 fitblk 驱动
    (drivers/block/fitblk.c) 会直接跳过未对齐的子镜像。
    """

    ALIGN = 4096

    @staticmethod
    def build(*, kernel: bytes, fdt: bytes, rootfs: bytes | None,
              kernel_compression: str = "lzma", description: str = "PT1570-R10",
              config_name: str = "config-1", timestamp: int | None = None) -> bytes:
        import hashlib

        def pad(data: bytes) -> bytes:
            return data + b"\x00" * (_align(len(data), Fit.ALIGN) - len(data))

        # U-Boot 的 fit_image_get_data() 只读 data-position/data-size(绝对偏移+精确长度),
        # 绝不认识自定义的 data 属性 —— 后者会被当成"内嵌数据", 长度只有 8 字节, 必然起不来。
        # kernel/fdt 用精确长度(LZMA/DTB 自己有结束标记); 只有 rootfs 补到 4 KiB,
        # 因为内核 fitblk 驱动按 4 KiB 块映射子镜像。
        # 各段起始位置统一 4 KiB 对齐(与 mkimage -E 及上游验证过的 itb 一致)。
        images: list[tuple[str, str, bytes, str, str]] = [
            # (节点名, type, 实际落盘数据, compression, description)
            ("kernel-1", "kernel", kernel, kernel_compression, "OpenWrt kernel"),
            ("fdt-1", "flat_dt", fdt, "none", "PT1570-R10 device tree"),
        ]
        if rootfs is not None:
            images.append(("rootfs-1", "filesystem", pad(rootfs), "none", "OpenWrt squashfs rootfs"))

        def make_tree(offsets: dict[str, tuple[int, int]]) -> Dtb:
            root = Node("")
            root.set_str("description", description)
            root.set_u32("#address-cells", 2)
            root.set_u32("#size-cells", 2)
            if timestamp is not None:
                root.set_u32("timestamp", timestamp)

            imgs = root.add_child("images")
            for name, kind, data, comp, desc in images:
                node = imgs.add_child(name)
                node.set_str("description", desc)
                pos, size = offsets[name]
                # U-Boot 标准属性: data-position 是从 FIT 起始算的绝对字节偏移,
                # data-size 是精确长度; 不能写成 data=<pos,size> 的内嵌数据。
                node.set_u32("data-position", pos)
                node.set_u32("data-size", size)
                node.set_str("type", kind)
                node.set_str("compression", comp)
                if kind == "kernel":
                    node.set_str("arch", "arm64")
                    node.set_str("os", "linux")
                elif kind == "flat_dt":
                    node.set_str("arch", "arm64")
                h = node.add_child("hash-1")
                h.set_str("algo", "sha256")
                h.set_bytes("value", hashlib.sha256(data).digest())

            cfgs = root.add_child("configurations")
            cfgs.set_str("default", config_name)
            cfg = cfgs.add_child(config_name)
            cfg.set_str("description", "PT1570-R10 FIT boot")
            cfg.set_str("kernel", "kernel-1")
            cfg.set_str("fdt", "fdt-1")
            if rootfs is not None:
                cfg.set_strlist("loadables", ["rootfs-1"])
            return Dtb(root)

        # 第一遍: 只为拿到 FDT 的实际长度(内容里的偏移不影响长度)
        dummy = {name: (0, 0) for name, *_ in images}
        fdt_len = len(make_tree(dummy).serialize())

        offsets: dict[str, tuple[int, int]] = {}
        cursor = _align(fdt_len, Fit.ALIGN)
        for name, _kind, data, _comp, _desc in images:
            offsets[name] = (cursor, len(data))
            cursor = _align(cursor + len(data), Fit.ALIGN)

        header_dtb = make_tree(offsets).serialize()
        if len(header_dtb) != fdt_len:
            raise FdtError("FIT 头长度不稳定: %d != %d" % (len(header_dtb), fdt_len))

        out = bytearray(header_dtb)
        for name, _kind, data, _comp, _desc in images:
            pos, _size = offsets[name]
            if len(out) < pos:
                out.extend(b"\x00" * (pos - len(out)))
            out.extend(data)
        return bytes(out)

    @staticmethod
    def parse(blob: bytes) -> dict:
        dtb = Dtb.parse(blob)
        if dtb.root.name != "":
            raise FdtError("FIT 根节点名不是空字符串")
        images_node = dtb.find("/images")
        if images_node is None:
            raise FdtError("FIT 缺少 /images")
        cfgs = dtb.find("/configurations")
        if cfgs is None:
            raise FdtError("FIT 缺少 /configurations")

        images: dict[str, dict] = {}
        for node in images_node.children:
            # 只认 U-Boot 的 data-position/data-size。旧版打包器写的是 8 字节
            # data 属性, U-Boot 会当成内嵌数据直接加载 —— 这里必须拒绝, 不能兼容。
            if node.get("data") is not None:
                raise FdtError("子镜像 %s 用了非标准 data 属性(应为 data-position/data-size)" % node.name)
            pos_raw, size_raw = node.get("data-position"), node.get("data-size")
            if pos_raw is None or len(pos_raw) != 4 or size_raw is None or len(size_raw) != 4:
                raise FdtError("子镜像 %s 缺少 data-position/data-size" % node.name)
            pos, size = struct.unpack(">II", pos_raw + size_raw)
            images[node.name] = {
                "type": node.get_str("type", ""),
                "compression": node.get_str("compression", "none"),
                "position": pos,
                "size": size,
                "node": node,
            }

        default = cfgs.get_str("default", "config-1")
        cfg = cfgs.child(default)
        if cfg is None:
            raise FdtError("找不到默认配置 %s" % (default,))
        loadables = [x for x in cfg.get_str("loadables", "").split("\x00") if x] \
            if cfg.get("loadables") else []

        return {
            "description": dtb.root.get_str("description", ""),
            "images": images,
            "default_config": default,
            "config": {
                "kernel": cfg.get_str("kernel", ""),
                "fdt": cfg.get_str("fdt", ""),
                "loadables": loadables,
            },
            "dtb": dtb,
        }


def image_check(blob: bytes) -> None:
    if blob[:4] != SQUASHFS_MAGIC:
        raise ValueError("不是 SquashFS 4.0 镜像")
    if len(blob) < 96:
        raise ValueError("SquashFS 太短")


def kernel_check(blob: bytes) -> None:
    if len(blob) < 0x40 or blob[0x38:0x3C] != IMAGE_MAGIC:
        raise ValueError("不是 arm64 Image（缺少 ARM\\x64 魔数）")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: fitimg.py info <file.itb> | dtb <file.dtb> | tree <file.itb>")
        return 2
    cmd, path = argv[0], argv[1]
    blob = open(path, "rb").read()
    if cmd == "dtb":
        trace(Dtb.parse(blob).root, 0)
        return 0
    info = Fit.parse(blob)
    print("description: %s" % info["description"])
    for name, img in info["images"].items():
        print("  %-10s type=%-10s comp=%-5s pos=%-9d size=%-9d"
              % (name, img["type"], img["compression"], img["position"], img["size"]))
    cfg = info["config"]
    print("config %s: kernel=%s fdt=%s loadables=%s"
          % (info["default_config"], cfg["kernel"], cfg["fdt"], cfg["loadables"]))
    if cmd == "tree":
        trace(info["dtb"].root, 0)
    return 0


def trace(node: Node, depth: int) -> None:
    head = "  " * depth + (node.name or "/")
    extra = []
    for key in ("volname", "compatible", "bootargs", "u-boot,version", "type", "compression"):
        value = node.get_str(key)
        if value:
            extra.append("%s=%r" % (key, value))
    if "phandle" in node.props:
        extra.append("phandle=%d" % node.get_u32("phandle"))
    print(head + ("  " + " ".join(extra) if extra else ""))
    for child in node.children:
        trace(child, depth + 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
