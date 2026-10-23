"""ZeBra 移动数控电源 V3 的串口 AT 控制（只开放开关输出和读数）。

协议要点（固件 3.18 实测，手册见 README）：
- 115200 8N1；命令后面不能加 \\r\\n，加了电源不回应。
- 回复以 \\r\\n 结尾，形如 ``AT_OUTPUT:OK``、``AT_Q_VOLT_OUT:5.037``；中文提示是 GBK。
  开输出时先回一行 ``打开输出 \\n``（只有 \\n），约 0.65 s 后才到。
- 关输出后马上连续发查询，实测电源会回"已关机"并整机关机，只能人工长按旋钮开机。
  所以这里规定：输出关着的时候，除了开输出，不发任何命令。

故意不提供改电压、改限流的方法：断电台只需要开关，误改电压可能烧板子。
"""

from __future__ import annotations

import time

BAUD = 115200
SHUTDOWN_TEXT = "已关机"


class PsuError(RuntimeError):
    pass


class PsuShutdown(PsuError):
    """电源报告自己已关机，需要人工长按旋钮开机。"""


class ZebraPsu:
    def __init__(self, ser, reply_timeout: float = 1.5):
        # ser 只需要 write / read / reset_input_buffer / in_waiting，测试里可以换成假对象
        self._ser = ser
        self._reply_timeout = reply_timeout
        self._output_on: bool | None = None
        self.noise: list[str] = []  # 收到的、和当前命令无关的行，留给调用方记录

    @classmethod
    def open(cls, port: str) -> "ZebraPsu":
        import serial

        ser = serial.Serial(port, BAUD, timeout=0.05)
        time.sleep(0.2)
        return cls(ser)

    def close(self) -> None:
        self._ser.close()

    @property
    def output_on(self) -> bool | None:
        return self._output_on

    # ---- 底层收发 ----

    def _read_line(self, deadline: float, buf: bytearray) -> str | None:
        while time.monotonic() < deadline:
            # 开输出时电源先回一行只以 \n 结尾的中文提示，所以按 \n 切行
            idx = buf.find(b"\n")
            if idx >= 0:
                line = bytes(buf[:idx])
                del buf[: idx + 1]
                return line.decode("gbk", errors="replace").strip()
            chunk = self._ser.read(max(1, self._ser.in_waiting))
            if chunk:
                buf.extend(chunk)
        return None

    def _check_pending(self) -> None:
        """清缓冲前先看一眼两条命令之间电源主动发来的东西：自己关机时会报“已关机”，不能丢掉。"""
        pending = self._ser.read(self._ser.in_waiting) if self._ser.in_waiting else b""
        if not pending:
            return
        text = pending.decode("gbk", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self.noise.append(line.strip())
        if SHUTDOWN_TEXT in text:
            self._output_on = None
            raise PsuShutdown(f"电源在两条命令之间主动报“{SHUTDOWN_TEXT}”，需要人工长按旋钮开机")

    def _transact(self, cmd: str) -> str:
        """发一条命令，返回以 ``<命令名>:`` 开头的那一行冒号后的内容。"""
        name = cmd.split("=", 1)[0]
        self._check_pending()
        self._ser.reset_input_buffer()
        self._ser.write(cmd.encode("ascii"))
        deadline = time.monotonic() + self._reply_timeout
        buf = bytearray()
        while True:
            line = self._read_line(deadline, buf)
            if line is None:
                raise PsuError(f"{cmd}: {self._reply_timeout}s 内没有回复")
            if SHUTDOWN_TEXT in line:
                self._output_on = None
                raise PsuShutdown(f"{cmd}: 电源回复“{line}”，已整机关机，需要人工长按旋钮开机")
            if line.startswith(name + ":"):
                return line[len(name) + 1 :]
            if line:
                self.noise.append(line)

    def _query(self, cmd: str) -> str:
        if self._output_on is False:
            raise PsuError(f"输出关着时不允许查询（{cmd}），会触发电源关机")
        return self._transact(cmd)

    def _query_float(self, cmd: str) -> float:
        value = self._query(cmd)
        try:
            return float(value)
        except ValueError as e:
            raise PsuError(f"{cmd}: 无法解析回复 {value!r}") from e

    # ---- 对外接口 ----

    def refresh_output_state(self) -> bool:
        """查询当前输出状态。只在启动时调用；关机状态下查询过两次未出问题。"""
        value = self._transact("AT_OUTPUT")
        if value not in ("0", "1"):
            raise PsuError(f"AT_OUTPUT: 无法解析回复 {value!r}")
        self._output_on = value == "1"
        return self._output_on

    def set_output(self, on: bool) -> None:
        if on is False and self._output_on is None:
            self.refresh_output_state()
        value = self._transact(f"AT_OUTPUT={1 if on else 0}")
        if value != "OK":
            raise PsuError(f"AT_OUTPUT={1 if on else 0}: 电源回复 {value!r}")
        self._output_on = on

    def setpoints(self) -> tuple[float, float]:
        """返回 (设定电压 V, 限流 A)。"""
        return self._query_float("AT_VBUS"), self._query_float("AT_CURRENT")

    def measure(self) -> tuple[float, float]:
        """返回输出端实测 (电压 V, 电流 A)，只能在输出打开时调用。"""
        return self._query_float("AT_Q_VOLT_OUT"), self._query_float("AT_Q_CURRENT_OUT")

    def version(self) -> str:
        return self._transact("AT_VERSION")
