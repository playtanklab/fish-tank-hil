import csv
import queue
import re
import tempfile
import time
import unittest
from pathlib import Path

from hil.camlog import CameraState, RoundLog, analyze, match_line, parse_round_log
from hil.camplug import CSV_FIELDS, CameraBench, CamTiming, Session, _cam_round, summarize_camplug
from hil.zebra_psu import ZebraPsu
from test_zebra_psu import FakeSerial

FIXTURES = Path(__file__).parent / "fixtures"
REBOOT = FIXTURES / "cam_unplug_reboot.log"
VIDPID = "13d3:784b"
# 当前固件里“受控重启”相关的行；去掉它们就得到“未重启、自己恢复”的样子
REBOOT_LINES = re.compile(r"controlled whole-device restart|Application: Rebooting|ESP-ROM:|Build:"
                          r"|rst:0x|App version|recovery budget used")


def load(path: Path = REBOOT, drop: re.Pattern | None = None, extra: list[str] = ()):
    text = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if drop is None or not drop.search(l))
    return parse_round_log(text + "\n" + "\n".join(extra))


def analyze_text(path: Path = REBOOT, drop=None, extra=()) -> dict:
    lines, t_off, t_on = load(path, drop, extra)
    return analyze(lines, t_off, t_on, VIDPID)


class AnalyzeTest(unittest.TestCase):
    def test_real_reboot_round(self):
        # 2026-10-23 5C84388210 主固件 2.4.2.5：断电 0.19 s 后检出掉线，受控重启 1/2，重启后恢复出流
        r = analyze_text()
        self.assertEqual(r["result"], "rebooted")
        self.assertEqual(r["drop_ms"], 190)
        self.assertEqual(r["reboot_ms"], 1390)
        self.assertEqual(r["attach_ms"], 860)
        self.assertEqual(r["uvc_ms"], 860)
        self.assertEqual(r["stream_ms"], 9870)
        self.assertEqual(r["publish_ms"], 17080)
        self.assertEqual((r["usb_err"], r["urb_err"], r["teardown_unsafe"]), (27, 4, 1))
        self.assertEqual(r["restart_req"], "1/2")
        self.assertEqual(r["budget_boot"], "1/2")
        self.assertEqual(r["reset_reason"], "0xc SW_CPU_RESET")
        self.assertEqual(r["fw"], "2.4.2.5")
        self.assertEqual((r["reboots"], r["stream_starts"], r["budget_exhausted"]), (1, 2, 0))
        self.assertIn("受控重启 1/2", r["detail"])
        self.assertIn("重启后恢复出流 9870ms", r["detail"])
        self.assertTrue(r["events"].startswith("usb_err@190;urb_err@190;"))

    def test_recovered_without_reboot(self):
        # 改造目标：掉线后不重启，摄像头重新插上后自己恢复出流
        r = analyze_text(drop=REBOOT_LINES)
        self.assertEqual(r["result"], "recovered")
        self.assertEqual((r["drop_ms"], r["attach_ms"], r["stream_ms"]), (190, 860, 9870))
        self.assertIsNone(r["reboot_ms"])
        self.assertEqual(r["teardown_unsafe"], 1)

    def test_reboot_without_stream_is_still_rebooted(self):
        after_on = re.compile(r"stream started|Rtmps")
        r = analyze_text(drop=after_on)
        self.assertEqual(r["result"], "rebooted")
        self.assertIsNone(r["stream_ms"])
        self.assertIn("重启后未恢复出流", r["detail"])

    def test_budget_exhausted_is_offline(self):
        # 固件 TickMediaRecovery 预算耗尽时的原文；摄像头重新上电也不再出流
        lines = [l for l in REBOOT.read_text(encoding="utf-8").splitlines()]
        cut = next(i for i, l in enumerate(lines) if "controlled whole-device restart" in l)
        on = next(l for l in lines if "PSU ON" in l)
        text = "\n".join(lines[:cut] + [
            "[    5.420] E (53060) SmartFishtankBoard: USB camera recovery budget exhausted; camera "
            "stays offline; control remains active, physical power cycle required", on]) + "\n"
        r = analyze(*parse_round_log(text), VIDPID)
        self.assertEqual(r["result"], "offline")
        self.assertEqual(r["budget_exhausted"], 1)
        self.assertIn("预算耗尽", r["detail"])

    def test_offline_without_any_drop_event(self):
        r = analyze_text(extra=[], drop=re.compile(r"\] [IWE] \(|ESP-ROM|Build:|rst:0x"))
        self.assertEqual(r["result"], "rig_fault")   # 断电后一行日志都没有
        lines, t_off, t_on = parse_round_log(
            "[0.0] ===== PSU OFF =====\n[1.0] I (100) SystemInfo: free sram: 1\n"
            "[3.0] ===== PSU ON =====\n[9.0] I (900) SystemInfo: free sram: 1\n")
        r = analyze(lines, t_off, t_on, VIDPID)
        self.assertEqual(r["result"], "offline")
        self.assertIn("未检出掉线", r["detail"])

    def test_crash_wins(self):
        r = analyze_text(extra=["[   30.000] Guru Meditation Error: Core  1 panic'ed (Load access fault)"])
        self.assertEqual(r["result"], "crash")
        self.assertIn("Guru Meditation", r["crash"])
        wdt = analyze_text(drop=re.compile(r"rst:0x"),
                           extra=["[    6.611] rst:0x7 (TG0WDT_HPSYS_RST),boot:0x30f"])
        self.assertEqual(wdt["result"], "crash")
        self.assertIn("TG0WDT", wdt["detail"])

    def test_c6_watchdog_armed_is_not_crash(self):
        r = analyze_text(extra=["[   21.630] I (15070) SmartFishtankBoard: C6 heartbeat #0 arrived; "
                                "watchdog armed (timeout 45000 ms)"])
        self.assertEqual(r["result"], "rebooted")

    def test_stream_while_off_is_rig_fault(self):
        r = analyze_text(extra=["[    7.000] I (500) UvcCamera: stream started (actual) 640x480@20fps MJPEG"])
        self.assertEqual(r["result"], "rig_fault")

    def test_other_usb_device_attach_ignored(self):
        lines, t_off, t_on = load(drop=re.compile("13d3:784b"),
                                  extra=["[    9.000] I (2500) UsbDeviceInventory: USB attach addr=3 "
                                         "vid:pid=1a86:8091 class=09 (hub)"])
        self.assertIsNone(analyze(lines, t_off, t_on, VIDPID)["attach_ms"])

    def test_ansi_colors_stripped(self):
        self.assertEqual(match_line("\x1b[0;33mW (1) uvc-isoc: usb err 1\x1b[0m")[0], "usb_err")


class CameraStateTest(unittest.TestCase):
    def test_transitions(self):
        s = CameraState()
        self.assertEqual(s.state, "unknown")
        s.feed(1.0, "I (1) UvcCamera: stream started (actual) 1280x720@20fps MJPEG [tier=L2]")
        s.feed(1.1, "I (2) UvcCamera: stream stopped")
        self.assertEqual(s.state, "down")
        s.feed(1.2, "I (3) UvcCamera: stream started (actual) 640x480@20fps MJPEG [tier=manual]")
        self.assertEqual((s.state, s.since), ("streaming", 1.2))
        s.feed(2.0, "W (13150) uvc-isoc: usb err 1")    # 正常出流时也偶有一条
        self.assertEqual((s.state, s.since), ("streaming", 1.2))
        s.feed(3.0, "E (5) UvcCamera: USB teardown unsafe; camera offline, recovery required (reason=4)")
        self.assertEqual(s.state, "down")


class FakePsuSerial(FakeSerial):
    """电源假串口：开关输出时往板载串口队列里注入实机日志。"""

    def __init__(self, q: queue.Queue, on_off: list[str], on_on: list[str]):
        super().__init__({b"AT_OUTPUT": b"AT_OUTPUT:1\r\n", b"AT_OUTPUT=0": b"AT_OUTPUT:OK\r\n",
                          b"AT_OUTPUT=1": b"AT_OUTPUT:OK\r\n",
                          b"AT_Q_VOLT_OUT": b"AT_Q_VOLT_OUT:5.01\r\n",
                          b"AT_Q_CURRENT_OUT": b"AT_Q_CURRENT_OUT:0.120\r\n"})
        self.q, self.on_off, self.on_on = q, on_off, on_on

    def write(self, data: bytes):
        super().write(data)
        for line in {b"AT_OUTPUT=0": self.on_off, b"AT_OUTPUT=1": self.on_on}.get(data, []):
            self.q.put((time.monotonic(), line))


class RoundFlowTest(unittest.TestCase):
    def test_round_sends_nothing_while_off(self):
        lines, t_off, t_on = load()
        before_on = [l for t, l in lines if t_off < t < t_on]
        after_on = [l for t, l in lines if t > t_on]

        class Reader:
            q: queue.Queue = queue.Queue()

        Reader.q.put((time.monotonic() - 30, "I (1) UvcCamera: stream started (actual) 640x480@20fps"))
        ser = FakePsuSerial(Reader.q, before_on, after_on)
        psu = ZebraPsu(ser, reply_timeout=0.05)
        psu.refresh_output_state()
        bench = CameraBench("t", "", "", 5.0, VIDPID)
        s = Session(Reader, bench, CamTiming(off_s=0.05, on_wait_s=0.1, pre_wait_s=0.05,
                                             stream_settle_s=0))
        s.log = RoundLog(time.monotonic())
        row = _cam_round(psu, s, first=False)
        self.assertEqual(row["pre"], "streaming")
        self.assertEqual(row["result"], "rebooted")
        cmds = ser.written
        off = cmds.index(b"AT_OUTPUT=0")
        self.assertEqual(cmds[off + 1], b"AT_OUTPUT=1")       # 关着时只发了开输出
        self.assertEqual(cmds[off + 2:], [b"AT_Q_VOLT_OUT", b"AT_Q_CURRENT_OUT"])
        self.assertEqual(s.state.state, "streaming")          # 重启后又出流了，下一轮可以直接断电

    def test_not_ready_skips_power(self):
        class Reader:
            q: queue.Queue = queue.Queue()

        Reader.q.put((time.monotonic(), "E (1) SmartFishtankBoard: USB camera recovery budget "
                                         "exhausted; camera stays offline"))
        ser = FakePsuSerial(Reader.q, [], [])
        psu = ZebraPsu(ser, reply_timeout=0.05)
        psu.refresh_output_state()
        s = Session(Reader, CameraBench("t", "", "", 5.0, VIDPID), CamTiming(pre_wait_s=0.05))
        s.log = RoundLog(time.monotonic())
        row = _cam_round(psu, s, first=True)
        self.assertEqual(row["result"], "not_ready")
        self.assertNotIn(b"AT_OUTPUT=0", ser.written)


class SummaryTest(unittest.TestCase):
    def test_summary_counts_and_distribution(self):
        rows = [analyze_text(), analyze_text(drop=REBOOT_LINES), analyze_text(drop=REBOOT_LINES)]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with open(out / "rounds.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                for i, r in enumerate(rows, 1):
                    w.writerow({k: {**r, "round": i, "pre": "streaming"}.get(k, "") for k in CSV_FIELDS})
            text = summarize_camplug(out)
        self.assertIn("recovered 2，rebooted 1", text)
        self.assertIn("上电→重新出流  min=9870 p50=9870 p95=9870 max=9870（2 轮）", text)
        self.assertIn("其中重启后恢复出流 1 轮", text)
        self.assertIn("0xc SW_CPU_RESET ×1", text)
        self.assertIn("第1轮 1/2", text)
        self.assertIn("2.4.2.5", text)


class BenchTest(unittest.TestCase):
    def test_camera_bench_loads(self):
        b = CameraBench.load(Path(__file__).parent.parent / "benches" / "5C84388210-camera.json")
        self.assertEqual(b.camera_vidpid, VIDPID)
        self.assertIn("5C84388210", b.console_port)


if __name__ == "__main__":
    unittest.main()
