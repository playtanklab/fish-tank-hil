import json
import unittest
from pathlib import Path

from hil.ch370_log import Expected
from hil.soak import SoakMonitor, format_result

FIXTURE = Path(__file__).parent / "fixtures" / "boot_v2_11modules.log"
BENCH = Path(__file__).parent.parent / "benches" / "5C84388205-full.json"
EXPECTED = [Expected(d["path"], d["vidpid"], d.get("name", ""))
            for d in json.loads(BENCH.read_text(encoding="utf-8"))["devices"]]


def booted() -> SoakMonitor:
    # 2026-10-17 5C84388205 最终固件（V2）实机开机，只保留 USB 相关标签的行，chip_id 已抹掉
    m = SoakMonitor(EXPECTED)
    m.feed(FIXTURE.read_bytes())
    return m


class SoakMonitorTest(unittest.TestCase):
    def test_clean_boot_converges_without_failures(self):
        m = booted()
        self.assertEqual(m.converged_ms, 5974)
        self.assertEqual(m.failures(), [])
        st = m.status()
        self.assertEqual(st["devices"], 14)
        self.assertEqual((st["missing"], st["extra"]), ([], []))
        self.assertEqual(m.attaches, 11)
        self.assertEqual(m.cpu_ch370, [4.8, 4.7])
        self.assertIn("pass", format_result({**st, "verdict": "pass"}))

    def test_chunked_feed_matches_whole(self):
        data = FIXTURE.read_bytes()
        m = SoakMonitor(EXPECTED)
        for i in range(0, len(data), 7):
            m.feed(data[i:i + 7])
        self.assertEqual(m.status(), booted().status())

    def test_unplug_after_converge_is_missing_and_detach(self):
        m = booted()
        m.feed_line("I (300000) UsbTreeCh370: 热拔出: hub#3 端口2 设备移除,回收地址", "t1")
        m.feed_line("I (300001) Ch370Bus: ch370 detach: addr=12", "t1")
        kinds = [e["kind"] for e in m.failures()]
        self.assertEqual(kinds, ["topology", "detach"])
        self.assertIn("root.7.2", m.failures()[0]["detail"])
        self.assertEqual(m.status()["missing"], ["root.7.2"])

    def test_reattach_with_new_address_flags_addr_new(self):
        m = booted()
        m.feed_line("I (300000) UsbTreeCh370: 热拔出: hub#3 端口2 设备移除,回收地址")
        m.feed_line("I (301000) UsbTreeCh370: 热插入: hub#3 端口2 → addr=15 2402:5601 'PUMP-CHAMBER'")
        m.feed_line("I (301005) Ch370Bus: ch370 attach: addr=15 vid=2402 pid=5601")
        kinds = [e["kind"] for e in m.failures()]
        self.assertEqual(kinds, ["topology", "topology", "addr_new"])
        self.assertEqual(m.failures()[1]["detail"], "恢复完整")
        self.assertEqual(m.status()["missing"], [])

    def test_ghost_device_is_extra(self):
        m = booted()
        m.feed_line("I (300000) UsbTreeCh370: 热插入: hub#2 端口1 → addr=15 2402:5605 'SPOTLIGHT'")
        self.assertEqual(m.status()["extra"], ["root.6.1"])
        self.assertEqual([e["kind"] for e in m.failures()], ["topology"])

    def test_reboot_and_chip_events(self):
        m = booted()
        m.feed_line("ESP-ROM:esp32p4-eco2-20240710")
        m.feed_line("W (400000) UsbTreeCh370: chip_reset CH370 寄存器回到默认值(USB_SETUP=0x00),重新初始化并全量重建")
        m.feed_line("W (400001) UsbTreeCh370: USB 地址用尽")
        kinds = [e["kind"] for e in m.events]
        self.assertIn("reset", kinds)
        self.assertIn("tree_warn", kinds)
        self.assertIn("addr_exhausted", [e["kind"] for e in m.failures()])

    def test_xfer_fail_counted_but_not_failure(self):
        m = booted()
        m.feed_line("W (300000) Ch370Bus: xfer_fail addr=13 cmd=0x51 st=timeout stage=R out_nak=0")
        self.assertEqual(m.xfer_fails, 1)
        self.assertEqual(m.failures(), [])
        self.assertEqual([e["kind"] for e in m.events], ["xfer_fail"])


if __name__ == "__main__":
    unittest.main()
