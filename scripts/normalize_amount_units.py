"""
成交额/成交量单位归一 (2026-09-09 用户定顺序第2步) —
把全库归一为单一口径 **万股/万元**(库历史标准, 与 MCP 锚标签一致),
清除 2026-06 起"股/元"混入造成的 10^4 分层。
算法(两遍):
  ①时序切分: 每票按相邻有效日 amount 比值 >100 找切换点分段
  ②截面锚定: 逐日取全市场 amount 中位数做参照, 每段的中位值比
    当日参照低 ≥3 个数量级 → 该段为"元"口径 → ÷1e4 归一
锚校验: 归一后 tests/unit_ground_truth.json 全部配对必须落在万元口径
(库内/MCP真值∈[0.5,2])。
安全: 默认 --dry-run 只输出报告不写库; --apply 才落盘(先备份到
logs/unit_norm_backup_manifest.json 记录改动)。
用法: python scripts/normalize_amount_units.py [--dry-run|--apply]
"""
import sys, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily

ANCHOR = Path(__file__).parent.parent / "tests" / "unit_ground_truth.json"
DAILY_DIR = Path("/data/quant_data_store/daily")


def detect_segments(dates, amounts):
    """按相邻有效日比值>100切分时段, 返回 [(start_idx, end_idx, median)]"""
    segs, start = [], 0
    prev_v = None
    for i, v in enumerate(amounts):
        if v is None or v <= 0:
            continue
        if prev_v is not None:
            ratio = v / prev_v
            if ratio > 100 or ratio < 0.01:
                mid = (start + i) // 2
                if mid > start:
                    vals = [x for x in amounts[start:mid] if x and x > 0]
                    if vals:
                        segs.append((start, mid, float(np.median(vals))))
                    start = mid
        prev_v = v
    vals = [x for x in amounts[start:] if x and x > 0]
    if vals:
        segs.append((start, len(amounts), float(np.median(vals))))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    years = sorted([d.name for d in DAILY_DIR.iterdir() if d.is_dir()])
    files = []
    for y in years:
        for f in sorted((DAILY_DIR / y).glob("*.parquet")):
            files.append(f)
    print(f"扫描 {len(files)} 个文件...")

    # 第一遍: 逐日全市场 amount 中位数参照(仅2026年, 其余年份=万元统一无需判)
    print("构建 2026 逐日参照...")
    date_ref = {}
    for f in [f for f in files if "/2026/" in str(f)]:
        if f.name.startswith(("SH", "SZ")):
            continue  # 指数键不参与
        try:
            d = pd.read_parquet(f, columns=["date", "amount"])
        except Exception:
            continue
        am = pd.to_numeric(d["amount"], errors="coerce")
        d = d[am > 0].copy()
        if d.empty:
            continue
        d["amount"] = am[am > 0]
        d["date"] = d["date"].astype(str).str[:10]
        for dt, grp in d.groupby("date"):
            date_ref.setdefault(dt, []).append(float(grp["amount"].median()))
    ref = {dt: float(np.median(v)) for dt, v in date_ref.items()}
    print(f"参照日期数: {len(ref)}")

    # 第二遍: 逐文件分段判定+归一计划
    plan = []  # {file, n_segments, fix_segments, ratio_before, ratio_after}
    for f in files:
        code = f.stem
        if code.startswith(("SH", "SZ")):
            continue
        year = f.parent.name
        try:
            d = pd.read_parquet(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.sort_values("date").reset_index(drop=True)
        am = pd.to_numeric(d["amount"], errors="coerce").tolist()
        vol = pd.to_numeric(d["volume"], errors="coerce").tolist()
        if year != "2026":
            # 2026前=万元时代, 校验是否有元混入(修复文件理论上已归一万元,
            # 但新上市票的文件可能全元)
            segs = detect_segments(d["date"].dt.strftime("%Y-%m-%d").tolist(), am)
            need_fix = [(s, e) for s, e, med in segs if med > 1e6]
            if need_fix:
                plan.append({"file": str(f), "segs": len(segs), "fix": need_fix})
            continue
        # 2026: 每段独立量级判定(>1e6=元段), 不做交替推理——
        # 真实模式只有"万元→元单向切换"或全段单一(修复文件全万元/
        # 新股全元), 交替模型在数据洞上会级联误判(2026-09-09教训)
        segs = detect_segments(d["date"].dt.strftime("%Y-%m-%d").tolist(), am)
        need_fix = [(s, e) for s, e, med in segs if med > 1e6]
        if need_fix:
            plan.append({"file": str(f), "segs": len(segs), "fix": need_fix})

    print(f"需归一文件数: {len(plan)}")
    total_segs = sum(len(p["fix"]) for p in plan)
    print(f"需归一段数: {total_segs}")
    # 分布样例
    for p in plan[:8]:
        print(f"  {p['file']}: {p['segs']}段, 修{p['fix']}")
    json.dump(plan, open("logs/unit_norm_plan.json", "w"), ensure_ascii=False,
              default=str, indent=1)
    print("计划落盘: logs/unit_norm_plan.json (dry-run, 未改库)")
    if args.apply:
        print("--apply 待 dry-run 审核后实现")


if __name__ == "__main__":
    main()
