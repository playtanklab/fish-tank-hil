# fish-tank-hil

鱼缸主板 CH370 USB 树的断电台（HIL），以及 UVC 摄像头插拔台（见“摄像头插拔台”一节）。用一台串口可控的数控电源反复给整板断电、上电，
抓 P4 的串口日志，判断期望的 USB 模块是否在规定时间内全部枚举出来，统计缺失率和收敛时间。

背景和指标定义见固件仓的方案文档
`fish-tank-esp32/docs/superpowers/specs/2026-10-17-ch370-usb-host-robustness-options.md`（§5.4、§6）。

## 硬件

| 部分 | 现状 |
|---|---|
| 电源 | ZeBra 移动数控电源 V3（固件 3.18），扩展口经 CH340 接电脑；[手册与固件](https://gitee.com/uYuToo/SmartPowerV3) |
| 被测板 | smart-fishtank 主板，由电源输出供电（5 V）；断电时 P4、CH370、各级 hub、模块一起掉电 |
| 日志 | 板载 USB 转串口（`1a86:55d3`），连电脑的 USB 线**不带 5 V**，板子断电时串口一起消失 |

板载串口随整板断电，所以每轮上电后都要等它重新出现再打开。实测约 1.2 s 出现，打开后抓到的第一行是
P4 开机后约 577 ms 的日志，CH370 的冷枚举（约 2.2 s 开始）完整可见。用 pyserial 默认方式打开、
不碰 DTR/RTS，不会复位 P4（2026-10-17 在 5C84388205 上实测）。

## 电源控制的坑

- 115200 波特率；命令后面**不能加回车换行**。
- 开输出时先回一行只以 `\n` 结尾的 GBK 提示“打开输出”，约 0.65 s 后才回 `AT_OUTPUT:OK`。
- **输出关着时不要发查询命令。** 实测关输出后马上连续查询，电源会回“已关机”并整机关机，只能人工长按旋钮开机。
  `hil/zebra_psu.py` 在输出关闭期间拒绝查询。
- 电源菜单里要打开“外置供电不休眠”，并把“空载关闭输出”改为保持输出，否则长时间跑批会被它中途关掉。这两项没有 AT 命令。
- 驱动只开放开关输出和读数，**不提供改电压、改限流的方法**；运行前会核对设定电压与台架文件一致，不一致就拒绝运行。

## 用法

```sh
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
# 或：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m hil psu status --bench benches/5C84388205-2mod.json
.venv/bin/python -m hil run --bench benches/5C84388205-2mod.json --rounds 50 --label rig-accept
.venv/bin/python -m hil summary results/<运行目录>
# 只复位 P4（拉 EN，CH370/hub/模块保持供电），模拟 P4 OTA 后重启、崩溃重启
.venv/bin/python -m hil run --bench benches/5C84388205-full.json --reset warm --rounds 300 --settle-ms 3000
# 长跑（S9）：冷启动一次后保持供电，被动读串口 72 小时；每分钟刷新运行目录里的 status.json
.venv/bin/python -m hil soak --bench benches/5C84388205-full.json --hours 72
.venv/bin/python -m unittest discover -s tests
```

`run` 每轮：关输出 → 断电 `--off` 秒（默认 5，期间不发任何命令）→ 开输出 → 等板载串口出现并打开 →
抓日志直到期望设备全部出现后再观察 1 s，或 CH370 上线后 30 s 为止。结束后保持输出打开。

台架文件（`benches/*.json`）写明电源和板载串口的 by-id 路径、设定电压，以及期望设备的端口路径和 VID:PID。
路径从固件日志还原：根口设备是 `root`，根 hub 端口 6 上的是 `root.6`，再往下是 `root.6.2`。
注意 hub 的端口号和外壳上的插座编号不一定对应。

## 结果

`results/<时间>-<标签>/` 下有 `meta.json`、`rounds.csv`、`summary.txt`，失败轮次的完整日志在 `logs/`
（`--keep-all-logs` 保留全部）。`results/` 不进仓库：串口日志里可能有网络和设备标识信息。

每轮分类：

| 结果 | 含义 |
|---|---|
| `pass` | CH370 上线后 10 s 内期望设备全部出现 |
| `usb_missing` | 超过 10 s 仍有期望设备没出现，或最后一个设备来得太晚。这是要统计的失败 |
| `boot_fail` | 有日志但没等到 `CH370 online` |
| `reset` | 抓取中途 P4 又复位了一次，这一轮不可信 |
| `rig_fault` | 测试台自身问题：断电期间串口仍在、串口没出现或打不开、没数据、开头日志丢得太多 |

缺失率只按 `pass`/`usb_missing`/`boot_fail` 计算。零失败时按“三法则”给出 95% 置信上界约 3/N。

日志解析依赖固件 `main/boards/mhpsy/smart-fishtank/usb_tree_ch370.cc` 当前的日志格式；固件改了格式，
`hil/ch370_log.py` 和 `tests/fixtures/` 要一起改。

## 摄像头插拔台（camplug）

另一种接法：**主板另外供电，电源输出只接 UVC 摄像头**。关输出 = 摄像头掉线，主板和板载串口一直在线，
用来统计主板对摄像头热插拔的反应（固件改造前的基线、改造后的验收）。

```sh
.venv/bin/python -m hil psu status --bench benches/5C84388210-camera.json
.venv/bin/python -m hil camplug --bench benches/5C84388210-camera.json --rounds 20 --label after-fix
# 可调：--off 3（断电秒数）--on-wait 40（上电后观察秒数）--pre-wait 20（断电前确认在流最多等多久）
#       --abort-after 3（连续这么多轮 offline/crash/not_ready/rig_fault 就中止）--keep-all-logs
.venv/bin/python -m hil summary results/<运行目录>
```

摄像头台架文件写电源和板载串口的 by-id 路径、设定电压和摄像头 VID:PID：
`{"name", "psu_port", "console_port", "volt", "camera": {"vidpid": "13d3:784b"}}`。

全程由后台线程读板载串口（pyserial 默认打开，不碰 DTR/RTS，不会复位主板）。每轮：

1. 断电前确认摄像头在出流：上一轮或本次 `--pre-wait` 内看到 `stream started` 且之后没有掉线/重启迹象，
   再等出流稳定 10 s（让推流连上）。固件出流后没有周期日志，**第 1 轮无法被动确认**：听满 pre-wait、
   没看到反面迹象就假定在流（`pre=assumed`），开跑前请人工确认。确认不了记 `not_ready`，这一轮不动电源。
2. 关输出 `--off` 秒，期间不发任何电源命令；开输出；观察 `--on-wait` 秒。结束或异常都会把输出开回去。

每轮分类（`rounds.csv`）：

| 结果 | 含义 |
|---|---|
| `recovered` | 没重启，上电后摄像头重新出流（`UvcCamera: stream started`，恢复成功的主判据） |
| `rebooted` | 断电后主板整机重启；`stream_ms` 有值表示重启后恢复了出流 |
| `offline` | `--on-wait` 内没恢复出流，含恢复预算耗尽（`budget_exhausted`） |
| `crash` | Guru Meditation、abort、任务/中断看门狗，或复位原因是看门狗/掉电 |
| `not_ready` | 断电前确认不了摄像头在出流，没动电源 |
| `rig_fault` | 台架问题：断电后串口没数据、断电期间仍在出流、电源没回复 |

耗时列（ms，主机时间）：`drop_ms` 断电→第一条掉线迹象、`reboot_ms` 断电→ROM 横幅、`attach_ms` 上电→
`USB attach`（按台架 VID:PID 过滤）、`uvc_ms` 上电→`UVC connected`、`stream_ms` 上电→出流、
`publish_ms` 上电→推流连上。另记 usb err / URB 入队失败次数、teardown unsafe、受控重启序号
`restart_req`、开机打印的已用恢复预算 `budget_boot`、复位原因和事件摘要 `events`。
`summary.txt` 给出各类计数、恢复耗时 min/p50/p95/max、重启次数与原因、预算使用情况。
recovered 以外的轮次日志写入 `logs/`（每行 `[主机秒] 原始行`，带 `===== PSU OFF/ON =====` 标记，
和 `tests/fixtures/cam_*.log` 同格式，可以直接截成夹具）。

日志匹配规则集中在 `hil/camlog.py` 的 `RULES`（基于主固件 2.4.2.5 的措辞）。固件改造后措辞会变：
先用新固件跑几轮、看 `logs/` 里的原始行，改 `RULES`，再把新日志截成夹具补测试。

当前主固件（2.4.2.5）每次摄像头掉线都会受控整机重启，并消耗 RTC 里的恢复预算（30 分钟 2 次）；
预算用完后摄像头一直离线，只有整板断电能清。在这块台架上我们控制不了主板供电，跑基线要限量。

2026-10-23 首次实机运行遇到过：关输出约 3 s 后电源对 `AT_OUTPUT=1` 完全没有回复（空闲也不发任何字节），
摄像头保持断电，只能到电源面板上处理。原因未明；驱动现在会在发命令前检查电源主动发来的“已关机”。

## 许可证

MIT，见 [LICENSE](LICENSE)。
