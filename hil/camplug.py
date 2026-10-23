"""摄像头插拔台：主板保持供电，只开关摄像头的供电，统计主板对 UVC 摄像头热插拔的反应。

台架接法：数控电源输出只接摄像头，主板另外供电。所以关输出 = 摄像头掉线，主板和板载串口一直在线。
板载串口由后台线程全程读取（pyserial 默认打开，不碰 DTR/RTS，不复位主板）。

每轮：断电前确认摄像头在出流 → 关输出 ``off`` 秒（期间不发任何电源命令）→ 开输出 →
观察 ``on_wait`` 秒 → 用 ``camlog.analyze`` 分类。分类和日志匹配规则见 ``hil/camlog.py``。

断电前确认：固件出流之后没有周期日志，被动读串口确认不了“现在是否在出流”。
- 上一轮（或本次 pre-wait 内）看到过 ``stream started`` 且之后没有掉线/重启迹象 → 在流，
  再等它稳定 ``stream_settle_s`` 秒（让推流连上）后断电；
- 第 1 轮开跑时状态未知：听满 pre-wait，没有看到任何反面迹象就**假定在流**（rounds.csv 的
  pre 列记 ``assumed``），开跑前请人工确认；
- 其余情况（看到掉线/重启/预算耗尽后 pre-wait 内没重新出流）记 ``not_ready``，这一轮不动电源。
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .camlog import MARK_OFF, MARK_ON, MARK_SERIAL_LOST, CameraState, RoundLog, analyze
from .summary import percentile
from .zebra_psu import ZebraPsu

KIND = "camplug"
CSV_FIELDS = [
    "round", "started", "result", "detail", "pre", "drop_ms", "reboot_ms", "attach_ms", "uvc_ms",
    "stream_ms", "enc_ms", "publish_ms", "usb_err", "urb_err", "teardown_unsafe", "restart_req",
    "budget_boot", "budget_exhausted", "reset_reason", "reboots", "stream_starts", "fw",
    "serial_lost", "crash", "amp_before", "amp", "events", "log",
]
# 连续出现这么多轮就中止：摄像头卡在离线（如恢复预算耗尽）后继续跑没有意义
FAIL_RESULTS = ("offline", "crash", "not_ready", "rig_fault")


@dataclass
class CameraBench:
    name: str
    psu_port: str
    console_port: str
    volt: float
    camera_vidpid: str
    camera_name: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "CameraBench":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if "camera" not in d:
            raise SystemExit(f"{path} 不是摄像头台架文件（缺 camera 字段）")
        cam = d["camera"]
        return cls(d["name"], d["psu_port"], d["console_port"], float(d["volt"]),
                   cam["vidpid"].lower(), cam.get("name", ""))


@dataclass
class CamTiming:
    off_s: float = 3.0            # 摄像头断电秒数；期间不给电源发任何命令
    on_wait_s: float = 40.0       # 上电后观察秒数（整机重启后出流约要 10 s，推流再晚几秒）
    pre_wait_s: float = 20.0      # 断电前最多等多久确认在流
    stream_settle_s: float = 10.0  # 看到出流后至少再等这么久才断电，让推流连上


class ConsoleReader(threading.Thread):
    """后台读板载串口，按行放进队列，每行带主机收到时刻。串口掉了就重开（同样不碰 DTR/RTS）。"""

    def __init__(self, port: str):
        super().__init__(daemon=True, name="console-reader")
        self.port = port
        self.q: queue.Queue[tuple[float, str]] = queue.Queue()
        self._halt = threading.Event()
        self._con = None

    def open(self) -> None:
        from .runner import _open_console

        self._con = _open_console(self.port, 2.0)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)

    def run(self) -> None:
        from .runner import _open_console

        buf = b""
        while not self._halt.is_set():
            if self._con is None:
                try:
                    self._con = _open_console(self.port, 0.5)
                except Exception:
                    time.sleep(0.5)
                    continue
            try:
                chunk = self._con.read(4096)
            except Exception:
                self.q.put((time.monotonic(), MARK_SERIAL_LOST))
                try:
                    self._con.close()
                except Exception:
                    pass
                self._con = None
                continue
            if chunk:
                t = time.monotonic()
                *lines, buf = (buf + chunk).split(b"\n")
                for line in lines:
                    self.q.put((t, line.decode("utf-8", errors="replace").rstrip("\r")))
        if self._con is not None:
            self._con.close()


class Session:
    def __init__(self, reader: ConsoleReader, bench: CameraBench, t: CamTiming):
        self.reader, self.bench, self.t = reader, bench, t
        self.state = CameraState()
        self.log: RoundLog | None = None
        self.seen_lines = 0

    def pump(self, until: float) -> None:
        """把串口行喂给状态机和本轮日志，直到主机时间 until。"""
        while True:
            left = until - time.monotonic()
            try:
                t, line = self.reader.q.get(timeout=max(0.0, min(left, 0.05)))
            except queue.Empty:
                if left <= 0:
                    return
                continue
            self.seen_lines += 1
            self.state.feed(t, line)
            if self.log is not None:
                self.log.add(t, line)

    def mark(self, text: str) -> float:
        now = time.monotonic()
        if self.log is not None:
            self.log.add(now, text)
        return now

    def wait_ready(self, first: bool) -> str:
        """断电前确认摄像头在流，返回 pre 列：streaming / assumed / not_ready。"""
        t0 = time.monotonic()
        deadline = t0 + self.t.pre_wait_s
        while True:
            self.pump(min(deadline, time.monotonic() + 0.2))
            now = time.monotonic()
            st = self.state
            if st.state == "streaming" and now - st.since >= self.t.stream_settle_s:
                return "streaming"
            if now >= deadline:
                break
        if self.state.state == "streaming":
            # pre-wait 结束时刚出流不久：再等到稳定为止，最多再等 stream_settle_s
            self.pump(self.state.since + self.t.stream_settle_s)
            return "streaming" if self.state.state == "streaming" else "not_ready"
        if first and self.state.state == "unknown":
            return "assumed"
        return "not_ready"


def _cam_round(psu: ZebraPsu, s: Session, first: bool) -> dict:
    row: dict = {"started": datetime.now().isoformat(timespec="seconds")}
    pre = s.wait_ready(first)
    row["pre"] = pre
    if pre == "not_ready":
        return {**row, "result": "not_ready",
                "detail": f"pre-wait 内没确认在流（最后迹象: {s.state.evidence[:100]}）"}
    try:
        _, row["amp_before"] = psu.measure()
    except Exception:
        pass
    t_off = s.mark(MARK_OFF)
    psu.set_output(False)
    s.pump(t_off + s.t.off_s)           # 只读串口；输出关着时不发任何电源命令
    t_on = s.mark(MARK_ON)
    psu.set_output(True)
    s.pump(t_on + s.t.on_wait_s)
    try:
        row["amp"] = psu.measure()[1]
    except Exception:
        pass
    row.update(analyze(s.log.lines, t_off, t_on, s.bench.camera_vidpid))
    return row


def run_camplug(bench: CameraBench, rounds: int, out_dir: Path, t: CamTiming,
                keep_all_logs: bool = False, abort_after: int = 3) -> Path:
    from .runner import _tool_rev

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(exist_ok=True)
    meta = {"kind": KIND, "bench": bench.name, "camera_vidpid": bench.camera_vidpid,
            "rounds": rounds, "timing": t.__dict__, "tool_rev": _tool_rev(),
            "started": datetime.now().isoformat(timespec="seconds")}
    reader = ConsoleReader(bench.console_port)
    reader.open()           # 先在主线程打开，串口不在就直接失败
    reader.start()
    psu = ZebraPsu.open(bench.psu_port)
    try:
        psu.refresh_output_state()
        if not psu.output_on:
            print("电源输出原本是关的（摄像头没供电），先打开", flush=True)
            psu.set_output(True)
            time.sleep(1)
        volt, amp = psu.setpoints()
        if abs(volt - bench.volt) > 0.05:
            raise SystemExit(f"电源设定 {volt}V 和台架要求 {bench.volt}V 不一致，拒绝运行")
        meta.update(psu_version=psu.version(), psu_setpoint_v=volt, psu_limit_a=amp)
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        s = Session(reader, bench, t)
        with open(out_dir / "rounds.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            fails = 0
            for i in range(1, rounds + 1):
                log_path = out_dir / "logs" / f"round-{i:04d}.log"
                s.log = RoundLog(time.monotonic())
                try:
                    row = _cam_round(psu, s, first=i == 1)
                except Exception as e:
                    # 电源没回复/关机等：先把这一轮已经抓到的日志和一行 rig_fault 落盘再退出
                    s.pump(time.monotonic() + 2)
                    s.mark(f"===== 中止: {e} =====")
                    log_path.write_text(s.log.text(), encoding="utf-8")
                    w.writerow({k: {"round": i, "result": "rig_fault", "detail": f"中止: {e}",
                                    "log": log_path.name}.get(k, "") for k in CSV_FIELDS})
                    f.flush()
                    raise
                row["round"] = i
                if row["result"] == "recovered" and not keep_all_logs:
                    row["log"] = ""
                else:
                    log_path.write_text(s.log.text(), encoding="utf-8")
                    row["log"] = log_path.name
                w.writerow({k: row.get(k, "") for k in CSV_FIELDS})
                f.flush()
                print(f"[{i}/{rounds}] {row['result']:<9} pre={row['pre']} drop={row.get('drop_ms')} "
                      f"reboot={row.get('reboot_ms')} attach={row.get('attach_ms')} "
                      f"stream={row.get('stream_ms')} enc={row.get('enc_ms')} "
                      f"pub={row.get('publish_ms')} usb_err={row.get('usb_err', '')} "
                      f"{row.get('detail', '')}", flush=True)
                fails = fails + 1 if row["result"] in FAIL_RESULTS else 0
                if abort_after and fails >= abort_after:
                    print(f"连续 {fails} 轮 {row['result']}，摄像头可能卡在离线，中止", flush=True)
                    break
    finally:
        # 不管怎么结束都让摄像头恢复供电
        try:
            if psu.output_on is not True:
                psu.set_output(True)
        except Exception as e:
            print(f"收尾时开输出失败: {e}")
        psu.close()
        reader.stop()
    return out_dir


def _dist(values: list[int]) -> str:
    if not values:
        return "无"
    return (f"min={min(values)} p50={percentile(values, .5)} p95={percentile(values, .95)} "
            f"max={max(values)}（{len(values)} 轮）")


def is_camplug_dir(out_dir: Path) -> bool:
    try:
        return json.loads((out_dir / "meta.json").read_text()).get("kind") == KIND
    except Exception:
        return False


def summarize_camplug(out_dir: Path) -> str:
    with open(out_dir / "rounds.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    res = Counter(r["result"] for r in rows)

    def ints(rs, key):
        return [int(r[key]) for r in rs if r[key] not in ("", None)]

    rec = [r for r in rows if r["result"] == "recovered"]
    reb = [r for r in rows if r["result"] == "rebooted"]
    reb_ok = [r for r in reb if r["stream_ms"]]
    order = ["recovered", "rebooted", "offline", "crash", "not_ready", "rig_fault"]
    lines = [
        f"运行目录: {out_dir}",
        f"总轮数 {len(rows)}；分类: " + "，".join(f"{k} {res[k]}" for k in order if res[k]),
        f"固件版本（轮内开机时抓到）: {', '.join(sorted({r['fw'] for r in rows if r['fw']})) or '未抓到'}",
        "",
        f"未重启恢复（recovered）{len(rec)} 轮，耗时 ms：",
        f"  断电→检出掉线  {_dist(ints(rec, 'drop_ms'))}",
        f"  上电→USB attach {_dist(ints(rec, 'attach_ms'))}",
        f"  上电→重新出流  {_dist(ints(rec, 'stream_ms'))}",
        f"  上电→编码恢复  {_dist(ints(rec, 'enc_ms'))}",
        f"  上电→推流恢复  {_dist(ints(rec, 'publish_ms'))}",
        f"整机重启（rebooted）{len(reb)} 轮，其中重启后恢复出流 {len(reb_ok)} 轮；"
        f"含崩溃在内的重启次数 {sum(int(r['reboots'] or 0) for r in rows)}",
    ]
    if reb:
        reasons = Counter(r["reset_reason"] or "未抓到" for r in reb)
        lines.append("  复位原因: " + "，".join(f"{k} ×{v}" for k, v in reasons.most_common()))
        lines.append(f"  断电→重启横幅  {_dist(ints(reb, 'reboot_ms'))}")
        lines.append(f"  上电→重新出流  {_dist(ints(reb_ok, 'stream_ms'))}")
    usb_err = ints(rows, "usb_err")
    lines += [
        "",
        f"usb err 合计 {sum(usb_err)}，单轮最多 {max(usb_err, default=0)}；"
        f"URB 入队失败合计 {sum(ints(rows, 'urb_err'))}；"
        f"teardown unsafe 出现在 {sum(1 for r in rows if int(r['teardown_unsafe'] or 0))} 轮",
        "恢复预算: 受控重启 " + (", ".join(f"第{r['round']}轮 {r['restart_req']}"
                                    for r in rows if r["restart_req"]) or "无")
        + "；开机已用 " + (", ".join(f"第{r['round']}轮 {r['budget_boot']}"
                                  for r in rows if r["budget_boot"]) or "无")
        + f"；预算耗尽 {sum(1 for r in rows if int(r['budget_exhausted'] or 0))} 轮",
    ]
    for r in rows:
        if r["result"] not in ("recovered", "rebooted"):
            lines.append(f"  第 {r['round']} 轮 {r['result']}: {r['detail']}")
    return "\n".join(lines)
