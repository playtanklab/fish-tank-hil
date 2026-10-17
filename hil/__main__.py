"""命令行入口：python -m hil {psu,run,summary} ..."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .runner import Bench, Timing, run
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
    t = Timing(off_s=args.off, deadline_ms=args.deadline_ms)
    run(bench, args.rounds, out, t, args.keep_all_logs)
    text = summarize(out)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


def main() -> None:
    p = argparse.ArgumentParser(prog="hil", description="CH370 USB 断电台")
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
    sr.add_argument("--deadline-ms", type=int, default=10_000)
    sr.add_argument("--out", default="results")
    sr.add_argument("--keep-all-logs", action="store_true", help="通过的轮次也保留日志")
    sr.set_defaults(func=cmd_run)

    ss = sub.add_parser("summary", help="重新汇总一次运行")
    ss.add_argument("dir")
    ss.set_defaults(func=lambda a: print(summarize(Path(a.dir))))

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
