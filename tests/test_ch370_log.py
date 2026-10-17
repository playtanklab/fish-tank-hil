import unittest
from pathlib import Path

from hil.ch370_log import Ch370Tree, Expected, compare

FIXTURE = Path(__file__).parent / "fixtures" / "boot_2modules.log"
EXPECTED = [
    Expected("root", "1a86:809e"),
    Expected("root.6", "1a86:809e"),
    Expected("root.7", "1a86:809e"),
    Expected("root.6.2", "2402:5605"),
    Expected("root.7.2", "2402:5601"),
]


def tree_from(text: str) -> Ch370Tree:
    t = Ch370Tree()
    t.feed(text.encode())
    return t


class Ch370LogTest(unittest.TestCase):
    def test_real_boot_capture(self):
        # 2026-10-17 5C84388205 实机抓取：两个模块都由增量扫描补上
        tree = tree_from(FIXTURE.read_text())
        self.assertEqual(tree.online_ms, 2225)
        self.assertEqual(tree.first_ms, 577)
        missing, extra, last_ms = compare(tree, EXPECTED)
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])
        self.assertEqual(last_ms, 5464)
        self.assertEqual(tree.devices["root.6.2"].how, "hot")
        self.assertEqual(tree.devices["root.6"].how, "cold")
        self.assertTrue(tree.devices["root.7"].is_hub)
        self.assertEqual(tree.resets, [])

    def test_chunked_feed_matches_whole(self):
        # 串口按任意字节交付数据，7 字节一块会切断中文字符
        data = FIXTURE.read_bytes()
        t = Ch370Tree()
        for i in range(0, len(data), 7):
            t.feed(data[i:i + 7])
        self.assertEqual(t.devices, tree_from(data.decode()).devices)
        self.assertEqual(t.devices["root.7.2"].name, "PUMP-CHAMBER")

    def test_missing_device_reported(self):
        text = "\n".join(l for l in FIXTURE.read_text().splitlines() if "PUMP-CHAMBER" not in l)
        missing, _, _ = compare(tree_from(text + "\n"), EXPECTED)
        self.assertEqual([m.path for m in missing], ["root.7.2"])

    def test_unplug_removes_subtree(self):
        text = FIXTURE.read_text() + "I (9000) UsbTreeCh370: 热拔出: hub#1 端口6 设备移除,回收地址\n"
        tree = tree_from(text)
        self.assertNotIn("root.6", tree.devices)
        self.assertNotIn("root.6.2", tree.devices)
        self.assertIn("root.7.2", tree.devices)

    def test_root_detach_clears_all(self):
        text = FIXTURE.read_text() + "I (9000) UsbTreeCh370: CH370 根口 attach: 1 -> 0 (拔出)\n"
        self.assertEqual(tree_from(text).devices, {})

    def test_enum_failure_counted(self):
        line = ("W (3000) UsbTreeCh370: hub#1 端口3 复位/枚举失败(第1次,退避1 tick) "
                "status=0x0303 change=0x0000 [CONN ENABLE POWER LOW-SPEED]\n")
        self.assertEqual(tree_from(FIXTURE.read_text() + line).enum_fail_lines, 1)

    def test_reset_detected(self):
        self.assertTrue(tree_from(FIXTURE.read_text() + "ESP-ROM:esp32p4-eco2-20240710\n").resets)
        self.assertTrue(tree_from(FIXTURE.read_text() + "I (100) boot: x\n").resets)
        # 串口打开得够早、开头就是 ROM 横幅，不算复位
        self.assertFalse(tree_from("ESP-ROM:esp32p4\n" + FIXTURE.read_text()).resets)

    def test_interleaved_task_logs_are_not_reset(self):
        # 2026-10-17 第 36 轮实机日志：不同任务的日志先取时间戳再打印，出现 3ms 回退
        text = (
            "I (2752) transport: Attempt connection with slave: retry[0]\n"
            "W (2759) H_SDIO_DRV: Reset slave using GPIO[54]\n"
            "I (2762) os_wrapper_esp: GPIO [54] configured\n"
            "I (2759) UsbTreeCh370:   hub#1 端口7: addr=3 1a86:809e <HUB> 'USB2.0 HUB'\n"
        )
        tree = tree_from(text)
        self.assertEqual(tree.resets, [])
        self.assertEqual(tree.last_ms, 2762)

    def test_ansi_colors_stripped(self):
        text = "".join(f"\x1b[0;32m{l}\x1b[0m\n" for l in FIXTURE.read_text().splitlines())
        missing, _, _ = compare(tree_from(text), EXPECTED)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
