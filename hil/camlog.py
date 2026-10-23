"""摄像头插拔台的日志匹配与单轮分类（不碰串口，可以喂实机日志做单元测试）。

被测板不断电，只有 UVC 摄像头的供电被电源开关。所以这里关心的是主板对摄像头热插拔的反应：
有没有检出掉线、有没有整机重启、上电后摄像头有没有重新出流。

**所有和固件日志措辞相关的规则都集中在 RULES / CRASH_RESET 里。** 固件改了措辞只改这里，
再用新的实机日志补 ``tests/fixtures/cam_*.log``。规则基于 fish-tank-esp32 主固件 2.4.2.5：
``main/camera/uvc_camera.cc``、``main/usb/usb_device_inventory.cc``、
``main/boards/mhpsy/smart-fishtank/smart_fishtank_board.cc``（TickMediaRecovery）。

恢复成功的主判据是“上电后摄像头重新出流”（``UvcCamera: stream started``），其余事件只记录。

单轮日志格式（工具自己写的 ``logs/round-NNNN.log``，也是夹具格式）：每行 ``[  秒] 原始行``，
秒是主机时间、相对本轮开始；``===== PSU OFF =====`` / ``===== PSU ON =====`` 标记电源动作
（命令发出前那一刻）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ANSI = re.compile(r"\x1b\[[0-9;]*m")
ESP_MS = re.compile(r"^[IWEDV] \((\d+)\) ")

# (事件, 正则)。对去掉 ANSI 颜色后的整行做 re.search，按顺序取第一条命中的。
RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    # 崩溃类：panic、abort、任务看门狗、中断看门狗、栈溢出、断言、堆损坏
    ("crash", r"Guru Meditation|abort\(\) was called|Task watchdog got triggered"
              r"|Interrupt wdt timeout|[Ss]tack overflow|assert failed|CORRUPT HEAP"
              r"|heap integrity CORRUPTED"),
    # 整机重启：ROM 横幅和复位原因（复位原因见 CRASH_RESET）
    ("rom", r"^ESP-ROM:"),
    ("reset_reason", r"^rst:0x([0-9a-f]+) \((\w+)\)"),
    ("app_version", r"app_init: App version:\s+(\S+)"),
    # 固件的受控恢复：重启请求（第 n/m 次）、开机时打印的已用预算、预算耗尽
    ("restart_req", r"controlled whole-device restart for USB camera recovery \((\d+)/(\d+)\)"),
    ("rebooting", r"Application: Rebooting"),
    ("budget", r"USB camera recovery budget used=(\d+)/(\d+)"),
    ("budget_exhausted", r"USB camera recovery budget exhausted"),
    ("budget_reset", r"USB camera recovery budget reset"),
    # 掉线迹象（断电后第一条算“检出掉线”）
    ("usb_err", r"uvc-isoc: usb err"),
    ("urb_err", r"USB HOST: Enqueue URB error"),
    ("teardown_unsafe", r"UvcCamera: USB teardown unsafe"),
    ("cam_offline", r"camera safe offline"),
    ("usb_detach", r"UsbDeviceInventory: USB detach"),
    # 稳定性改造（E1 起）：断开时关流，不再锁存重启
    ("cam_gone_close_failed", r"camera gone: stream_close.*returned"),
    ("cam_gone", r"UvcCamera: camera gone: stream closed \((\w[\w-]*)"),
    ("cam_gone_deferred", r"UvcCamera: camera gone while stream busy"),
    ("heap_ok", r"camera gone: heap integrity ok"),
    # 周期状态（E1 起，每 5 s）：断电前确认在出流
    ("camstat_up", r"CamStat: streaming=1 "),
    ("camstat_down", r"CamStat: streaming=0 "),
    # 重新出现与出流
    ("usb_attach", r"UsbDeviceInventory: USB attach addr=(\d+) vid:pid=([0-9a-f]{4}:[0-9a-f]{4})"),
    ("uvc_connected", r"UvcCamera: UVC connected: dev_addr=(\d+)"),
    ("stream_stopped", r"UvcCamera: stream stopped"),
    ("stream_started", r"UvcCamera: stream started"),
    ("publish", r"Rtmps: secure publish connected"),
]]

# 复位原因名里带这些字的算崩溃（看门狗、掉电、CPU 锁死）；SW_CPU_RESET 等受控重启不算
CRASH_RESET = re.compile(r"WDT|BROWN|LOCKUP")

DROP_KINDS = {"usb_err", "urb_err", "teardown_unsafe", "cam_offline", "usb_detach", "cam_gone",
              "cam_gone_deferred", "cam_gone_close_failed"}
BOOT_KINDS = {"rom", "reset_reason"}
# 断电前确认用：这些事件之后认为摄像头不在出流。零星的 usb err 不算（正常出流时也偶尔有一条）；
# stream_stopped 在切分辨率时会先停再起，起来之后又回到 streaming
STATE_DOWN_KINDS = {"teardown_unsafe", "cam_offline", "usb_detach", "budget_exhausted", "crash",
                    "rebooting", "restart_req", "stream_stopped", "cam_gone", "cam_gone_deferred",
                    "cam_gone_close_failed", "camstat_down"} | BOOT_KINDS

MARK_OFF = "===== PSU OFF ====="
MARK_ON = "===== PSU ON ====="
MARK_SERIAL_LOST = "===== 串口断开 ====="
LOG_LINE = re.compile(r"^\[\s*(-?[\d.]+)\] ?(.*)$")


@dataclass
class Event:
    t: float            # 主机时间（秒）
    kind: str
    line: str
    groups: tuple = ()


def match_line(raw: str) -> tuple[str, tuple] | None:
    line = ANSI.sub("", raw).rstrip("\r")
    for kind, pat in RULES:
        if m := pat.search(line):
            return kind, m.groups()
    return None


def clean(raw: str) -> str:
    return ANSI.sub("", raw).rstrip("\r")


@dataclass
class CameraState:
    """跨轮跟踪摄像头是否在出流，用于每轮断电前确认。初始 unknown（出流后固件没有周期日志）。"""

    state: str = "unknown"          # unknown / streaming / down
    since: float | None = None      # 进入当前状态的主机时间
    evidence: str = ""

    def feed(self, t: float, raw: str) -> None:
        hit = match_line(raw)
        if hit is None:
            if raw.startswith(MARK_SERIAL_LOST):
                self._set("down", t, raw)
            return
        kind = hit[0]
        if kind in ("stream_started", "camstat_up"):
            self._set("streaming", t, raw)
        elif kind in STATE_DOWN_KINDS:
            self._set("down", t, raw)

    def _set(self, state: str, t: float, raw: str) -> None:
        if state != self.state:
            self.since = t
        self.state = state
        self.evidence = clean(raw).strip()


def parse_round_log(text: str) -> tuple[list[tuple[float, str]], float | None, float | None]:
    """解析单轮日志，返回 ([(秒, 行)], 断电时刻, 上电时刻)。"""
    lines: list[tuple[float, str]] = []
    t_off = t_on = None
    for raw in text.splitlines():
        m = LOG_LINE.match(raw)
        if not m:
            continue
        t, line = float(m.group(1)), m.group(2)
        if line.startswith(MARK_OFF):
            t_off = t
        elif line.startswith(MARK_ON):
            t_on = t
        lines.append((t, line))
    return lines, t_off, t_on


def format_line(t: float, line: str) -> str:
    return f"[{t:9.3f}] {line}"


def _ms(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else round((a - b) * 1000)


def analyze(lines: list[tuple[float, str]], t_off: float, t_on: float, vidpid: str) -> dict:
    """按断电/上电时刻给一轮分类，返回 rounds.csv 的一行（不含 round/started/log 等）。

    result:
      crash      出现崩溃类日志，或复位原因是看门狗/掉电
      rebooted   断电后主板整机重启（ROM 横幅）；stream_ms 有值表示重启后恢复出流
      recovered  没重启，上电后摄像头重新出流
      offline    on-wait 内没恢复出流（含恢复预算耗尽）
      rig_fault  台架问题：断电后串口没有任何数据、断电期间仍在出流（摄像头没断电）
    """
    vidpid = vidpid.lower()
    ev: list[Event] = []
    serial_lost = 0
    after_off = 0
    for t, raw in lines:
        if t < t_off:
            continue
        line = clean(raw)
        if line.startswith(MARK_SERIAL_LOST):
            serial_lost += 1
            continue
        if line.startswith("====="):
            continue
        after_off += 1
        hit = match_line(line)
        if hit:
            ev.append(Event(t, hit[0], line.strip(), hit[1]))

    def first(kind: str, since: float = t_off, pred=None) -> Event | None:
        return next((e for e in ev if e.kind == kind and e.t >= since and (pred is None or pred(e))),
                    None)

    def count(kind: str) -> int:
        return sum(e.kind == kind for e in ev)

    drop = next((e for e in ev if e.kind in DROP_KINDS), None)
    boot = next((e for e in ev if e.kind in BOOT_KINDS), None)
    reason = first("reset_reason")
    crash_ev = first("crash")
    restart = first("restart_req")
    budget = [e for e in ev if e.kind == "budget"]
    version = first("app_version")
    attach = first("usb_attach", t_on, lambda e: e.groups[1] == vidpid)
    uvc = first("uvc_connected", t_on)
    stream = first("stream_started", t_on)
    publish = first("publish", t_on)
    stream_while_off = first("stream_started", t_off, lambda e: e.t < t_on)

    row = {
        "drop_ms": _ms(drop.t, t_off) if drop else None,
        "reboot_ms": _ms(boot.t, t_off) if boot else None,
        "attach_ms": _ms(attach.t, t_on) if attach else None,
        "uvc_ms": _ms(uvc.t, t_on) if uvc else None,
        "stream_ms": _ms(stream.t, t_on) if stream else None,
        "publish_ms": _ms(publish.t, t_on) if publish else None,
        "usb_err": count("usb_err"),
        "urb_err": count("urb_err"),
        "teardown_unsafe": count("teardown_unsafe"),
        "restart_req": "/".join(restart.groups) if restart else "",
        "budget_boot": "/".join(budget[-1].groups) if budget else "",
        "budget_exhausted": count("budget_exhausted"),
        "reset_reason": f"0x{reason.groups[0]} {reason.groups[1]}" if reason else "",
        "reboots": count("rom"),
        "stream_starts": sum(e.kind == "stream_started" and e.t >= t_on for e in ev),
        "fw": version.groups[0] if version else "",
        "serial_lost": serial_lost,
        "crash": crash_ev.line[:160] if crash_ev else "",
        "events": ";".join(f"{e.kind}@{_ms(e.t, t_off)}" for e in _dedup(ev))[:400],
    }

    if crash_ev or (reason and CRASH_RESET.search(reason.groups[1])):
        detail = crash_ev.line[:120] if crash_ev else f"复位原因 {row['reset_reason']}"
        return {**row, "result": "crash", "detail": detail}
    if after_off == 0:
        return {**row, "result": "rig_fault", "detail": "断电后板载串口没有任何数据"}
    if stream_while_off:
        return {**row, "result": "rig_fault", "detail": "断电期间仍看到出流，摄像头可能不由电源供电"}
    if boot:
        why = row["reset_reason"] or "没抓到复位原因行"
        if restart:
            why += f"，受控重启 {row['restart_req']}"
        back = f"重启后恢复出流 {row['stream_ms']}ms" if stream else "重启后未恢复出流"
        return {**row, "result": "rebooted", "detail": f"{why}；{back}"}
    if stream:
        return {**row, "result": "recovered", "detail": ""}
    if row["budget_exhausted"]:
        return {**row, "result": "offline", "detail": "恢复预算耗尽，摄像头保持离线"}
    if drop is None:
        return {**row, "result": "offline", "detail": "未检出掉线事件，上电后也没重新出流"}
    return {**row, "result": "offline", "detail": "on-wait 内没重新出流"}


def _dedup(ev: list[Event]) -> list[Event]:
    """事件摘要：连续同类事件只留第一条（usb err 会一口气刷几十行）。"""
    out: list[Event] = []
    for e in ev:
        if not out or out[-1].kind != e.kind:
            out.append(e)
    return out


@dataclass
class RoundLog:
    """一轮的原始行，带主机时间。"""

    t0: float
    lines: list[tuple[float, str]] = field(default_factory=list)

    def add(self, t: float, line: str) -> None:
        self.lines.append((t, line))

    def text(self) -> str:
        return "".join(format_line(t - self.t0, l) + "\n" for t, l in self.lines)
