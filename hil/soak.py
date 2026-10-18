"""长跑（方案 §6 S9）：冷启动一次后保持供电，被动读串口若干小时，判定

- 零漏设备：收敛后任何时刻期望设备都在；
- 零幽灵设备：收敛后没有多出来的设备，也没有拔出/插入事件（无人值守时拓扑不该变）；
- 地址池无泄漏：没有 "USB 地址用尽"，收敛后没有新地址出现；
- 顺带记录：P4 复位、串口掉线、枚举失败、模块收发失败、IDENTIFY 失败、CPU 统计。

解析（SoakMonitor）不碰串口，可以喂实机日志做单元测试；run_soak 负责电源和串口。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .ch370_log import ANSI, LOG, Ch370Tree, Expected, compare

RE_BUS_ATTACH = re.compile(r"^ch370 attach: addr=(\d+) vid=([0-9a-f]{4}) pid=([0-9a-f]{4})")
RE_BUS_DETACH = re.compile(r"^ch370 detach: addr=(\d+)")
RE_XFER_FAIL = re.compile(r"^xfer_fail addr=(\d+) cmd=0x([0-9a-f]+) st=(\S+)")
RE_CPU = re.compile(r"^win=\d+s ch370_bus=([\d.]+)%")
RE_ADDR_OUT = re.compile(r"USB 地址用尽")
# 只记不判:芯片复位/聋态、hub 假死、过流、升级复位都应该在静态长跑里为 0
RE_TREE_WARN = re.compile(r"chip_reset|chip_deaf|假死|过流|enum_escalate|enum_unsupported|全量重建")


@dataclass
class SoakMonitor:
    expected: list[Expected]
    tree: Ch370Tree = field(default_factory=Ch370Tree)
    converged_ms: int | None = None          # 期望设备首次全部到齐的设备时间
    converged_addrs: set[int] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)   # 收敛后的异常事件
    xfer_fails: int = 0
    tree_warns: int = 0
    addr_exhausted: int = 0
    attaches: int = 0
    published: set[int] = field(default_factory=set)   # 已向注册表发布过的地址
    detaches: int = 0
    cpu_ch370: list[float] = field(default_factory=list)
    _seen_resets: int = 0
    _snapshot: list | None = None
    _partial: bytes = b""

    def feed(self, data: bytes, wall: str = "") -> None:
        *lines, self._partial = (self._partial + data).split(b"\n")
        for line in lines:
            self.feed_line(line.decode("utf-8", errors="replace"), wall)

    def _event(self, kind: str, detail: str, wall: str) -> None:
        self.events.append({"kind": kind, "detail": detail, "dev_ms": self.tree.last_ms,
                            "wall": wall})

    def feed_line(self, raw: str, wall: str = "") -> None:
        self.tree.feed_line(raw)
        if len(self.tree.resets) > self._seen_resets:
            for r in self.tree.resets[self._seen_resets:]:
                self._event("reset", r, wall)
            self._seen_resets = len(self.tree.resets)
        line = ANSI.sub("", raw).rstrip("\r")
        m = LOG.match(line)
        if m:
            self._line(m.group(2), m.group(3), wall)
        self._check_topology(wall)

    def _line(self, tag: str, msg: str, wall: str) -> None:
        after = self.converged_ms is not None
        if tag == "Ch370Bus":
            if g := RE_BUS_ATTACH.match(msg):
                self.attaches += 1
                addr = int(g.group(1))
                first = addr not in self.published
                self.published.add(addr)
                # V2 延迟发布:树到齐后 attach 还会陆续发出,收敛地址各自第一次发布属于正常启动
                if after and addr not in self.converged_addrs:
                    self._event("addr_new", msg, wall)
                elif after and not first:
                    self._event("attach", msg, wall)
            elif g := RE_BUS_DETACH.match(msg):
                self.detaches += 1
                self.published.discard(int(g.group(1)))
                if after:
                    self._event("detach", msg, wall)
            elif RE_XFER_FAIL.match(msg):
                self.xfer_fails += 1
                if after:
                    self._event("xfer_fail", msg, wall)
        elif tag == "CpuStats":
            if g := RE_CPU.match(msg):
                self.cpu_ch370.append(float(g.group(1)))
        elif tag == "UsbTreeCh370":
            if RE_ADDR_OUT.search(msg):
                self.addr_exhausted += 1
                self._event("addr_exhausted", msg, wall)
            elif RE_TREE_WARN.search(msg):
                self.tree_warns += 1
                if after:
                    self._event("tree_warn", msg, wall)
        elif tag == "FishtankRegistry" and after and msg.startswith(("ch370 identify cb failed",
                                                                      "identify timeout")):
            self._event("identify", msg, wall)

    def _check_topology(self, wall: str) -> None:
        if self.tree.online_ms is None:
            return
        now = sorted((p, d.vidpid) for p, d in self.tree.devices.items())
        if now == self._snapshot:
            return
        self._snapshot = now
        missing, extra, _ = compare(self.tree, self.expected)
        if self.converged_ms is None:
            if not missing and not extra:
                self.converged_ms = self.tree.last_ms
                self.converged_addrs = {d.addr for d in self.tree.devices.values()}
            return
        detail = (" 缺 " + ",".join(e.path for e in missing) if missing else "") + \
                 (" 多 " + ",".join(f"{d.path}={d.vidpid}" for d in extra) if extra else "")
        self._event("topology", detail.strip() or "恢复完整", wall)

    # ---- 结论 ----

    def failures(self) -> list[dict]:
        bad = {"reset", "serial_lost", "topology", "attach", "detach", "addr_new",
               "addr_exhausted", "identify"}
        return [e for e in self.events if e["kind"] in bad]

    def status(self) -> dict:
        missing, extra, _ = compare(self.tree, self.expected)
        return {
            "converged_ms": self.converged_ms,
            "online_ms": self.tree.online_ms,
            "dev_ms": self.tree.last_ms,
            "devices": len(self.tree.devices),
            "missing": [e.path for e in missing],
            "extra": [d.path for d in extra],
            "failures": len(self.failures()),
            "events": len(self.events),
            "xfer_fails": self.xfer_fails,
            "enum_fail_lines": self.tree.enum_fail_lines + len(self.tree.diag_fails),
            "identify_fails": self.tree.identify_fails,
            "tree_warns": self.tree_warns,
            "addr_exhausted": self.addr_exhausted,
            "attaches": self.attaches,
            "detaches": self.detaches,
            "cpu_ch370_min": min(self.cpu_ch370, default=None),
            "cpu_ch370_max": max(self.cpu_ch370, default=None),
            "cpu_windows": len(self.cpu_ch370),
        }


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def run_soak(bench, hours: float, out_dir: Path, off_s: float = 5.0,
             converge_timeout_s: float = 60.0, status_every_s: float = 60.0,
             psu_every_s: float = 600.0) -> dict:
    """冷启动一次，然后被动读串口 hours 小时。期间不再动电源输出，只每隔 psu_every_s 读一次读数。"""
    from .runner import _open_console, _tool_rev
    from .zebra_psu import ZebraPsu

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"bench": bench.name, "hours": hours, "tool_rev": _tool_rev(),
            "started": datetime.now().isoformat(timespec="seconds")}
    mon = SoakMonitor(bench.expected)
    log = open(out_dir / "soak.log", "ab")
    psu = ZebraPsu.open(bench.psu_port)
    amps: list[float] = []
    try:
        psu.refresh_output_state()
        if not psu.output_on:
            psu.set_output(True)
            time.sleep(1)
        volt, amp = psu.setpoints()
        if abs(volt - bench.volt) > 0.05:
            raise SystemExit(f"电源设定 {volt}V 和台架要求 {bench.volt}V 不一致，拒绝运行")
        meta.update(psu_version=psu.version(), psu_setpoint_v=volt, psu_limit_a=amp)
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        psu.set_output(False)
        time.sleep(off_s)               # 关着时不发任何命令
        psu.set_output(True)
        t0 = time.monotonic()
        end = t0 + hours * 3600
        next_status, next_psu = t0 + status_every_s, t0 + psu_every_s
        con = None
        while time.monotonic() < end:
            now_wall = datetime.now().isoformat(timespec="seconds")
            if con is None:
                if not os.path.exists(bench.console_port):
                    if time.monotonic() - t0 > 10 and not any(
                            e["kind"] == "serial_lost" and e.get("open") for e in mon.events):
                        mon._event("serial_lost", "P4 串口不在", now_wall)
                        mon.events[-1]["open"] = True
                    time.sleep(0.05)
                    continue
                try:
                    con = _open_console(bench.console_port, 2.0)
                except Exception:
                    time.sleep(0.2)
                    continue
                for e in mon.events:
                    e.pop("open", None)
            try:
                chunk = con.read(4096)
            except Exception as e:
                mon._event("serial_lost", f"读串口出错: {e}", now_wall)
                mon.events[-1]["open"] = True
                try:
                    con.close()
                except Exception:
                    pass
                con = None
                continue
            if chunk:
                log.write(chunk)
                mon.feed(chunk, now_wall)
            if mon.converged_ms is None and time.monotonic() - t0 > converge_timeout_s and \
                    not any(e["kind"] == "no_converge" for e in mon.events):
                mon._event("no_converge", f"上电 {converge_timeout_s:.0f}s 仍未到齐", now_wall)
            t = time.monotonic()
            if t >= next_psu:
                next_psu = t + psu_every_s
                try:
                    amps.append(psu.measure()[1])
                except Exception:
                    pass
            if t >= next_status:
                next_status = t + status_every_s
                log.flush()
                st = {**mon.status(), "elapsed": _hms(t - t0), "wall": now_wall,
                      "amp_min": min(amps, default=None), "amp_max": max(amps, default=None)}
                (out_dir / "status.json").write_text(json.dumps(st, ensure_ascii=False, indent=2))
                with open(out_dir / "events.jsonl", "w", encoding="utf-8") as f:
                    for e in mon.events:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                print(f"[{st['elapsed']}] 设备 {st['devices']} 缺 {len(st['missing'])} "
                      f"多 {len(st['extra'])} 失败事件 {st['failures']} xfer_fail {st['xfer_fails']} "
                      f"enum_fail {st['enum_fail_lines']} ch370 {st['cpu_ch370_max']}%", flush=True)
        if con is not None:
            con.close()
    finally:
        log.close()
        try:
            if psu.output_on is not True:
                psu.set_output(True)
        except Exception as e:
            print(f"收尾时开输出失败: {e}")
        psu.close()

    st = {**mon.status(), "hours": hours, "amp_min": min(amps, default=None),
          "amp_max": max(amps, default=None)}
    with open(out_dir / "events.jsonl", "w", encoding="utf-8") as f:
        for e in mon.events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    st["verdict"] = "pass" if mon.converged_ms is not None and not mon.failures() else "fail"
    (out_dir / "status.json").write_text(json.dumps(st, ensure_ascii=False, indent=2))
    return st


def format_result(st: dict) -> str:
    lines = [f"结论: {st['verdict']}",
             f"时长 {st.get('hours')} h；收敛（设备时间）{st['converged_ms']} ms；当前设备 {st['devices']}",
             f"缺 {st['missing'] or '无'}；多 {st['extra'] or '无'}；失败事件 {st['failures']}",
             f"attach {st['attaches']} / detach {st['detaches']}；地址用尽 {st['addr_exhausted']}",
             f"xfer_fail {st['xfer_fails']}；enum_fail {st['enum_fail_lines']}；"
             f"IDENTIFY 失败 {st['identify_fails']}；树告警 {st['tree_warns']}",
             f"ch370_bus 占核 0：{st['cpu_ch370_min']}–{st['cpu_ch370_max']}%（{st['cpu_windows']} 个窗口）；"
             f"电流 {st.get('amp_min')}–{st.get('amp_max')} A"]
    return "\n".join(lines)
