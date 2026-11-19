#!/usr/bin/env python3
"""最小 TFTP 服务端(RRQ), 给原厂 U-Boot 的 tftpboot 用。

    python tools/tftp_serve.py out/pt1570-r10-0.8.5-pt1570

默认监听 0.0.0.0:69, 只提供指定目录内的文件, 不做写入/删除。
支持 blksize/tsize 选项(U-Boot 会协商 1468 字节块, 否则退回 512)。
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

OP_RRQ = 1
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

ERROR_TEXT = {
    0: "未定义错误",
    1: "文件不存在",
    2: "访问被拒绝",
    4: "非法操作",
    6: "文件已存在",
}


def parse_request(payload: bytes) -> tuple[int, str, str, dict[str, str]]:
    opcode = struct.unpack_from(">H", payload, 0)[0]
    parts = payload[2:].split(b"\x00")
    fields = [p.decode("latin-1") for p in parts if p]
    if len(fields) < 2:
        raise ValueError("请求格式错误")
    filename, mode = fields[0], fields[1].lower()
    options: dict[str, str] = {}
    for index in range(2, len(fields) - 1, 2):
        options[fields[index].lower()] = fields[index + 1]
    return opcode, filename, mode, options


def send_error(sock: socket.socket, address: tuple[str, int], code: int) -> None:
    sock.sendto(struct.pack(">HH", OP_ERROR, code) + ERROR_TEXT.get(code, "错误").encode() + b"\x00",
                address)


def serve_file(root: Path, filename: str, options: dict[str, str], address: tuple[str, int],
               verbose: bool) -> None:
    target = (root / filename.lstrip("/")).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        print("拒绝越界路径: %s" % filename, file=sys.stderr)
        return
    if not target.is_file():
        print("未找到: %s" % filename, file=sys.stderr)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as error_sock:
            send_error(error_sock, address, 1)
        return

    blksize = 512
    if "blksize" in options:
        try:
            blksize = max(8, min(1468, int(options["blksize"])))
        except ValueError:
            blksize = 512
    size = target.stat().st_size
    print("传送 %-28s %10d 字节 -> %s:%d (块 %d)" % (filename, size, address[0], address[1], blksize))

    payload = target.read_bytes()
    total = (len(payload) + blksize - 1) // blksize
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", 0))
        sock.settimeout(4.0)

        if any(key in options for key in ("blksize", "tsize", "timeout")):
            oack = struct.pack(">H", OP_OACK)
            oack += b"blksize\x00%d\x00" % blksize
            if "tsize" in options:
                oack += b"tsize\x00%d\x00" % size
            sock.sendto(oack, address)
            try:
                data, peer = sock.recvfrom(2048)
            except socket.timeout:
                print("等待 OACK 确认超时: %s" % filename, file=sys.stderr)
                return
            if struct.unpack_from(">H", data, 0)[0] == OP_ERROR:
                print("%s: U-Boot 拒绝了协商参数" % filename, file=sys.stderr)
                return
            address = peer

        block = 0
        sent = 0
        started = time.time()
        while True:
            start = block * blksize
            chunk = payload[start:start + blksize]
            packet = struct.pack(">HH", OP_DATA, (block + 1) & 0xFFFF) + chunk
            for attempt in range(5):
                sock.sendto(packet, address)
                try:
                    while True:
                        data, peer = sock.recvfrom(1024)
                        opcode = struct.unpack_from(">H", data, 0)[0]
                        if opcode == OP_ACK and struct.unpack_from(">H", data, 2)[0] == (block + 1) & 0xFFFF:
                            address = peer
                            break
                        if opcode == OP_ERROR:
                            print("U-Boot 返回错误, 中止 %s" % filename, file=sys.stderr)
                            return
                except socket.timeout:
                    continue
                break
            else:
                print("重传超时, 中止 %s" % filename, file=sys.stderr)
                return

            sent += len(chunk)
            block += 1
            if verbose and total and (block % 512 == 0 or len(chunk) < blksize):
                percent = sent * 100 // max(len(payload), 1)
                print("  %5.1f%%  %d/%d" % (percent, sent, len(payload)))
            if len(chunk) < blksize:
                break
        elapsed = max(time.time() - started, 0.001)
        print("完成 %-28s %.1fs  %.2f MiB/s" % (filename, elapsed, len(payload) / elapsed / 1048576))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="TFTP 根目录(刷机包目录)")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=69)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print("目录不存在: %s" % root, file=sys.stderr)
        return 2
    print("TFTP 根目录: %s" % root)
    print("监听 %s:%d, Ctrl+C 结束" % (args.bind, args.port))
    print("U-Boot 侧: setenv ipaddr 192.168.1.2; setenv serverip 192.168.1.7")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((args.bind, args.port))
        except OSError as error:
            print("绑定失败: %s (端口 %d 可能被占用)" % (error, args.port), file=sys.stderr)
            return 1
        try:
            while True:
                data, address = server.recvfrom(2048)
                try:
                    opcode, filename, _mode, options = parse_request(data)
                except ValueError:
                    continue
                if opcode != OP_RRQ:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as error_sock:
                        send_error(error_sock, address, 4)
                    continue
                try:
                    serve_file(root, filename, options, address, not args.quiet)
                except Exception as error:  # noqa: BLE001 - 单个请求失败不应终止服务
                    print("处理 %s 失败: %s" % (filename, error), file=sys.stderr)
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
