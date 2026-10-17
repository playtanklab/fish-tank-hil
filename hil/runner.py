"""断电循环：关输出 → 等待 → 开输出 → 抓 P4 串口 → 和期望拓扑比对。

每轮结果分类：
- pass          期望设备在 CH370 上线后 deadline 内全部出现
- usb_missing   CH370 上线了，但过了 deadline 仍有期望设备没出现（要统计的失败）
- boot_fail     串口有日志，但始终没等到 "CH370 online"
- reset         抓取中途 P4 又复位了一次（ROM 横幅或时间戳回退），这一轮不可信
- rig_fault     测试台自己的问题：串口没出现、打不开、没数据、开头日志丢太多
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ch370_log import Ch370Tree, Expected, compare
from .zebra_psu import PsuShutdown, ZebraPsu

CSV_FIELDS = [
    "round", "started", "result", "detail", "online_ms", "converge_ms", "first_log_ms",
    "tty_s", "cold", "hot", "enum_fail_lines", "missing", "extra", "volt", "amp", "log",
]


@dataclass
class Bench:
    name: str
    psu_port: str
    console_port: str
    volt: float
    expected: list[Expected]

    @classmethod
    def load(cls, path: str | Path) -> "Bench":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        exp = [Expected(e["path"], e["vidpid"], e.get("name", "")) for e in d["devices"]]
        return cls(d["name"], d["psu_port"], d["console_port"], float(d["volt"]), exp)


@dataclass
class Timing:
    off_s: float = 5.0          # 断电时长；期间不给电源发任何命令
    deadline_ms: int = 10_000   # S2 硬上限：CH370 上线后多久必须全部到齐
    settle_ms: int = 1_000      # 全部到齐后再多看一会儿，抓后续掉线
    tail_ms: int = 30_000       # 没到齐时继续观察到 CH370 上线后多久，看最终会不会收敛
    tty_timeout_s: float = 10.0
    online_timeout_s: float = 25.0
    max_first_log_ms: int = 2_000  # 打开串口时设备已经跑过这么久，就认为开头日志丢太多


def _tool_rev() -> str:
    try:
        return subprocess.check_output(
            ["git", "describe", "--always", "--dirty"], cwd=Path(__file__).parent,
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _open_console(path: str, timeout_s: float):
    import serial

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            # 默认打开，不碰 DTR/RTS：实测这样不会复位 P4
            return serial.Serial(path, 115200, timeout=0.05)
        except (serial.SerialException, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def run_round(psu: ZebraPsu, bench: Bench, t: Timing, log_path: Path) -> dict:
    row: dict = {"started": datetime.now().isoformat(timespec="seconds")}
    psu.set_output(False)
    time.sleep(t.off_s)
    if os.path.exists(bench.console_port):
        psu.set_output(True)
        return {**row, "result": "rig_fault", "detail": "断电期间 P4 串口仍在，板子没断干净"}
    psu.set_output(True)
    t_on = time.monotonic()

    while not os.path.exists(bench.console_port):
        if time.monotonic() - t_on > t.tty_timeout_s:
            return {**row, "result": "rig_fault", "detail": "上电后 P4 串口没出现"}
        time.sleep(0.005)
    row["tty_s"] = round(time.monotonic() - t_on, 3)

    tree = Ch370Tree()
    raw = bytearray()
    try:
        con = _open_console(bench.console_port, 2.0)
    except Exception as e:
        return {**row, "result": "rig_fault", "detail": f"P4 串口打不开: {e}"}
    try:
        while True:
            chunk = con.read(4096)
            if chunk:
                raw += chunk
                tree.feed(chunk)
            if tree.resets:
                break
            missing, _, last_ms = compare(tree, bench.expected)
            now_ms = tree.last_ms
            if tree.online_ms is not None and now_ms is not None:
                since = now_ms - tree.online_ms
                if not missing and since >= (last_ms - tree.online_ms) + t.settle_ms:
                    break
                if since >= t.tail_ms:
                    break
            elif time.monotonic() - t_on > t.online_timeout_s:
                break
            if time.monotonic() - t_on > t.online_timeout_s + t.tail_ms / 1000:
                break
    finally:
        con.close()
    log_path.write_bytes(raw)

    missing, extra, last_ms = compare(tree, bench.expected)
    devs = tree.devices.values()
    row.update(
        online_ms=tree.online_ms, first_log_ms=tree.first_ms,
        cold=sum(d.how == "cold" for d in devs), hot=sum(d.how == "hot" for d in devs),
        enum_fail_lines=tree.enum_fail_lines,
        missing=";".join(f"{e.path}={e.vidpid}" for e in missing),
        extra=";".join(f"{d.path}={d.vidpid}" for d in extra),
        log=log_path.name,
    )
    try:
        row["volt"], row["amp"] = psu.measure()
    except PsuShutdown:
        raise
    except Exception:
        pass

    if not raw:
        return {**row, "result": "rig_fault", "detail": "P4 串口没有任何数据"}
    if tree.resets:
        return {**row, "result": "reset", "detail": tree.resets[0]}
    if tree.online_ms is None:
        if tree.first_ms is not None and tree.first_ms > t.max_first_log_ms:
            return {**row, "result": "rig_fault", "detail": f"开头日志丢失，首行 {tree.first_ms}ms"}
        return {**row, "result": "boot_fail", "detail": "没等到 CH370 online"}
    if last_ms is not None and not missing:
        row["converge_ms"] = last_ms - tree.online_ms
    if not missing and row["converge_ms"] <= t.deadline_ms:
        return {**row, "result": "pass", "detail": ""}
    late = f"最后一个设备在上线后 {row.get('converge_ms')}ms 才出现" if not missing else "缺设备"
    return {**row, "result": "usb_missing", "detail": late}


def discover(psu_port: str, console_port: str, quiet_ms: int = 10_000, limit_ms: int = 60_000):
    """断电上电一次，抓到拓扑连续 quiet_ms 没有变化为止，返回 (Ch370Tree, 原始日志)。"""
    psu = ZebraPsu.open(psu_port)
    try:
        psu.refresh_output_state()
        psu.set_output(False)
        time.sleep(Timing.off_s)
        psu.set_output(True)
        t_on = time.monotonic()
        while not os.path.exists(console_port):
            if time.monotonic() - t_on > Timing.tty_timeout_s:
                raise SystemExit("上电后 P4 串口没出现")
            time.sleep(0.005)
        con = _open_console(console_port, 2.0)
        tree, raw = Ch370Tree(), bytearray()
        snapshot, changed_ms = None, None
        try:
            while time.monotonic() - t_on < limit_ms / 1000 + 5:
                chunk = con.read(4096)
                if chunk:
                    raw += chunk
                    tree.feed(chunk)
                if tree.online_ms is None or tree.last_ms is None:
                    continue
                now = sorted((p, d.vidpid) for p, d in tree.devices.items())
                if now != snapshot:
                    snapshot, changed_ms = now, tree.last_ms
                if tree.last_ms - changed_ms >= quiet_ms or tree.last_ms - tree.online_ms >= limit_ms:
                    break
        finally:
            con.close()
        return tree, bytes(raw)
    finally:
        if psu.output_on is not True:
            psu.set_output(True)
        psu.close()


def run(bench: Bench, rounds: int, out_dir: Path, t: Timing, keep_all_logs: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(exist_ok=True)
    meta = {"bench": bench.name, "rounds": rounds, "timing": t.__dict__, "tool_rev": _tool_rev(),
            "started": datetime.now().isoformat(timespec="seconds")}
    psu = ZebraPsu.open(bench.psu_port)
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

        with open(out_dir / "rounds.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for i in range(1, rounds + 1):
                log_path = out_dir / "logs" / f"round-{i:04d}.log"
                row = run_round(psu, bench, t, log_path)
                row["round"] = i
                if row["result"] == "pass" and not keep_all_logs and log_path.exists():
                    log_path.unlink()
                    row["log"] = ""
                w.writerow({k: row.get(k, "") for k in CSV_FIELDS})
                f.flush()
                print(f"[{i}/{rounds}] {row['result']:<11} online={row.get('online_ms')} "
                      f"converge={row.get('converge_ms')} cold/hot={row.get('cold')}/{row.get('hot')} "
                      f"{row.get('detail', '')}", flush=True)
    finally:
        # 不管怎么结束都让板子保持供电
        try:
            if psu.output_on is not True:
                psu.set_output(True)
        except Exception as e:
            print(f"收尾时开输出失败: {e}")
        psu.close()
    return out_dir
