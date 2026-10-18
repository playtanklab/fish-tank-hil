"""从 P4 串口日志还原 CH370 那棵 USB 树。

只依赖固件 `usb_tree_ch370.cc` 现有的日志格式（fish-tank-esp32 main@cb539d0f）。
日志里的 ``hub#N`` 是该 hub 的 USB 地址，所以能把每个设备还原成端口路径：
根口设备是 ``root``，挂在根 hub 端口 6 上的是 ``root.6``，再往下是 ``root.6.2``。
固件改了日志格式，这里要一起改，测试夹具来自实机抓取。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG = re.compile(r"^[IWEDV] \((\d+)\) ([^:]+): (.*)$")
DEV = r"addr=(\d+) ([0-9a-f]{4}):([0-9a-f]{4}) (<HUB> )?'(.*)'"
RE_ONLINE = re.compile(r"^CH370 online")
RE_ROOT_DEV = re.compile(r"^CH370 根口设备: " + DEV)
RE_COLD = re.compile(r"^\s*hub#(\d+) 端口(\d+): " + DEV)
RE_HOT = re.compile(r"^热插入: hub#(\d+) 端口(\d+) → " + DEV)
RE_UNPLUG = re.compile(r"^热拔出: hub#(\d+) 端口(\d+)")
RE_LATCH = re.compile(r"^hub#(\d+) 端口(\d+) 变化锁存.*先摘除原设备")
RE_HUB_DEAD = re.compile(r"^hub#(\d+) 连续 \d+ 轮全端口读失败")
# V2 兜底:端口连续失败 → 复位所在 hub(固件路径 r / r.6 …),整个 hub 子树先摘除再重新枚举
RE_ESCALATE = re.compile(r"^enum_escalate path=\S+ .*复位所在 hub (r(?:\.\d+)*)")
RE_ROOT_GONE = re.compile(r"^CH370 根口 attach: \d+ -> 0|全量重建")
RE_ENUM_FAIL = re.compile(r"^hub#(\d+) 端口(\d+) 复位/枚举失败")
RE_ROOT_FAIL = re.compile(r"^根口(设备)?枚举失败")
RE_DIAG_FAIL = re.compile(
    r"^enum_fail (\w+) path=(\S+) hub#(\d+) p(\d+) try=(\d+) step=(\w+) stage=(\S) "
    r"resp=(-?\d+) naks=(\d+) tog=(\d+) pst=0x([0-9a-f]+)")
RE_DIAG_WARN = re.compile(r"^enum_warn path=(\S+) (.*)$")
RE_HUB_INFO = re.compile(
    r"^hub path=(\S+) nports=(\d+) chars=0x([0-9a-f]+) power=(\S+) pwrgood=(\d+)ms")
RE_ROM = re.compile(r"ESP-ROM:|rst:0x[0-9a-f]+")
# FishtankRegistry:模块 IDENTIFY 单次失败 / 重试用尽(放弃准入 = 模块在平台上不可用)
RE_ID_FAIL = re.compile(r"^ch370 identify cb failed st=(\d+) addr=(\d+)")
RE_ID_GIVEUP = re.compile(r"^identify timeout: dev=\S+ addr=(\d+)")
RESET_DROP_MS = 1000


@dataclass
class Device:
    path: str
    addr: int
    vidpid: str
    name: str
    is_hub: bool
    seen_ms: int
    how: str  # "cold" 冷枚举 / "hot" 增量扫描补上


@dataclass
class Ch370Tree:
    devices: dict[str, Device] = field(default_factory=dict)  # path -> Device
    online_ms: int | None = None
    first_ms: int | None = None
    last_ms: int | None = None
    enum_fail_lines: int = 0
    escalations: int = 0        # 兜底复位 hub 次数(会让兄弟设备短暂掉线)
    identify_fails: int = 0
    identify_giveups: list[int] = field(default_factory=list)   # 放弃准入的 USB 地址
    diag_fails: list[dict] = field(default_factory=list)  # 固件 P0 起的结构化 enum_fail 行
    diag_warns: list[str] = field(default_factory=list)
    hubs: dict[str, dict] = field(default_factory=dict)   # 路径 → nports/power/pwrgood
    resets: list[str] = field(default_factory=list)  # 抓取中途出现的复位迹象
    _addr_path: dict[int, str] = field(default_factory=dict)
    _partial: bytes = b""

    # ---- 输入 ----

    def feed(self, data: bytes) -> None:
        # 串口一次读到的数据可能切在中文字符中间，凑满一行再解码
        *lines, self._partial = (self._partial + data).split(b"\n")
        for line in lines:
            self.feed_line(line.decode("utf-8", errors="replace"))

    def feed_line(self, raw: str) -> None:
        line = ANSI.sub("", raw).rstrip("\r")
        if RE_ROM.search(line):
            if self.first_ms is not None:
                self.resets.append(line.strip())
            return
        m = LOG.match(line)
        if not m:
            return
        ms, msg = int(m.group(1)), m.group(3)
        # 不同任务的日志先取时间戳再排队打印，几毫秒的回退是正常交错；
        # 只有回退超过 RESET_DROP_MS 才当作重新开机
        if self.last_ms is not None and ms < self.last_ms - RESET_DROP_MS:
            self.resets.append(f"时间戳回退 {self.last_ms} -> {ms}")
        if self.first_ms is None:
            self.first_ms = ms
        self.last_ms = ms if self.last_ms is None else max(self.last_ms, ms)
        if m.group(2) == "FishtankRegistry":
            if RE_ID_FAIL.match(msg):
                self.identify_fails += 1
            elif g := RE_ID_GIVEUP.match(msg):
                self.identify_giveups.append(int(g.group(1)))
            return
        if m.group(2) != "UsbTreeCh370":
            return
        self._usb_tree(ms, msg)

    def _usb_tree(self, ms: int, msg: str) -> None:
        if RE_ONLINE.match(msg):
            if self.online_ms is None:
                self.online_ms = ms
            return
        if RE_ROOT_GONE.search(msg):
            self.devices.clear()
            self._addr_path.clear()
            return
        if m := RE_ROOT_DEV.match(msg):
            self.devices.clear()
            self._addr_path.clear()
            self._add("root", m.groups(), ms, "cold")
            return
        if m := RE_COLD.match(msg):
            self._add_child(int(m.group(1)), m.group(2), m.groups()[2:], ms, "cold")
            return
        if m := RE_HOT.match(msg):
            self._add_child(int(m.group(1)), m.group(2), m.groups()[2:], ms, "hot")
            return
        if m := (RE_UNPLUG.match(msg) or RE_LATCH.match(msg)):
            parent = self._addr_path.get(int(m.group(1)))
            if parent:
                self._remove(f"{parent}.{m.group(2)}")
            return
        if m := RE_ESCALATE.match(msg):
            self.escalations += 1
            self._remove("root" + m.group(1)[1:])
            return
        if m := RE_HUB_DEAD.match(msg):
            path = self._addr_path.get(int(m.group(1)))
            if path:
                self._remove(path)
            return
        if RE_ENUM_FAIL.match(msg) or RE_ROOT_FAIL.match(msg):
            self.enum_fail_lines += 1
            return
        if m := RE_DIAG_FAIL.match(msg):
            keys = ("phase", "path", "hub", "port", "try", "step", "stage", "resp", "naks", "tog")
            d = dict(zip(keys, m.groups()[:10]))
            d["pst"] = int(m.group(11), 16)
            d["ms"] = ms
            self.diag_fails.append(d)
            return
        if m := RE_DIAG_WARN.match(msg):
            self.diag_warns.append(f"{m.group(1)} {m.group(2)}")
            return
        if m := RE_HUB_INFO.match(msg):
            self.hubs[m.group(1)] = {"nports": int(m.group(2)), "chars": int(m.group(3), 16),
                                     "power": m.group(4), "pwrgood_ms": int(m.group(5))}

    def _add_child(self, hub_addr: int, port: str, groups, ms: int, how: str) -> None:
        parent = self._addr_path.get(hub_addr, f"?hub{hub_addr}")
        self._add(f"{parent}.{port}", groups, ms, how)

    def _add(self, path: str, groups, ms: int, how: str) -> None:
        addr, vid, pid, hub, name = groups
        self._remove(path)
        dev = Device(path, int(addr), f"{vid}:{pid}", name, hub is not None, ms, how)
        self.devices[path] = dev
        self._addr_path[dev.addr] = path

    def _remove(self, path: str) -> None:
        for p in [p for p in self.devices if p == path or p.startswith(path + ".")]:
            dev = self.devices.pop(p)
            if self._addr_path.get(dev.addr) == p:
                del self._addr_path[dev.addr]


@dataclass(frozen=True)
class Expected:
    path: str
    vidpid: str
    name: str = ""


def compare(tree: Ch370Tree, expected: list[Expected]):
    """返回 (缺失的期望设备, 多出来的设备, 期望设备里最后一个出现的时刻 ms)。"""
    missing, last_ms = [], None
    for e in expected:
        dev = tree.devices.get(e.path)
        if dev is None or dev.vidpid != e.vidpid:
            missing.append(e)
        elif last_ms is None or dev.seen_ms > last_ms:
            last_ms = dev.seen_ms
    want = {e.path for e in expected}
    extra = [d for p, d in tree.devices.items() if p not in want]
    return missing, extra, last_ms
