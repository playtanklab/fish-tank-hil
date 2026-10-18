# fish-tank-hil

鱼缸主板 CH370 USB 树的断电台（HIL）。用一台串口可控的数控电源反复给整板断电、上电，
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

## 许可证

MIT，见 [LICENSE](LICENSE)。
