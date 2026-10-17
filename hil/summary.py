"""汇总一次运行的 rounds.csv。"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


def percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def summarize(out_dir: Path) -> str:
    rows = list(csv.DictReader(open(out_dir / "rounds.csv", encoding="utf-8")))
    results = Counter(r["result"] for r in rows)
    valid = [r for r in rows if r["result"] in ("pass", "usb_missing", "boot_fail")]
    fails = [r for r in valid if r["result"] != "pass"]
    conv = [int(r["converge_ms"]) for r in rows if r["converge_ms"]]
    missing = Counter(m for r in rows for m in r["missing"].split(";") if m)

    n, k = len(valid), len(fails)
    lines = [f"运行目录: {out_dir}", f"总轮数 {len(rows)}；分类: {dict(results)}"]
    if n:
        bound = f"95% 置信上界约 {3 / n:.2%}（三法则）" if k == 0 else f"观测失败率 {k / n:.2%}"
        lines.append(f"有效轮次 {n}，失败 {k}，{bound}")
    if conv:
        lines.append(f"收敛时间（CH370 上线到最后一个期望设备）ms: P50={percentile(conv, .5)} "
                     f"P95={percentile(conv, .95)} max={max(conv)}")
    hot = [int(r["hot"]) for r in rows if r["hot"]]
    if hot:
        lines.append(f"靠增量扫描补上的设备数（每轮）: 平均 {sum(hot) / len(hot):.2f}，最多 {max(hot)}")
    if missing:
        lines.append("缺失分布: " + ", ".join(f"{m} ×{c}" for m, c in missing.most_common()))
    rig = [r for r in rows if r["result"] in ("rig_fault", "reset")]
    for r in rig[:10]:
        lines.append(f"  第 {r['round']} 轮 {r['result']}: {r['detail']}")
    return "\n".join(lines)
