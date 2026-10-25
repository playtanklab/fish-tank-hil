"""推流故障注入代理：透明转发设备的 RTMPS(TLS 不解开)，按命令制造“链路卡住”和“连接被重置”。

用途：在没有 root、不碰路由器的前提下复现 Wi-Fi 抖动对推流的影响。
设备连 ``listen``，代理转发到 ``upstream``(MediaMTX 的 RTMPS 端口)。

故障：
- ``stall N``：N 秒内两个方向都不读不写。设备的发送缓冲很快填满，``esp_rtmp_push_video`` 阻塞，
  等价于 2.4 GHz 上的一次长停顿；超过固件发送期限(2 s)时固件主动断开重连。
- ``reset``：对所有现存连接发 RST(SO_LINGER=0)，等价于 NAT 表项丢失 / AP 切换。
- ``blackhole N``：N 秒内新连接一律 RST，现存连接不动；模拟“重连时网络还没恢复”。

控制口只听 127.0.0.1，一行一条命令，回一行 JSON：``stall 3`` / ``reset`` / ``blackhole 5`` / ``stats``。

    python -m hil tcpchaos --listen 0.0.0.0:47936 --upstream 127.0.0.1:47937 --control 127.0.0.1:47940
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time


RCVBUF = 32 * 1024


def _split(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host, int(port)


class Chaos:
    def __init__(self, upstream: tuple[str, int]):
        self.upstream = upstream
        self.resume = asyncio.Event()
        self.resume.set()
        self.blackhole_until = 0.0
        self.conns: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self.stats = {"accepted": 0, "refused": 0, "stalls": 0, "resets": 0, "bytes_up": 0, "bytes_down": 0}

    async def _pump(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, key: str) -> None:
        try:
            while True:
                await self.resume.wait()
                data = await r.read(8192)
                if not data:
                    break
                await self.resume.wait()
                w.write(data)
                await w.drain()
                self.stats[key] += len(data)
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    async def handle(self, dr: asyncio.StreamReader, dw: asyncio.StreamWriter) -> None:
        if time.monotonic() < self.blackhole_until:
            self.stats["refused"] += 1
            _rst(dw)
            return
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 5)
        except Exception:
            _rst(dw)
            return
        self.stats["accepted"] += 1
        pair = (dw, uw)
        self.conns.add(pair)
        try:
            await asyncio.gather(self._pump(dr, uw, "bytes_up"), self._pump(ur, dw, "bytes_down"))
        finally:
            self.conns.discard(pair)

    async def stall(self, seconds: float) -> None:
        self.stats["stalls"] += 1
        self.resume.clear()
        await asyncio.sleep(seconds)
        self.resume.set()

    def reset(self) -> int:
        n = 0
        for dw, uw in list(self.conns):
            _rst(dw)
            _rst(uw)
            n += 1
        self.stats["resets"] += 1
        return n

    async def control(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while line := (await r.readline()).decode().strip():
                cmd, *arg = line.split()
                if cmd == "stall":
                    asyncio.ensure_future(self.stall(float(arg[0])))
                    out = {"ok": True, "stall_s": float(arg[0]), "conns": len(self.conns)}
                elif cmd == "reset":
                    out = {"ok": True, "reset": self.reset()}
                elif cmd == "blackhole":
                    self.blackhole_until = time.monotonic() + float(arg[0])
                    out = {"ok": True, "blackhole_s": float(arg[0])}
                elif cmd == "stats":
                    out = {"ok": True, "conns": len(self.conns), **self.stats}
                else:
                    out = {"ok": False, "error": f"unknown {cmd}"}
                w.write((json.dumps(out) + "\n").encode())
                await w.drain()
        finally:
            w.close()


def _rst(w: asyncio.StreamWriter) -> None:
    sock = w.get_extra_info("socket")
    try:
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        w.transport.abort()
    except Exception:
        pass


async def serve(listen: str, upstream: str, control: str) -> None:
    chaos = Chaos(_split(upstream))
    # 接收缓冲要小:默认 Linux 会替设备收下好几 MB,stall 期间设备根本感觉不到卡。
    # SO_RCVBUF 必须在 listen 之前设,窗口缩放才按它协商。
    host, port = _split(listen)
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    ls.bind((host, port))
    ls.listen(16)
    srv = await asyncio.start_server(chaos.handle, sock=ls, limit=RCVBUF)
    ctl = await asyncio.start_server(chaos.control, *_split(control))
    print(f"tcpchaos {listen} -> {upstream}，控制口 {control}", flush=True)
    async with srv, ctl:
        await asyncio.gather(srv.serve_forever(), ctl.serve_forever())


def send_command(control: str, line: str, timeout: float = 3.0) -> dict:
    """给运行中的代理发一条命令(同步版，压测脚本里用)。"""
    with socket.create_connection(_split(control), timeout=timeout) as s:
        s.sendall((line + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")
