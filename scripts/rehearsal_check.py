"""预演/考试核验主检(10-12 预演与 10-19 考试日共用, 实现 runbook 已锁定条款)。

六段机器输出, 每段 PASS/FAIL:
  1. 重建证明: 快照 mtime=当日(06:30 后) + cron_cb_data.log 当日完成行
     "✓ 快照: N个月, 共M条记录" + 当日段无 ERROR——mtime/完成行缺一, 增量 0 视同失败
  2. 行数判据: 净增量 ∈ [--lo, --hi](默认 -100, 600), 机械执行禁止现场判断
  3. 行级毛/净分报: 新增/删除/净, 逐 snap_date 分布
  4. 字段级 diff: 共存 (code,snap_date) 行的 rating/价格改写数(评级改写不改行数,
     行级 diff 看不见, 周重建把当前评级覆盖写进全部历史=已知漂移源)
  5. 两侧 PIT: 新增侧/删除侧各自核对 锚定交易日 bar 有无 + 成员日期上限
     (list_date≤snap / delist≥snap)
  6. 漂移日志第 N 行素材(monthly_zero 由 backtest_cb_sched_compare.py 复跑后填)

用法:
  python scripts/rehearsal_check.py --date 2026-10-12 --baseline logs/cb_snapshots_baseline_2026-10-12.parquet
  10-19 行数判据只记基线行数时: --baseline-rows 35772 (跳过 3/4/5 段)
  10-12 预演结束时另存 10-19 基线: --save-baseline-as 2026-10-19
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_meta

SNAP = Path("/data/quant_data_store/meta/cb_snapshots.parquet")
CRON_LOG = Path("/root/quant/logs/cron_cb_data.log")
DAILY_DIR = Path("/data/quant_data_store/convertible_bonds/daily")

results = []


def report(section, ok, detail):
    results.append((section, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {section}: {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD 当日")
    ap.add_argument("--baseline", help="基线 parquet 路径(3/4/5 段的 diff 对端)")
    ap.add_argument("--baseline-rows", type=int, help="仅行数基线(10-19 用, 跳过 3/4/5)")
    ap.add_argument("--lo", type=int, default=-100)
    ap.add_argument("--hi", type=int, default=600)
    ap.add_argument("--save-baseline-as", help="检查通过后另存当日快照为指定日期的基线")
    args = ap.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d")
    live = pd.read_parquet(SNAP)
    live["sd"] = pd.to_datetime(live["snap_date"]).dt.strftime("%Y-%m-%d")
    live_n = len(live)

    # ── 1. 重建证明 ──────────────────────────────────────────────
    mtime_ok = datetime.fromtimestamp(SNAP.stat().st_mtime) >= day.replace(hour=6, minute=30)
    log_txt = CRON_LOG.read_text(errors="ignore") if CRON_LOG.exists() else ""
    day_lines = [l for l in log_txt.splitlines() if l.startswith(args.date)]
    done_line = [l for l in day_lines if "✓ 快照:" in l and "共" in l and "条记录" in l]
    err_lines = [l for l in day_lines if "ERROR" in l]
    proof_ok = mtime_ok and bool(done_line) and not err_lines
    report("1 重建证明",
           proof_ok,
           f"mtime当日06:30后={mtime_ok} 完成行={len(done_line)}条"
           f"({'…' + done_line[-1].split('|')[-1].strip() if done_line else '无'})"
           f" 当日ERROR={len(err_lines)}条")
    if not proof_ok:
        report("1a 缺失明细", False,
               f"mtime={datetime.fromtimestamp(SNAP.stat().st_mtime)}"
               + (f" | 当日ERROR: {err_lines[0][:120]}" if err_lines else ""))

    # ── 2. 行数判据 ──────────────────────────────────────────────
    if args.baseline:
        base_df = pd.read_parquet(args.baseline)
        base_n = len(base_df)
        base_label = args.baseline
    elif args.baseline_rows is not None:
        base_n = args.baseline_rows
        base_label = f"记账行数 {base_n}"
    else:
        base_n, base_label = None, None
    if base_n is not None:
        net = live_n - base_n
        ok = args.lo <= net <= args.hi
        report("2 行数判据", ok,
               f"净增量={net:+d} ∈ [{args.lo}, {args.hi}] (基线 {base_label}, 现 {live_n})")

    # ── 3/4/5 需要 parquet 基线 ──────────────────────────────────
    if args.baseline:
        base_df["sd"] = pd.to_datetime(base_df["snap_date"]).dt.strftime("%Y-%m-%d")
        lp = set(zip(live["code"], live["sd"]))
        bp = set(zip(base_df["code"], base_df["sd"]))
        added = sorted(lp - bp)
        removed = sorted(bp - lp)
        from collections import Counter
        add_by = Counter(d for _, d in added)
        rem_by = Counter(d for _, d in removed)
        report("3 行级毛/净分报", True,
               f"新增 {len(added)} / 删除 {len(removed)} / 净 {len(added)-len(removed):+d}")
        print(f"   新增逐月: {dict(sorted(add_by.items())[-6:])}")
        print(f"   删除逐月: {dict(sorted(rem_by.items())[-6:])}")

        # ── 4. 字段级 diff ───────────────────────────────────────
        m = base_df.merge(live, on=["code", "sd"], suffixes=("_b", "_l"))
        rchg = m[m["rating_b"] != m["rating_l"]]
        pchg = m[(m["price_b"] - m["price_l"]).abs() > 0.01]
        report("4 字段级 diff", True,
               f"共存 {len(m)} 行: rating 改写 {len(rchg)} / 价格改写 {len(pchg)}")
        if len(rchg):
            samp = list(rchg[["code", "sd", "rating_b", "rating_l"]]
                        .itertuples(index=False, name=None))[:6]
            print("   rating 改写样本: " + ", ".join(
                f"{c}@{d} {rb}→{rl}" for c, d, rb, rl in samp))

        # ── 5. 两侧 PIT ───────────────────────────────────────────
        cal = load_meta("trade_calendar")["trade_date"].astype(str).tolist()
        uni = load_meta("cb_universe")
        uni["code"] = uni["code"].astype(str)
        list_map = dict(zip(uni["code"], uni["list_date"].astype(str).str[:10]))
        red = load_meta("cb_redemptions")
        if not red.empty:
            red["code"] = red["code"].astype(str)
            delist_map = dict(zip(red["code"], red["redeem_date"].astype(str).str[:10]))
        else:
            delist_map = {}

        def anchor(snap):
            v = [d for d in cal if d <= snap]
            return v[-1] if v else None

        def pit_side(pairs, side):
            bar_miss = bar_hit = date_viol = 0
            viol = []
            for code, snap in pairs:
                td = anchor(snap)
                has_bar = False
                fpath = DAILY_DIR / f"{code}.parquet"
                if fpath.exists():
                    f = pd.read_parquet(fpath)
                    f["date"] = pd.to_datetime(f["date"]).dt.strftime("%Y-%m-%d")
                    has_bar = td in f["date"].tolist()
                if has_bar:
                    bar_hit += 1
                else:
                    bar_miss += 1
                ld = list_map.get(code, "")
                dd = delist_map.get(code, "2099-12-31")
                if (ld and ld > snap) or (dd and dd < snap):
                    date_viol += 1
                    viol.append((code, snap, f"list={ld or '?'} delist={dd}"))
            expect = "锚定日应有 bar" if side == "新增侧" else "锚定日应无 bar(价格缺失机制)"
            weird = date_viol + (bar_miss if side == "新增侧" else bar_hit)
            ok = weird == 0
            report(f"5 {side} PIT ({len(pairs)} 对)", ok,
                   f"锚定日有bar {bar_hit} / 无bar {bar_miss} / 日期上限违规 {date_viol}"
                   f" (期望: {expect})")
            for v in viol[:10]:
                print(f"   日期违规: {v}")
            return ok

        if added:
            pit_side(added, "新增侧")
        else:
            print("[SKIP] 5 新增侧: 无新增对")
        if removed:
            pit_side(removed, "删除侧")
        else:
            print("[SKIP] 5 删除侧: 无删除对")

    # ── 6. 漂移日志素材 ──────────────────────────────────────────
    print("6 漂移日志第 N 行素材(monthly_zero 由 backtest_cb_sched_compare.py 复跑后填):")
    if args.baseline:
        print(f"   | 5 | {args.date} 周更快照重建 | monthly_zero=__ | Δ=__ | "
              f"行级 +{len(added)}/−{len(removed)}/净{len(added)-len(removed):+d}; "
              f"字段级 rating 改写 {len(rchg)}、价格改写 {len(pchg)} |")

    if args.save_baseline_as:
        import hashlib, shutil
        live.to_parquet(Path(f"/root/quant/logs/cb_snapshots_baseline_{args.save_baseline_as}.parquet"))
        h = hashlib.sha256(SNAP.read_bytes()).hexdigest()
        print(f"[基线另存] 当日快照 → logs/cb_snapshots_baseline_{args.save_baseline_as}.parquet sha256={h}")

    failed = [s for s, ok in results if not ok]
    print(f"\n总结: {len(results)-len(failed)}/{len(results)} 段 PASS"
          + (f", FAIL 段: {failed}" if failed else " ✓"))


if __name__ == "__main__":
    main()
