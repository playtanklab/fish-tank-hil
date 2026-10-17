import unittest

from hil.zebra_psu import PsuError, PsuShutdown, ZebraPsu


class FakeSerial:
    """按收到的命令吐出预设回复。"""

    def __init__(self, replies: dict[bytes, bytes]):
        self.replies = replies
        self.written: list[bytes] = []
        self._pending = b""

    @property
    def in_waiting(self):
        return len(self._pending)

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data: bytes):
        self.written.append(data)
        self._pending += self.replies.get(data, b"")

    def read(self, n: int) -> bytes:
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        pass


def psu(replies) -> tuple[ZebraPsu, FakeSerial]:
    ser = FakeSerial(replies)
    return ZebraPsu(ser, reply_timeout=0.05), ser


class ZebraPsuTest(unittest.TestCase):
    def test_commands_have_no_line_ending(self):
        p, ser = psu({b"AT_OUTPUT": b"AT_OUTPUT:1\r\n"})
        self.assertTrue(p.refresh_output_state())
        self.assertEqual(ser.written, [b"AT_OUTPUT"])

    def test_measure_parses_floats(self):
        p, _ = psu({b"AT_OUTPUT": b"AT_OUTPUT:1\r\n", b"AT_Q_VOLT_OUT": b"AT_Q_VOLT_OUT:5.037\r\n",
                    b"AT_Q_CURRENT_OUT": b"AT_Q_CURRENT_OUT:0.586\r\n"})
        p.refresh_output_state()
        self.assertEqual(p.measure(), (5.037, 0.586))

    def test_no_queries_while_output_off(self):
        p, ser = psu({b"AT_OUTPUT": b"AT_OUTPUT:1\r\n", b"AT_OUTPUT=0": b"AT_OUTPUT:OK\r\n"})
        p.set_output(False)
        with self.assertRaises(PsuError):
            p.measure()
        self.assertNotIn(b"AT_Q_VOLT_OUT", ser.written)

    def test_unrelated_lines_skipped(self):
        # 固件 3.18 实测原始字节：先一行只以 \n 结尾的 GBK "打开输出 "，再回 OK
        p, _ = psu({b"AT_OUTPUT=1": b"\xb4\xf2\xbf\xaa\xca\xe4\xb3\xf6 \nAT_OUTPUT:OK\r\n"})
        p.set_output(True)
        self.assertTrue(p.output_on)
        self.assertEqual(p.noise, ["打开输出"])

    def test_shutdown_reply_raises(self):
        p, _ = psu({b"AT_VERSION": "已关机\r\n".encode("gbk")})
        with self.assertRaises(PsuShutdown):
            p.version()

    def test_timeout_raises(self):
        p, _ = psu({})
        with self.assertRaises(PsuError):
            p.version()


if __name__ == "__main__":
    unittest.main()
