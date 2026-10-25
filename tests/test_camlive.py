import csv
import tempfile
import unittest
from pathlib import Path

from hil.camlive import (CSV_FIELDS, Round, Runner, Timing, match_line, parse_plan, round_row,
                         summarize_camlive)

FIXTURES = Path(__file__).parent / "fixtures"
# 2026-10-25 台架 5C84388205 实机抓取(固件 4ca168ac 诊断版):直播中码率回路一路降到下限后停推
PAUSE = FIXTURES / "live_governor_pause.log"


class _Tap:
    """SerialTap 替身:between() 原样给出事件。"""

    def __init__(self, lines: list[str]):
        self.events = []
        for i, line in enumerate(lines):
            hit = match_line(line)
            if hit:
                self.events.append((float(i), hit[0], hit[1], line))

    def between(self, t0, t1):
        return [e for e in self.events if t0 <= e[0] < t1]


class MatchLineTest(unittest.TestCase):
    def test_fixture_kinds(self):
        kinds = [match_line(l)[0] for l in PAUSE.read_text(encoding="utf-8").splitlines() if match_line(l)]
        for k in ("lease_started", "enc_start", "rtmp_connected", "governor", "rtmp_sockets",
                  "rtmp_ended", "live_diag"):
            self.assertIn(k, kinds)

    def test_governor_both_formats(self):
        old = "I (1) SmartFishtankBoard: Governor: bitrate=400000 paused=1 resume_hold_ms=30000 (mqtt connected=1 age_ms=3124)"
        new = "I (1) SmartFishtankBoard: Governor: bitrate=1400000 paused=0 (rtmp congestion age=0 late=0)"
        self.assertEqual(match_line(old), ("governor", ("400000", "1")))
        self.assertEqual(match_line(new), ("governor", ("1400000", "0")))

    def test_rtmp_ended_error_code(self):
        self.assertEqual(match_line("W (5) Rtmps: publish ended; reconnect with a new credential (error=589826)"),
                         ("rtmp_ended", ("589826",)))


class FinishTest(unittest.TestCase):
    def test_round_collects_pause_and_last_diag(self):
        tap = _Tap(PAUSE.read_text(encoding="utf-8").splitlines())
        runner = Runner(api=None, cfg=None, tap=tap, t=Timing(), client="admin", out=Path("."))
        rec = runner.finish(Round(1, "long", "admin", "00:00:00"), t0=0.0)
        self.assertEqual(rec.fw["gov_pause"], 1)
        self.assertEqual(rec.fw["gov_min_br"], 400000)
        self.assertEqual(rec.fw["rtmp_ended"], 1)
        self.assertIn("paused=1", rec.fw["last_diag"])
        row = round_row(rec)
        self.assertEqual(set(row), set(CSV_FIELDS))


class PlanAndSummaryTest(unittest.TestCase):
    def test_parse_plan(self):
        self.assertEqual(parse_plan("cold:2, warm ,stall:1"), ["cold", "cold", "warm", "stall"])

    def test_summary_counts_failures(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            rows = [
                {"round": 1, "scenario": "cold", "result": "ok", "first_frame_s": 5.5, "stalls": 0,
                 "fw_rtmp_ended": 0, "fw_reboot": 0, "fw_gov_pause": 0},
                {"round": 2, "scenario": "cold", "result": "open_timeout", "first_frame_s": "",
                 "stalls": 0, "fw_rtmp_ended": 0, "fw_reboot": 0, "fw_gov_pause": 1,
                 "detail": "last=starting/starting", "states": "starting/starting",
                 "fw_last_diag": "pub=2 paused=1"},
            ]
            with (out / "rounds.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            text = summarize_camlive(out)
            self.assertIn("'open_timeout': 1", text)
            self.assertIn("停推=1", text)
            self.assertIn("LiveDiag: pub=2 paused=1", text)


if __name__ == "__main__":
    unittest.main()
