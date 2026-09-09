"""
调仓频率严格验证：双周 vs 每周（含 look-ahead bug 修复对比）

验证维度：
  1. 原始版本(有先知交易bug) biweekly vs weekly
  2. 修复版本(无先知交易bug) biweekly vs weekly
  3. 三频率梯度: monthly vs biweekly vs weekly (修复版)
  4. OOS交叉验证: train 2019-2022 / test 2023-2026

运行：
    python scripts/compare_frequency_v2.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a2 import _make_rebal_dates
from run_backtest_a import load_panels, calc_metrics, BACKTEST_START, COMMISSION
from run_backtest_a4 import run_backtest_a4, N_HOLDINGS
from run_backtest_a4_fixed import run_backtest_a4_fixed

logger.add("logs/compare_frequency_v2.log", rotation="1 day")


def parse_pct(s):
    return float(str(s).strip("%")) / 100


def fmt_pct(v):
    return f"{v:+.1%}"


def run_one(panel, ap, calendar, freq, index_close, stock_info, fixed=False):
    """运行一次回测，返回 nav + metrics"""
    rebal_dates = _make_rebal_dates(calendar, freq)
    fn = run_backtest_a4_fixed if fixed else run_backtest_a4
    nav = fn(panel, rebal_dates, ap, index_close, stock_info)
    m = calc_metrics(nav)
    return {
        "nav": nav,
        "n_rebal": len(rebal_dates),
        "年化": parse_pct(m["年化收益率"]),
        "夏普": float(m["夏普比率"]),
        "回撤": parse_pct(m["最大回撤"]),
        "波动": parse_pct(m["年化波动率"]),
    }


def main():
    cal_df = load_meta("trade_calendar")
    end_candidates = [d for d in sorted(cal_df["trade_date"].tolist()) if d <= "2026-12-31"]
    end = os.getenv("BACKTEST_END", end_candidates[-1] if end_candidates else "2024-12-31")

    calendar = [d for d in cal_df["trade_date"].tolist() if BACKTEST_START <= d <= end]
    csi800 = load_meta("csi800")
    codes = sorted(csi800["code"].tolist())
    panel, ap = load_panels(codes, BACKTEST_START, end)

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        index_close = None
    else:
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        index_close = idx_df.set_index("date")["close"].sort_index()

    years = int(end[:4]) - int(BACKTEST_START[:4]) + 1

    # ============================================================
    # TEST 1: 原始版本 biweekly vs weekly（复现之前的对比）
    # ============================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 1: 原始版本(有先知交易bug) — biweekly vs weekly")
    logger.info("=" * 80)

    t1 = {}
    for freq in ["biweekly", "weekly"]:
        t1[freq] = run_one(panel, ap, calendar, freq, index_close, stock_info, fixed=False)
        logger.info(f"  {freq:10s}: 年化={fmt_pct(t1[freq]['年化'])}  夏普={t1[freq]['夏普']:.2f}  "
                     f"回撤={fmt_pct(t1[freq]['回撤'])}  调仓={t1[freq]['n_rebal']}次")

    diff_ar_bug = t1["weekly"]["年化"] - t1["biweekly"]["年化"]
    diff_sr_bug = t1["weekly"]["夏普"] - t1["biweekly"]["夏普"]
    logger.info(f"  → 差异: 年化{fmt_pct(diff_ar_bug)}  夏普{diff_sr_bug:+.2f}")

    # ============================================================
    # TEST 2: 修复版本 biweekly vs weekly（消除先知交易后）
    # ============================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 2: 修复版本(消除先知交易) — biweekly vs weekly")
    logger.info("=" * 80)

    t2 = {}
    for freq in ["biweekly", "weekly"]:
        t2[freq] = run_one(panel, ap, calendar, freq, index_close, stock_info, fixed=True)
        logger.info(f"  {freq:10s}: 年化={fmt_pct(t2[freq]['年化'])}  夏普={t2[freq]['夏普']:.2f}  "
                     f"回撤={fmt_pct(t2[freq]['回撤'])}  调仓={t2[freq]['n_rebal']}次")

    diff_ar_fix = t2["weekly"]["年化"] - t2["biweekly"]["年化"]
    diff_sr_fix = t2["weekly"]["夏普"] - t2["biweekly"]["夏普"]
    logger.info(f"  → 差异: 年化{fmt_pct(diff_ar_fix)}  夏普{diff_sr_fix:+.2f}")

    # ============================================================
    # TEST 3: 修复版三频率梯度 (monthly → biweekly → weekly)
    # ============================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 3: 修复版三频率梯度 — monthly vs biweekly vs weekly")
    logger.info("=" * 80)

    t3 = {}
    for freq in ["monthly", "biweekly", "weekly"]:
        t3[freq] = run_one(panel, ap, calendar, freq, index_close, stock_info, fixed=True)
        logger.info(f"  {freq:10s}: 年化={fmt_pct(t3[freq]['年化'])}  夏普={t3[freq]['夏普']:.2f}  "
                     f"回撤={fmt_pct(t3[freq]['回撤'])}  波动={fmt_pct(t3[freq]['波动'])}  "
                     f"调仓={t3[freq]['n_rebal']}次 (年均{t3[freq]['n_rebal']/years:.0f})")

    logger.info(f"\n  梯度: monthly → biweekly: 年化{fmt_pct(t3['biweekly']['年化']-t3['monthly']['年化'])}  夏普{t3['biweekly']['夏普']-t3['monthly']['夏普']:+.2f}")
    logger.info(f"         biweekly → weekly:   年化{fmt_pct(t3['weekly']['年化']-t3['biweekly']['年化'])}  夏普{t3['weekly']['夏普']-t3['biweekly']['夏普']:+.2f}")

    # ============================================================
    # TEST 4: OOS 交叉验证 (修复版)
    # ============================================================
    logger.info("\n" + "=" * 80)
    logger.info("TEST 4: OOS交叉验证 — train 2019-2022 / test 2023-2026 (修复版)")
    logger.info("=" * 80)

    train_start, train_end = "2019-01-01", "2022-12-31"
    test_start, test_end = "2023-01-01", end

    cal_train = [d for d in calendar if train_start <= d <= train_end]
    cal_test  = [d for d in calendar if test_start  <= d <= test_end]

    idx_tr = index_close[train_start:train_end] if index_close is not None else None
    idx_te = index_close[test_start:test_end] if index_close is not None else None

    oos = {}
    for freq in ["biweekly", "weekly"]:
        # Train period
        tr = run_one(panel, ap, cal_train, freq, idx_tr, stock_info, fixed=True)
        # Test period
        te = run_one(panel, ap, cal_test, freq, idx_te, stock_info, fixed=True)
        oos[freq] = {"train": tr, "test": te}
        logger.info(f"  {freq:10s}: Train 年化={fmt_pct(tr['年化'])}  夏普={tr['夏普']:.2f}  |  "
                     f"Test 年化={fmt_pct(te['年化'])}  夏普={te['夏普']:.2f}  回撤={fmt_pct(te['回撤'])}")

    diff_test_ar = oos["weekly"]["test"]["年化"] - oos["biweekly"]["test"]["年化"]
    diff_test_sr = oos["weekly"]["test"]["夏普"] - oos["biweekly"]["test"]["夏普"]
    logger.info(f"  → OOS Test差异: 年化{fmt_pct(diff_test_ar)}  夏普{diff_test_sr:+.2f}")

    # ============================================================
    # SUMMARY
    # ============================================================
    logger.info("\n" + "=" * 80)
    logger.info("综合结论")
    logger.info("=" * 80)

    # Bug impact quantification
    bug_impact_bi = t1["biweekly"]["年化"] - t2["biweekly"]["年化"]
    bug_impact_wk = t1["weekly"]["年化"] - t2["weekly"]["年化"]
    logger.info(f"  Look-ahead bug影响:")
    logger.info(f"    biweekly: 虚增 {fmt_pct(bug_impact_bi)} 年化")
    logger.info(f"    weekly:   虚增 {fmt_pct(bug_impact_wk)} 年化")
    logger.info(f"    频率放大: {fmt_pct(bug_impact_wk - bug_impact_bi)} (weekly虚增收敛更多)")

    logger.info(f"\n  修复前 weekly - biweekly: {fmt_pct(diff_ar_bug)}")
    logger.info(f"  修复后 weekly - biweekly: {fmt_pct(diff_ar_fix)}")
    logger.info(f"  OOS Test weekly - biweekly: {fmt_pct(diff_test_ar)}")

    # Final verdict
    real_diff = diff_ar_fix  # 修复后 full sample
    oos_diff = diff_test_ar  # OOS test period

    if real_diff > 0.05 and oos_diff > 0.03:
        verdict = "✅ 强烈建议切换到每周调仓"
    elif real_diff > 0.02 and oos_diff > 0.01:
        verdict = "✅ 建议切换到每周调仓（改善显著且OOS确认）"
    elif real_diff > 0.01 and oos_diff > 0:
        verdict = "⚠️ 每周略优但改善幅度小，维持双周也可"
    else:
        verdict = "❌ 每周不优于双周（bug修复后优势消失），维持双周"

    logger.info(f"\n  最终判断: {verdict}")

    # Save all navs for later analysis
    for label, nav in [
        ("t1_biweekly_bug", t1["biweekly"]["nav"]),
        ("t1_weekly_bug", t1["weekly"]["nav"]),
        ("t2_biweekly_fix", t2["biweekly"]["nav"]),
        ("t2_weekly_fix", t2["weekly"]["nav"]),
        ("t3_monthly_fix", t3["monthly"]["nav"]),
        ("t2_weekly_fix_test", oos["weekly"]["test"]["nav"]),
        ("t2_biweekly_fix_test", oos["biweekly"]["test"]["nav"]),
    ]:
        nav.to_csv(f"logs/compare_v2_{label}_nav.csv", header=["nav"])

    logger.info("\n净值已保存 → logs/compare_v2_*_nav.csv")


if __name__ == "__main__":
    main()
