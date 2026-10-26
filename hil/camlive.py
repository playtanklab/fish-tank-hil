"""直播开播压测台：反复“建观看租约 → 等 ready → WHEP 收帧 → 退出”，统计开播成功率、首帧时间、断流与恢复。

和 camplug 的区别：camplug 关注摄像头热插拔时主板的反应；camlive 站在观众这一侧，量“点开画面能不能出来、
要多久、看着看着会不会断”。主板、摄像头、网络都不断电，只通过平台 API、可选的推流故障注入代理
(``hil.tcpchaos``)制造场景。

每轮的观众行为按 ``--client`` 选：

- ``admin``(默认)：照搬 admin 后台播放器(apps/admin/src/features/camera/{use-live-lease,live-player,whep}.ts)：
  ready 之前每 1.5 s 查一次状态，ready 之后只靠 20 s 一次的续约刷新；WHEP 失败后按 401→0.8 s、404→1 s、
  其他→2 s 用**同一个地址**重连；WHEP 建上但一直没有帧时不会主动放弃。复现用户看到的“加载不出来”。
- ``admin2``：修复后的 admin 播放器(fish-tank-platform fix/live-reliability)：ready 后也每 5 s 查状态；
  401/403/404 先刷新状态再 1 s 重连；首帧 8 s 没来、播放中 6 s 没有新帧都刷新状态后重连。
- ``admin3``：admin2 + 开播提速(fish-tank-platform fix/live-reliability 之后)：建租约后头 15 s 每 0.5 s
  查一次状态，之后每 1.5 s。
- ``admin4``：admin3 + 断流重连改进(fish-tank-platform fix/live-reliability 6653b5c)：任何断流(WHEP 失败、
  对端关连接、看门狗)都先刷新状态，之后 10 s 内每 0.5 s 查；地址变了立即连新地址，地址没变才 1 s
  (网络失败 2 s)后用原地址重试。
- ``ideal``：对照组。首帧 6 s 没来、或播放中断流 2 s 就立即重查租约状态，地址变了马上换。

场景(``--plan`` 里的名字)：

======== ==================================================================
cold     上一轮释放后等 ``gap_cold`` 秒(设备早已停播)再开，量冷启动
warm     上一轮释放后等 3 s 就开(平台 10 s 宽限期内，设备仍在推)
edge     释放后等 10.5 s 再开(平台刚好下发停播，与新租约赛跑)
stall    开播后第 10 s 让推流链路卡 ``stall_s`` 秒(需要 tcpchaos)，量恢复
reset    开播后第 10 s 重置推流 TCP 连接(需要 tcpchaos)，量恢复
long     连续看 ``long_hold_s`` 秒，统计播放中的断流
plug     开播后第 10 s 给摄像头断电 ``plug_off_s`` 秒再上电(需要 ``--psu``)，量上电到恢复出画面
plugcold 摄像头断电 ``plug_off_s`` 秒、上电后立即开播(需要 ``--psu``)，量首帧
view     开播后第 10 s 切换正面/侧面(需要平台配置里的 ``device_id``)，量切换到看到新镜头画面。
         编码后正面 640x480、侧面 640x464(采集 472 向下对齐 16)，按解码帧高度认新画面
======== ==================================================================

恢复时间：stall/reset 从注入算到恢复判定(断流后 1 s 内收到 ≥5 帧，含约 0.25 s 判定延迟)；
plug 从上电命令发出算到上电后第一帧；view 从切换请求发出算到第一帧新镜头画面。

插拔用 ZeBra 数控电源(``hil.zebra_psu``)，输出只接摄像头；台架要求和 camplug 相同(设定 5.0 V)。

结果分类：``ok``、``slow``(首帧 > 10 s)、``open_timeout``(``open_timeout`` 内没出首帧，即“加载不出来”)、
``lease_error``；故障场景另记 ``recovered`` / ``recover_timeout``。

串口全程后台读取(不碰 DTR/RTS)，原始日志按主机时间写进结果目录；固件关键事件按 ``RULES`` 归到各轮。

平台连接信息(地址、CA、账号、camera id)放在仓库外的 JSON 文件里，用 ``--platform`` 指定，格式见
``PlatformConfig``。结果目录与原始日志不进仓库。
"""

from __future__ import annotations

import asyncio
import csv
import http.cookiejar
import json
import os
import queue
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

KIND = "camlive"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# 固件日志 → 事件。措辞跟随 fish-tank-esp32 main/camera/rtmp_publisher.cc、
# boards/mhpsy/smart-fishtank/{smart_fishtank_board,camera_live_service,media_session_provider}.cc、
# main/camera/h264_encode_service.cc。新固件的诊断行(LiveDiag)也在这里。
RULES: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("crash", r"Guru Meditation|abort\(\) was called|Task watchdog got triggered|[Ss]tack overflow"
              r"|assert failed|CORRUPT HEAP"),
    ("rom", r"^ESP-ROM:"),
    ("app_version", r"app_init: App version:\s+(\S+)"),
    ("lease_started", r"CameraLive: live lease started"),
    ("lease_stopped", r"CameraLive: live lease stopped"),
    ("pub_stopped", r"camera live lease inactive; publisher stopped"),
    ("enc_start", r"H264Enc: streaming start(?: view=(\w+))?"),
    ("view_req", r"H264Enc: desired view -> (\w+)"),
    ("cam_gone", r"UvcCamera: active device addr=\d+ gone"),
    ("usb_attach", r"UsbDeviceInventory: USB attach addr=\d+ vid:pid=([0-9a-f]{4}:[0-9a-f]{4})"),
    ("uvc_stream_started", r"UvcCamera: stream started"),
    ("enc_stop", r"H264Enc: streaming stop"),
    ("enc_enter_fail", r"H264Enc: EnterStreaming failed"),
    ("enc_starve", r"H264Enc: (frame starvation|5s no frame)"),
    ("cam_busy", r"H264Enc: camera busy/absent"),
    ("no_slot", r"captured camera has no mapped physical slot"),
    ("session_fail", r"session request failed err=(\S+) status=(-?\d+)"),
    ("rtmp_connected", r"Rtmps: secure publish connected"),
    ("rtmp_ended", r"Rtmps: publish ended; reconnect with a new credential \(error=(\d+)\)"),
    ("rtmp_switch", r"Rtmps: camera switched; republishing"),
    ("rtmp_sockets", r"Rtmps: reclaimed (\d+) socket"),
    ("live_diag", r"LiveDiag: (.*)$"),
    ("governor", r"Governor: bitrate=(\d+) paused=(\d)"),
    ("camstat", r"CamStat: (.*)$"),
]]
CAMSTAT_KV = re.compile(r"(\w+)=(-?\d+)")


def match_line(line: str) -> tuple[str, tuple[str, ...]] | None:
    line = ANSI.sub("", line)
    for kind, pat in RULES:
        m = pat.search(line)
        if m:
            return kind, m.groups()
    return None


# ── 平台连接 ──────────────────────────────────────────────────────────────
@dataclass
class PlatformConfig:
    api: str                      # https://host:port
    ca: str | None                # 自签 CA(本机联调);None 用系统信任
    login_file: str               # email=… / password=… 两行
    camera_id: str
    resolve: dict[str, str] = field(default_factory=dict)  # 进程内固定解析(绕过 fake-ip DNS)
    chaos_control: str | None = None                       # tcpchaos 控制口
    device_id: str | None = None                           # 设备 MAC(切镜头写 host 属性用)

    @classmethod
    def load(cls, path: str | Path) -> "PlatformConfig":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["api"], d.get("ca"), d["login_file"], d["camera_id"], d.get("resolve", {}),
                   d.get("chaos_control"), d.get("device_id"))


def install_resolve(mapping: dict[str, str]) -> None:
    if not mapping:
        return
    orig = socket.getaddrinfo

    def fixed(host, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return orig(mapping.get(host, host), *a, **kw)

    socket.getaddrinfo = fixed
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(k, None)


class Api:
    def __init__(self, cfg: PlatformConfig):
        self.cfg = cfg
        ctx = ssl.create_default_context(cafile=cfg.ca) if cfg.ca else ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.ProxyHandler({}),
        )
        self.csrf = ""

    def req(self, method: str, path: str, body: object = None, *, raw: bytes | None = None,
            ctype: str = "application/json", headers: dict | None = None,
            timeout: float = 15) -> tuple[int, object, dict]:
        url = path if path.startswith("http") else self.cfg.api + path
        h = {"Origin": self.cfg.api}
        if self.csrf and method not in ("GET", "HEAD"):
            h["X-CSRF-Token"] = self.csrf
        data = raw if body is None else json.dumps(body).encode()
        if data is not None:
            h["Content-Type"] = ctype
        h.update(headers or {})
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                b, status, hdrs = resp.read(), resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            b, status, hdrs = e.read(), e.code, dict(e.headers)
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": str(e)}, {}
        try:
            parsed: object = json.loads(b) if b else None
        except ValueError:
            parsed = b
        return status, parsed, hdrs

    def login(self) -> None:
        creds = {}
        for line in Path(self.cfg.login_file).read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if not sep:
                k, sep, v = line.partition(":")
            if sep:
                creds[k.strip()] = v.strip()
        st, body, _ = self.req("POST", "/api/session/login",
                               {"email": creds["email"], "password": creds["password"]})
        if st != 200:
            raise SystemExit(f"登录失败 HTTP {st}: {body}")
        self.csrf = body["csrf_token"]  # type: ignore[index]

    # 租约接口(fish-tank-platform apps/backend/src/media/routes-live.ts)
    def lease_path(self, lease_id: str = "") -> str:
        base = f"/api/cameras/{self.cfg.camera_id}/live/leases"
        return f"{base}/{lease_id}" if lease_id else base


# ── 串口 ──────────────────────────────────────────────────────────────────
class SerialTap(threading.Thread):
    """后台读串口：原始行带主机时间写文件，命中 RULES 的行变成事件。"""

    def __init__(self, port: str, raw_path: Path):
        super().__init__(daemon=True, name="camlive-serial")
        from .camplug import ConsoleReader

        self.reader = ConsoleReader(port)
        self.raw = raw_path.open("a", encoding="utf-8")
        self.events: list[tuple[float, str, tuple[str, ...], str]] = []
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.lines = 0

    def run(self) -> None:
        self.reader.start()
        while not self.halt.is_set():
            try:
                t, line = self.reader.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.lines += 1
            clean = ANSI.sub("", line)
            self.raw.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {clean}\n")
            hit = match_line(clean)
            if hit:
                with self.lock:
                    self.events.append((t, hit[0], hit[1], clean[:300]))
        self.raw.flush()

    def stop(self) -> None:
        self.halt.set()
        self.reader.stop()
        self.join(timeout=2)
        self.raw.close()

    def between(self, t0: float, t1: float) -> list[tuple[float, str, tuple[str, ...], str]]:
        with self.lock:
            return [e for e in self.events if t0 <= e[0] < t1]


# ── WHEP 读端 ─────────────────────────────────────────────────────────────
class Whep:
    """aiortc 版 WHEP 读者：记录每帧到达时刻。"""

    def __init__(self, api: Api):
        self.api = api
        self.pc = None
        self.location: str | None = None
        self.token = ""
        self.frames: list[float] = []
        self.heights: list[int] = []   # 与 frames 一一对应
        self.first_frame = asyncio.Event()
        self.state = "new"
        self.size: tuple[int, int] | None = None

    async def start(self, url: str, token: str) -> int:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        self.token = token
        # aiortc 不给配置时默认用 Google STUN;本机代理 DNS 下收集候选要卡 5 s,浏览器侧也只等 2 s。
        # MediaMTX 有公网/局域网 host 候选,读端不需要 STUN。
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc = pc
        pc.addTransceiver("video", direction="recvonly")

        @pc.on("connectionstatechange")
        async def _state() -> None:  # noqa: RUF029
            self.state = pc.connectionState

        @pc.on("track")
        def _track(track) -> None:  # noqa: ANN001
            async def pump() -> None:
                while True:
                    try:
                        f = await track.recv()
                    except Exception:  # noqa: BLE001
                        return
                    self.frames.append(time.monotonic())
                    self.heights.append(f.height)
                    self.size = (f.width, f.height)
                    self.first_frame.set()

            asyncio.ensure_future(pump())

        await pc.setLocalDescription(await pc.createOffer())
        st, body, hdrs = await asyncio.to_thread(
            self.api.req, "POST", url, raw=pc.localDescription.sdp.encode(), ctype="application/sdp",
            headers={"Authorization": f"Bearer {token}"})
        if st not in (200, 201):
            await pc.close()
            self.state = "closed"
            return st
        loc = hdrs.get("Location") or hdrs.get("location")
        self.location = loc
        await pc.setRemoteDescription(RTCSessionDescription(
            sdp=body.decode() if isinstance(body, bytes) else str(body), type="answer"))
        return st

    async def close(self) -> None:
        if self.pc is not None:
            await self.pc.close()
        if self.location:
            await asyncio.to_thread(self.api.req, "DELETE", self.location,
                                    headers={"Authorization": f"Bearer {self.token}"})
        self.state = "closed"


# ── 一轮 ──────────────────────────────────────────────────────────────────
@dataclass
class Timing:
    open_timeout_s: float = 30.0   # 这么久没出首帧 = 加载不出来
    hold_s: float = 15.0           # 首帧后看多久
    long_hold_s: float = 300.0
    gap_cold_s: float = 25.0       # 平台宽限 10 s + 设备停播/释放摄像头的余量
    gap_warm_s: float = 3.0
    gap_edge_s: float = 10.5
    fault_at_s: float = 10.0       # stall/reset 在首帧后多少秒注入
    stall_s: float = 3.0
    recover_timeout_s: float = 60.0
    stall_gap_s: float = 1.0       # 帧间隔超过这么久记一次断流
    plug_off_s: float = 3.0        # plug / plugcold 摄像头断电秒数


CSV_FIELDS = [
    "round", "scenario", "client", "started", "result", "detail", "lease_http", "ready_s", "first_frame_s",
    "whep_attempts", "whep_codes", "states", "frames", "fps", "max_gap_s", "stalls", "stall_total_s",
    "fault", "recover_s", "url_changes", "view", "fw_rtmp_connected", "fw_rtmp_ended", "fw_rtmp_errors",
    "fw_session_fail", "fw_no_slot", "fw_cam_gone", "fw_usb_attach", "fw_enc_start", "fw_enc_enter_fail", "fw_reboot", "fw_crash",
    "internal_min", "fw_gov_pause", "fw_gov_min_br", "fw_last_diag", "events",
]


@dataclass
class Round:
    round: int
    scenario: str
    client: str
    started: str
    result: str = ""
    detail: str = ""
    lease_http: int = 0
    ready_s: float | None = None
    first_frame_s: float | None = None
    whep_attempts: int = 0
    whep_codes: list[int] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    frames: int = 0
    fps: float | None = None
    max_gap_s: float | None = None
    stalls: int = 0
    stall_total_s: float = 0.0
    fault: str = ""
    recover_s: float | None = None
    url_changes: int = 0
    view: str = ""                 # view 场景:切到哪个镜头
    fw: dict = field(default_factory=dict)
    timeline: list[str] = field(default_factory=list)


FRONT_HEIGHT = 480                 # 编码后正面 640x480、侧面 640x464(fish-tank-esp32 main/camera/camera_view.cc)


class Runner:
    def __init__(self, api: Api, cfg: PlatformConfig, tap: SerialTap | None, t: Timing, client: str,
                 out: Path, psu=None):  # noqa: ANN001
        self.api, self.cfg, self.tap, self.t, self.client, self.out = api, cfg, tap, t, client, out
        self.psu = psu                 # hil.zebra_psu.ZebraPsu,只开关输出
        self.last_release = time.monotonic() - 3600
        self.view_initial: str | None = None   # 第一次切镜头前的镜头,跑完切回去

    async def power(self, on: bool) -> None:
        if self.psu is None:
            raise SystemExit("plug / plugcold 场景需要 --psu")
        await asyncio.to_thread(self.psu.set_output, on)

    async def set_view(self, view: str) -> int:
        if not self.cfg.device_id:
            raise SystemExit("view 场景需要平台配置里的 device_id")
        dev = self.cfg.device_id
        st, _ = await self.call("PUT", f"/api/devices/{dev}/modules/host:{dev}/desired",
                                {"props": {"camera_view": view}})
        return st

    def chaos(self, line: str) -> dict:
        from .tcpchaos import send_command

        if not self.cfg.chaos_control:
            raise SystemExit("场景需要 tcpchaos：platform 配置里缺 chaos_control")
        return send_command(self.cfg.chaos_control, line)

    async def call(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        st, body, _ = await asyncio.to_thread(self.api.req, method, path, body)
        return st, body if isinstance(body, dict) else {}

    async def run_round(self, idx: int, scenario: str) -> Round:
        t = self.t
        gap = {"warm": t.gap_warm_s, "edge": t.gap_edge_s}.get(scenario, t.gap_cold_s)
        wait = self.last_release + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        rec = Round(idx, scenario, self.client, datetime.now().strftime("%H:%M:%S"))
        pre_marks: list[tuple[float, str]] = []
        if scenario == "plugcold":
            # 设备已停播、摄像头空闲:断电 → 上电后立即开播
            pre_marks.append((time.monotonic(), "PSU OFF"))
            await self.power(False)
            await asyncio.sleep(self.t.plug_off_s)
            pre_marks.append((time.monotonic(), "PSU ON"))
            await self.power(True)
        t0 = time.monotonic()
        ideal = self.client == "ideal"
        admin4 = self.client == "admin4"
        admin3 = self.client == "admin3" or admin4
        admin2 = self.client == "admin2" or admin3

        def mark(what: str) -> None:
            rec.timeline.append(f"{time.monotonic() - t0:6.2f} {what}")

        for at, what in pre_marks:
            rec.timeline.append(f"{at - t0:6.2f} {what}")

        st, s = await self.call("POST", self.api.lease_path())
        rec.lease_http = st
        mark(f"lease POST {st}")
        if st != 201:
            rec.result, rec.detail = "lease_error", json.dumps(s, ensure_ascii=False)[:200]
            self.last_release = time.monotonic()
            return self.finish(rec, t0)
        lease_id = s["lease_id"]
        status = s

        def note(st_: dict) -> None:
            label = f"{st_.get('stream', {}).get('state')}/{st_.get('camera_state')}"
            if not rec.states or rec.states[-1] != label:
                rec.states.append(label)
                mark(f"status {label}")

        async def refresh() -> None:
            nonlocal status
            code, body = await self.call("GET", self.api.lease_path(lease_id))
            if code == 200:
                status = body
                note(status)

        note(status)
        next_renew = t0 + status.get("renew_interval_ms", 20000) / 1000
        fast_until = 0.0              # admin4:断流后快查状态到这个时刻

        def pending_poll_s(now: float) -> float:
            return 0.5 if (admin3 and now - t0 < 15) or now < fast_until else 1.5

        def ready_poll_s(now: float) -> float:
            return 0.5 if now < fast_until else 5.0

        next_poll = t0 + pending_poll_s(t0)
        whep: Whep | None = None
        frames: list[float] = []      # 跨重连累计
        heights: list[int] = []
        cur_url: str | None = None
        retry_url: str | None = None  # admin4:等着用这个地址重试;地址变了就不等
        whep_started = 0.0
        retry_at = 0.0
        hold_until: float | None = None
        hold = t.long_hold_s if scenario == "long" else t.hold_s
        faulty = scenario in ("stall", "reset", "plug", "view")
        if faulty:
            hold = max(hold, t.fault_at_s + t.recover_timeout_s)
        fault_at: float | None = None
        fault_end: float | None = None    # plug:上电命令发出时刻;其余场景 = fault_at
        pre_h: int | None = None          # view:切换前的帧高度
        try:
            while True:
                await asyncio.sleep(0.1)
                now = time.monotonic()
                if hold_until is None and now - t0 > t.open_timeout_s:
                    rec.result = "open_timeout"
                    rec.detail = f"last={rec.states[-1] if rec.states else '?'} whep={rec.whep_codes}"
                    break
                if hold_until is not None and now >= hold_until:
                    break
                if now >= next_renew:
                    code, body = await self.call("PUT", self.api.lease_path(lease_id))
                    mark(f"renew {code}")
                    if code == 200:
                        status = body
                        note(status)
                    next_renew = now + 20
                stream = status.get("stream", {})
                ready = stream.get("state") == "ready"
                if fault_at is not None and fault_end is None and scenario == "plug" \
                        and now - fault_at >= t.plug_off_s:
                    mark("PSU ON")
                    fault_end = time.monotonic()
                    await self.power(True)
                if whep is None:
                    # admin:ready 之前 1.5 s 轮询;ready 之后不再轮询(只靠续约)。ideal 一直轮询。
                    # admin4:断流后等重试期间(ready 也)按快查节奏轮询
                    if (not ready or ideal or (admin4 and retry_url is not None)) and now >= next_poll:
                        await refresh()
                        next_poll = time.monotonic() + pending_poll_s(now)
                        continue
                    if not ready:
                        continue
                    url_moved = admin4 and stream.get("whep_url") != retry_url
                    if now < retry_at and not url_moved:
                        continue
                    retry_url = None
                    if rec.ready_s is None:
                        rec.ready_s = round(now - t0, 2)
                    whep = Whep(self.api)
                    whep.frames = frames
                    whep.heights = heights
                    cur_url = stream["whep_url"]
                    rec.whep_attempts += 1
                    code = await whep.start(cur_url, stream["read_token"])
                    rec.whep_codes.append(code)
                    mark(f"WHEP POST {code}")
                    whep_started = time.monotonic()
                    if code not in (200, 201):
                        whep = None
                        if admin2:
                            retry_at = time.monotonic() + (2.0 if code == 0 else 1.0)
                        else:
                            retry_at = time.monotonic() + (0.8 if code in (401, 403) else 1.0 if code == 404 else 2.0)
                        if admin4:
                            retry_url = cur_url
                            fast_until = time.monotonic() + 10
                            next_poll = 0.0
                        elif ideal or code in (401, 403) or (admin2 and code != 0):
                            await refresh()
                    continue
                # 有 WHEP 会话
                if rec.first_frame_s is None and frames:
                    rec.first_frame_s = round(frames[0] - t0, 2)
                    mark("first frame")
                    hold_until = frames[0] + hold
                if faulty and fault_at is None and rec.first_frame_s is not None \
                        and now - (t0 + rec.first_frame_s) >= t.fault_at_s:
                    if scenario == "plug":
                        mark("PSU OFF")
                        fault_at = time.monotonic()
                        await self.power(False)
                        rec.fault = f"plug off {t.plug_off_s}s"
                    elif scenario == "view":
                        pre_h = heights[-1]
                        old = "front" if pre_h == FRONT_HEIGHT else "side"
                        rec.view = "side" if old == "front" else "front"
                        if self.view_initial is None:
                            self.view_initial = old
                        fault_at = time.monotonic()
                        code = await self.set_view(rec.view)
                        rec.fault = f"view {old}->{rec.view} PUT {code}"
                        mark(f"switch view -> {rec.view} ({code})")
                    else:
                        fault_at = fault_end = now
                        cmd = f"stall {t.stall_s}" if scenario == "stall" else "reset"
                        rec.fault = json.dumps(self.chaos(cmd))
                        mark(f"inject {cmd}")
                if fault_at is not None and rec.recover_s is None:
                    if scenario == "view":
                        new = [f for f, h in zip(frames, heights) if f > fault_at and h != pre_h]
                        if len(new) >= 5:
                            rec.recover_s = round(new[0] - fault_at, 2)
                            mark(f"recovered {rec.recover_s}s")
                            hold_until = now + 5
                    elif scenario == "plug":
                        after = [f for f in frames if fault_end is not None and f > fault_end]
                        if sum(1 for f in after if f > now - 1.0) >= 5:
                            rec.recover_s = round(after[0] - fault_end, 2)
                            mark(f"recovered {rec.recover_s}s")
                            hold_until = now + 5
                    else:
                        after = [f for f in frames if f > fault_at]
                        gapped = any(b - a > t.stall_gap_s for a, b in zip([fault_at] + after, after + [now]))
                        if gapped and sum(1 for f in after if f > now - 1.0) >= 5:
                            rec.recover_s = round(now - fault_at, 2)
                            mark(f"recovered {rec.recover_s}s")
                            hold_until = min(hold_until or now + 5, now + 5)
                    if rec.recover_s is None and now - fault_at > t.recover_timeout_s:
                        mark("recover timeout")
                        break
                dead = whep.state in ("failed", "closed")
                url_now = stream.get("whep_url")
                changed = bool(url_now) and url_now != cur_url
                last = frames[-1] if frames else whep_started
                silent = now - max(last, whep_started)
                need = dead or changed
                if admin2 and not need:
                    if now >= next_poll:
                        await refresh()
                        next_poll = time.monotonic() + ready_poll_s(now)
                        url_now = status.get("stream", {}).get("whep_url")
                        changed = bool(url_now) and url_now != cur_url
                        need = changed
                    limit = 6.0 if frames else 8.0
                    if not need and silent >= limit:
                        mark(f"watchdog {'stall' if frames else 'no first frame'} {limit:.0f}s")
                        await refresh()
                        url_now = status.get("stream", {}).get("whep_url")
                        changed = bool(url_now) and url_now != cur_url
                        need = True
                if ideal and not need and silent > 2.0 and now >= next_poll:
                    await refresh()
                    next_poll = time.monotonic() + 1.5
                    url_now = status.get("stream", {}).get("whep_url")
                    changed = bool(url_now) and url_now != cur_url
                    need = changed or silent > 8.0
                if admin4 and dead and not changed:
                    # 对端关了连接(推流断了多半换路径):先刷新,地址变了马上连
                    await refresh()
                    url_now = status.get("stream", {}).get("whep_url")
                    changed = bool(url_now) and url_now != cur_url
                if need:
                    rec.url_changes += int(changed)
                    mark(f"reconnect ({'url changed' if changed else whep.state if dead else 'silent'})")
                    await whep.close()
                    whep = None
                    retry_at = time.monotonic() + (0.0 if ideal or changed else 1.0 if admin2 else 2.0)
                    if admin4 and not changed:
                        retry_url = cur_url
                        fast_until = time.monotonic() + 10
                        next_poll = time.monotonic() + 0.5
        finally:
            if scenario == "plug" and fault_at is not None and fault_end is None:
                mark("PSU ON(收尾)")
                await self.power(True)
            if whep is not None:
                await whep.close()
            code, _ = await self.call("DELETE", self.api.lease_path(lease_id))
            mark(f"lease DELETE {code}")
            self.last_release = time.monotonic()
        self.frame_stats(rec, frames)
        if not rec.result:
            if rec.first_frame_s is None:
                rec.result = "open_timeout"
            elif faulty:
                rec.result = "recovered" if rec.recover_s is not None else "recover_timeout"
            else:
                rec.result = "slow" if rec.first_frame_s > 10 else "ok"
        return self.finish(rec, t0)

    def frame_stats(self, rec: Round, frames: list[float]) -> None:
        rec.frames = len(frames)
        if len(frames) >= 2:
            span = frames[-1] - frames[0]
            rec.fps = round((len(frames) - 1) / span, 1) if span > 0 else None
            gaps = [b - a for a, b in zip(frames, frames[1:])]
            rec.max_gap_s = round(max(gaps), 2)
            big = [g for g in gaps if g > self.t.stall_gap_s]
            rec.stalls = len(big)
            rec.stall_total_s = round(sum(big), 2)

    def finish(self, rec: Round, t0: float) -> Round:
        if self.tap is not None:
            evs = self.tap.between(t0 - 2, time.monotonic() + 0.5)
            c: dict[str, int] = {}
            errs: list[str] = []
            imin = None
            last_diag = ""
            gov_pause = 0
            gov_min = None
            for et, kind, groups, _line in evs:
                c[kind] = c.get(kind, 0) + 1
                if kind == "rtmp_ended":
                    errs.append(groups[0])
                if kind == "live_diag":
                    last_diag = groups[0]
                    continue  # 每 2 s 一行,不进时间线
                if kind == "governor":
                    gov_pause += int(groups[1] == "1")
                    gov_min = min(gov_min or 10**9, int(groups[0]))
                if kind == "camstat":
                    kv = dict(CAMSTAT_KV.findall(groups[0]))
                    if "internal_min" in kv:
                        imin = int(kv["internal_min"])
                if kind not in ("camstat",):
                    rec.timeline.append(f"{et - t0:6.2f} fw {kind} {' '.join(groups)[:80]}")
            rec.fw = {**c, "rtmp_errors": ",".join(errs), "internal_min": imin,
                      "gov_pause": gov_pause, "gov_min_br": gov_min, "last_diag": last_diag}
            rec.timeline.sort(key=lambda s: float(s.split()[0]))
        return rec


def round_row(r: Round) -> dict:
    fw = r.fw
    return {
        "round": r.round, "scenario": r.scenario, "client": r.client, "started": r.started,
        "result": r.result, "detail": r.detail, "lease_http": r.lease_http, "ready_s": r.ready_s,
        "first_frame_s": r.first_frame_s, "whep_attempts": r.whep_attempts,
        "whep_codes": " ".join(map(str, r.whep_codes)), "states": " > ".join(r.states),
        "frames": r.frames, "fps": r.fps, "max_gap_s": r.max_gap_s, "stalls": r.stalls,
        "stall_total_s": r.stall_total_s, "fault": r.fault, "recover_s": r.recover_s,
        "url_changes": r.url_changes, "view": r.view, "fw_rtmp_connected": fw.get("rtmp_connected", 0),
        "fw_rtmp_ended": fw.get("rtmp_ended", 0), "fw_rtmp_errors": fw.get("rtmp_errors", ""),
        "fw_session_fail": fw.get("session_fail", 0), "fw_no_slot": fw.get("no_slot", 0),
        "fw_cam_gone": fw.get("cam_gone", 0), "fw_usb_attach": fw.get("usb_attach", 0),
        "fw_enc_start": fw.get("enc_start", 0), "fw_enc_enter_fail": fw.get("enc_enter_fail", 0),
        "fw_reboot": fw.get("rom", 0), "fw_crash": fw.get("crash", 0),
        "internal_min": fw.get("internal_min"), "fw_gov_pause": fw.get("gov_pause", 0),
        "fw_gov_min_br": fw.get("gov_min_br"), "fw_last_diag": fw.get("last_diag", ""), "events": " | ".join(r.timeline)[:4000],
    }


def parse_plan(plan: str) -> list[str]:
    out: list[str] = []
    for part in plan.split(","):
        name, _, n = part.strip().partition(":")
        out += [name] * (int(n) if n else 1)
    return out


async def run_plan(api: Api, cfg: PlatformConfig, tap: SerialTap | None, t: Timing, client: str,
                   plan: list[str], out: Path, shuffle: bool, psu=None) -> None:  # noqa: ANN001
    if shuffle:
        random.shuffle(plan)
    runner = Runner(api, cfg, tap, t, client, out, psu)
    try:
        await _run_rounds(runner, plan, out)
    finally:
        if runner.view_initial is not None:
            code = await runner.set_view(runner.view_initial)
            print(f"镜头切回 {runner.view_initial}(PUT {code})", flush=True)


async def _run_rounds(runner: "Runner", plan: list[str], out: Path) -> None:
    # Ctrl-C / SIGTERM:跑完当前这一轮(释放租约、写结果)再停,不留半截租约
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    with (out / "rounds.csv").open("w", newline="", encoding="utf-8") as f, \
            (out / "rounds.jsonl").open("w", encoding="utf-8") as fj:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for i, sc in enumerate(plan, 1):
            if stop.is_set():
                print(f"收到停止信号,在第 {i} 轮前结束", flush=True)
                break
            rec = await runner.run_round(i, sc)
            w.writerow(round_row(rec))
            f.flush()
            fj.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            fj.flush()
            ff = f"{rec.first_frame_s:.1f}s" if rec.first_frame_s is not None else "-"
            extra = f" recover={rec.recover_s}" if rec.fault else ""
            extra += f" view={rec.view}" if rec.view else ""
            print(f"[{rec.started}] #{i:03d} {sc:<5} {rec.result:<15} ready={rec.ready_s} ff={ff} "
                  f"whep={rec.whep_codes} stalls={rec.stalls}{extra} fw_end={rec.fw.get('rtmp_ended', 0)} "
                  f"err={rec.fw.get('rtmp_errors', '')}", flush=True)


def is_camlive_dir(d: Path) -> bool:
    meta = d / "meta.json"
    return meta.exists() and json.loads(meta.read_text(encoding="utf-8")).get("kind") == KIND


def summarize_camlive(out: Path) -> str:
    rows = list(csv.DictReader((out / "rounds.csv").open(encoding="utf-8")))
    lines = [f"camlive 汇总 {out}", f"轮数 {len(rows)}"]
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["scenario"], []).append(r)

    def pct(vals: list[float], p: float) -> str:
        if not vals:
            return "-"
        vals = sorted(vals)
        k = min(len(vals) - 1, max(0, round(p / 100 * (len(vals) - 1))))
        return f"{vals[k]:.1f}"

    for sc, rs in by.items():
        res: dict[str, int] = {}
        for r in rs:
            res[r["result"]] = res.get(r["result"], 0) + 1
        ff = [float(r["first_frame_s"]) for r in rs if r["first_frame_s"]]
        rec = [float(r["recover_s"]) for r in rs if r["recover_s"]]
        stalls = sum(int(r["stalls"] or 0) for r in rs)
        lines.append(
            f"- {sc}: n={len(rs)} 结果={res} 首帧 p50={pct(ff, 50)} p90={pct(ff, 90)} max={pct(ff, 100)} s"
            + (f" 恢复 p50={pct(rec, 50)} max={pct(rec, 100)} s" if rec else "")
            + f" 播放中断流={stalls} 固件推流断开={sum(int(r['fw_rtmp_ended'] or 0) for r in rs)}"
            + f" 停推={sum(int(r.get('fw_gov_pause') or 0) for r in rs)}"
            + f" 重启={sum(int(r['fw_reboot'] or 0) for r in rs)}")
    fails = [r for r in rows if r["result"] in ("open_timeout", "lease_error", "recover_timeout")]
    if fails:
        lines.append("失败轮：")
        for r in fails:
            lines.append(f"  #{r['round']} {r['scenario']} {r['result']} {r['detail']} states={r['states']}")
            if r.get("fw_last_diag"):
                lines.append(f"      LiveDiag: {r['fw_last_diag']}")
    return "\n".join(lines)


PSU_VOLT = 5.0


def open_psu(port: str):  # noqa: ANN201
    """打开只给摄像头供电的 ZeBra 电源;设定不是 5.0 V 就拒绝(和 camplug 同一台架要求)。"""
    from .zebra_psu import ZebraPsu

    psu = ZebraPsu.open(port)
    try:
        if not psu.refresh_output_state():
            print("电源输出原本是关的(摄像头没供电),先打开", flush=True)
            psu.set_output(True)
            time.sleep(1)
        volt, _ = psu.setpoints()
        if abs(volt - PSU_VOLT) > 0.05:
            raise SystemExit(f"电源设定 {volt}V 和台架要求 {PSU_VOLT}V 不一致,拒绝运行")
    except BaseException:
        psu.close()
        raise
    return psu


def run_camlive(platform: str, console: str | None, plan: str, client: str, out_root: Path,
                t: Timing, shuffle: bool = False, psu_port: str | None = None) -> Path:
    cfg = PlatformConfig.load(platform)
    names = parse_plan(plan)
    if {"plug", "plugcold"} & set(names) and not psu_port:
        raise SystemExit("plug / plugcold 场景需要 --psu")
    if "view" in names and not cfg.device_id:
        raise SystemExit("view 场景需要平台配置里的 device_id")
    install_resolve(cfg.resolve)
    api = Api(cfg)
    api.login()
    psu = open_psu(psu_port) if psu_port else None
    out = out_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{KIND}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({
        "kind": KIND, "plan": plan, "client": client, "timing": asdict(t),
        "camera_id": cfg.camera_id, "console": console, "psu": psu_port}, ensure_ascii=False, indent=2), encoding="utf-8")
    tap = SerialTap(console, out / "console.log") if console else None
    if tap:
        tap.start()
    try:
        asyncio.run(run_plan(api, cfg, tap, t, client, names, out, shuffle, psu))
    except KeyboardInterrupt:
        print("中断", flush=True)
    finally:
        if psu is not None:
            # 不管怎么结束都让摄像头恢复供电
            try:
                if psu.output_on is not True:
                    psu.set_output(True)
            except Exception as e:  # noqa: BLE001
                print(f"收尾时开电源输出失败: {e}", flush=True)
            psu.close()
        if tap:
            tap.stop()
    summary = summarize_camlive(out)
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return out
