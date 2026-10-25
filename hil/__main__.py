"""命令行入口：python -m hil {psu,run,soak,camplug,camlive,tcpchaos,discover,summary} ..."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .camplug import CamTiming, CameraBench, is_camplug_dir, run_camplug, summarize_camplug
from .runner import Bench, Timing, discover, run
from .soak import format_result, run_soak
from .summary import summarize
from .zebra_psu import ZebraPsu


def cmd_psu(args) -> None:
    bench = Bench.load(args.bench)
    psu = ZebraPsu.open(bench.psu_port)
    try:
        on = psu.refresh_output_state()
        if args.action == "on":
            psu.set_output(True)
        elif args.action == "off":
            psu.set_output(False)
        elif on:
            v, a = psu.setpoints()
            mv, ma = psu.measure()
            print(f"固件 {psu.version()}；输出开；设定 {v}V 限流 {a}A；实测 {mv}V {ma}A")
            return
        else:
            print("输出关（关着时不查询读数）")
            return
        print("输出已" + ("打开" if args.action == "on" else "关闭"))
    finally:
        psu.close()


def cmd_run(args) -> None:
    bench = Bench.load(args.bench)
    label = args.label or Path(args.bench).stem
    out = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
    t = Timing(off_s=args.off, deadline_ms=args.deadline_ms, tail_ms=args.tail_ms,
               settle_ms=args.settle_ms, reset=args.reset)
    run(bench, args.rounds, out, t, args.keep_all_logs, args.abort_after)
    text = summarize(out)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


def cmd_soak(args) -> None:
    bench = Bench.load(args.bench)
    label = args.label or f"soak-{Path(args.bench).stem}"
    out = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
    st = run_soak(bench, args.hours, out, off_s=args.off)
    text = format_result(st)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(f"运行目录: {out}")
    print(text)


def cmd_camplug(args) -> None:
    bench = CameraBench.load(args.bench)
    label = args.label or Path(args.bench).stem
    out = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
    t = CamTiming(off_s=args.off, on_wait_s=args.on_wait, pre_wait_s=args.pre_wait,
                  chaos_pulses=args.chaos)
    try:
        run_camplug(bench, args.rounds, out, t, args.keep_all_logs, args.abort_after)
    finally:
        # 中途出错也把已跑完的轮次汇总出来
        if (out / "rounds.csv").exists():
            text = summarize_camplug(out)
            (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
            print(text)


def cmd_camlive(args) -> None:
    from .camlive import Timing, run_camlive

    t = Timing(open_timeout_s=args.open_timeout, hold_s=args.hold, long_hold_s=args.long_hold,
               gap_cold_s=args.gap_cold, stall_s=args.stall)
    run_camlive(args.platform, args.console, args.plan, args.client, Path(args.out), t, args.shuffle)


def cmd_tcpchaos(args) -> None:
    import asyncio

    from .tcpchaos import serve

    asyncio.run(serve(args.listen, args.upstream, args.control))


def cmd_summary(args) -> None:
    d = Path(args.dir)
    from .camlive import is_camlive_dir, summarize_camlive

    if is_camlive_dir(d):
        print(summarize_camlive(d))
        return
    print(summarize_camplug(d) if is_camplug_dir(d) else summarize(d))


def cmd_discover(args) -> None:
    tree, raw = discover(args.psu_port, args.console_port)
    log = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}-discover.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(raw)
    devs = sorted(tree.devices.values(), key=lambda d: [int(x) if x.isdigit() else -1
                                                         for x in d.path.split(".")])
    for d in devs:
        print(f"{d.path:<12} {d.vidpid}  {'HUB ' if d.is_hub else '    '}{d.name:<20} "
              f"{d.how:<4} {d.seen_ms - tree.online_ms:>6}ms")
    print(f"共 {len(devs)} 个设备；枚举失败日志 {tree.enum_fail_lines} 行；原始日志 {log}")
    if args.write:
        bench = {
            "name": args.name, "psu_port": args.psu_port, "console_port": args.console_port,
            "volt": args.volt,
            "devices": [{"path": d.path, "vidpid": d.vidpid, "name": d.name} for d in devs],
        }
        Path(args.write).write_text(json.dumps(bench, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
        print(f"已写台架文件 {args.write}，请核对设备清单")


def main() -> None:
    p = argparse.ArgumentParser(prog="hil", description="CH370 USB 断电台 / 摄像头插拔台")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("psu", help="查看或开关电源输出")
    sp.add_argument("action", choices=["status", "on", "off"])
    sp.add_argument("--bench", required=True)
    sp.set_defaults(func=cmd_psu)

    sr = sub.add_parser("run", help="跑 N 轮断电上电")
    sr.add_argument("--bench", required=True)
    sr.add_argument("--rounds", type=int, required=True)
    sr.add_argument("--label")
    sr.add_argument("--off", type=float, default=5.0, help="每轮断电秒数")
    sr.add_argument("--reset", choices=["power", "warm"], default="power",
                    help="power=整板断电上电；warm=只拉 EN 复位 P4，CH370/hub/模块保持供电")
    sr.add_argument("--deadline-ms", type=int, default=10_000)
    sr.add_argument("--tail-ms", type=int, default=30_000,
                    help="没到齐时继续观察到 CH370 上线后多少毫秒，看会不会自己恢复")
    sr.add_argument("--settle-ms", type=int, default=1000,
                    help="设备到齐后再观察多久(看 IDENTIFY 是否失败要 ≥5000)")
    sr.add_argument("--out", default="results")
    sr.add_argument("--keep-all-logs", action="store_true", help="通过的轮次也保留日志")
    sr.add_argument("--abort-after", type=int, default=5,
                    help="开头连续这么多轮失败就中止(0 = 不中止)")
    sr.set_defaults(func=cmd_run)

    sk = sub.add_parser("soak", help="冷启动一次后保持供电，被动读串口 N 小时（长跑 S9）")
    sk.add_argument("--bench", required=True)
    sk.add_argument("--hours", type=float, required=True)
    sk.add_argument("--label")
    sk.add_argument("--off", type=float, default=5.0, help="开头冷启动的断电秒数")
    sk.add_argument("--out", default="results")
    sk.set_defaults(func=cmd_soak)

    sc = sub.add_parser("camplug", help="主板保持供电，只开关摄像头供电，统计 UVC 热插拔反应")
    sc.add_argument("--bench", required=True, help="摄像头台架文件（带 camera 字段）")
    sc.add_argument("--rounds", type=int, required=True)
    sc.add_argument("--label")
    sc.add_argument("--off", type=float, default=3.0, help="每轮摄像头断电秒数")
    sc.add_argument("--on-wait", type=float, default=40.0, help="上电后观察秒数")
    sc.add_argument("--pre-wait", type=float, default=20.0, help="断电前最多等多久确认摄像头在出流")
    sc.add_argument("--chaos", type=int, default=0,
                    help="乱插：每轮上电后再随机断电/上电 N 次（落在枚举/开流等中间态）")
    sc.add_argument("--out", default="results")
    sc.add_argument("--keep-all-logs", action="store_true", help="recovered 的轮次也保留日志")
    sc.add_argument("--abort-after", type=int, default=3,
                    help="连续这么多轮 offline/crash/not_ready/rig_fault 就中止(0 = 不中止)")
    sc.set_defaults(func=cmd_camplug)

    sd = sub.add_parser("discover", help="断电上电一次，抓当前拓扑，可写成台架文件")
    sd.add_argument("--psu-port", required=True)
    sd.add_argument("--console-port", required=True)
    sd.add_argument("--volt", type=float, default=5.0)
    sd.add_argument("--name", default="")
    sd.add_argument("--write", help="写出的台架文件路径")
    sd.add_argument("--out", default="results")
    sd.set_defaults(func=cmd_discover)

    sl = sub.add_parser("camlive", help="观众侧开播压测：租约 → ready → WHEP 首帧 → 断流/恢复")
    sl.add_argument("--platform", required=True, help="平台连接 JSON(仓库外;见 hil/camlive.py PlatformConfig)")
    sl.add_argument("--console", help="主板串口(by-id 路径);不给就不读串口")
    sl.add_argument("--plan", default="cold:10,warm:10,edge:10",
                    help="场景:轮数,逗号分隔;场景 cold/warm/edge/stall/reset/long")
    sl.add_argument("--client", choices=["admin", "admin2", "ideal"], default="admin")
    sl.add_argument("--shuffle", action="store_true", help="打乱场景顺序")
    sl.add_argument("--open-timeout", type=float, default=30.0)
    sl.add_argument("--hold", type=float, default=15.0)
    sl.add_argument("--long-hold", type=float, default=300.0)
    sl.add_argument("--gap-cold", type=float, default=25.0)
    sl.add_argument("--stall", type=float, default=3.0)
    sl.add_argument("--out", default="results")
    sl.set_defaults(func=cmd_camlive)

    sx = sub.add_parser("tcpchaos", help="推流故障注入代理(透明转发 + stall/reset/blackhole)")
    sx.add_argument("--listen", default="0.0.0.0:47936")
    sx.add_argument("--upstream", default="127.0.0.1:47937")
    sx.add_argument("--control", default="127.0.0.1:47940")
    sx.set_defaults(func=cmd_tcpchaos)

    ss = sub.add_parser("summary", help="重新汇总一次运行")
    ss.add_argument("dir")
    ss.set_defaults(func=cmd_summary)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
