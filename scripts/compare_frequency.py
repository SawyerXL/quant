"""
调仓频率对比：双周(biweekly) vs 每周(weekly)

运行：
    python scripts/compare_frequency.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a2 import (
    _make_rebal_dates,
)
from run_backtest_a import (
    load_panels, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, CASH_YIELD,
    PERIOD_STOP, TRAILING_STOP, MA_PERIOD,
)

logger.add("logs/compare_frequency.log", rotation="1 day")

BACKTEST_END  = os.getenv("BACKTEST_END", "")
N_HOLDINGS    = int(os.getenv("N_HOLDINGS", "30"))
USE_REGIME    = os.getenv("USE_REGIME", "1") == "1"
MA10_EXIT_DAYS   = int(os.getenv("MA10_EXIT_DAYS", "3"))
MA_EXIT_WINDOW   = int(os.getenv("MA_EXIT_WINDOW", "10"))
NEW_STOCK_PROTECT = int(os.getenv("NEW_STOCK_PROTECT", "2"))

# 从A-4复用
from run_backtest_a4 import run_backtest_a4


def main():
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        end_candidates = [d for d in sorted(cal_df["trade_date"].tolist()) if d <= "2026-12-31"]
        _end = end_candidates[-1] if end_candidates else "2024-12-31"
    else:
        _end = BACKTEST_END

    calendar = [d for d in cal_df["trade_date"].tolist() if BACKTEST_START <= d <= _end]

    # 加载数据（只加载一次）
    csi800 = load_meta("csi800")
    codes = sorted(csi800["code"].tolist())
    panel, ap = load_panels(codes, BACKTEST_START, _end)

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        index_close = None
    else:
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        index_close = idx_df.set_index("date")["close"].sort_index()

    freqs = ["biweekly", "weekly"]
    results = {}

    for freq in freqs:
        rebal_dates = _make_rebal_dates(calendar, freq)
        n_rebal = len(rebal_dates)
        logger.info(f"\n{'='*60}")
        logger.info(f"调仓频率: {freq}  ({n_rebal} 个调仓日, 年均 {n_rebal/(int(_end[:4])-int(BACKTEST_START[:4])+1):.0f} 次)")
        logger.info(f"{'='*60}")

        nav = run_backtest_a4(panel, rebal_dates, ap, index_close, stock_info)
        results[freq] = {"nav": nav, "n_rebal": n_rebal}

        # 分年
        logger.info(f"── {freq} 分年度 ──")
        for yr in range(int(BACKTEST_START[:4]), int(_end[:4]) + 1):
            yn = nav[nav.index.year == yr]
            if len(yn) < 2:
                continue
            ret = yn.iloc[-1] / yn.iloc[0] - 1
            mdd = ((yn - yn.cummax()) / yn.cummax()).min()
            logger.info(f"  {yr}  收益:{ret:+.1%}  最大回撤:{mdd:.1%}")

    # ── 对比 ──
    logger.info(f"\n{'='*60}")
    logger.info("对比总览")
    logger.info(f"{'='*60}")
    logger.info(f"{'指标':<20} {'双周(biweekly)':<20} {'每周(weekly)':<20} {'变动':<15}")
    logger.info("-" * 75)

    for freq in freqs:
        nav = results[freq]["nav"]
        m = calc_metrics(nav)

        if freq == "biweekly":
            m_bi = m
            n_bi = results[freq]["n_rebal"]
        else:
            m_wk = m
            n_wk = results[freq]["n_rebal"]

    # 解析指标
    def parse_pct(s):
        return float(s.strip("%")) / 100

    metrics_to_compare = [
        ("年化收益率", "pct"),
        ("夏普比率", "float"),
        ("最大回撤", "pct"),
        ("年化波动率", "pct"),
        ("Calmar比率", "float"),
        ("胜率", "pct"),
    ]

    for key, typ in metrics_to_compare:
        v_bi = parse_pct(m_bi.get(key, "0%")) if typ == "pct" else float(m_bi.get(key, 0))
        v_wk = parse_pct(m_wk.get(key, "0%")) if typ == "pct" else float(m_wk.get(key, 0))
        delta = v_wk - v_bi
        if typ == "pct":
            logger.info(f"{key:<20} {v_bi:>+8.1%}            {v_wk:>+8.1%}            {delta:>+8.1%}")
        else:
            logger.info(f"{key:<20} {v_bi:>8.2f}            {v_wk:>8.2f}            {delta:>+8.2f}")

    logger.info(f"{'调仓次数':<20} {n_bi:>8d}            {n_wk:>8d}            {n_wk-n_bi:>+8d}")

    # ── 换手率估算 ──
    # 假设每次换手率相似，总换手成本 ≈ 次数 × 平均换手
    # 粗略估计：年均换手次数差异
    years = int(_end[:4]) - int(BACKTEST_START[:4]) + 1
    logger.info(f"{'年均调仓':<20} {n_bi/years:>8.1f}            {n_wk/years:>8.1f}            {n_wk/years-n_bi/years:>+8.1f}")

    # 结论
    ar_bi = parse_pct(m_bi["年化收益率"])
    ar_wk = parse_pct(m_wk["年化收益率"])
    sr_bi = float(m_bi["夏普比率"])
    sr_wk = float(m_wk["夏普比率"])
    md_bi = parse_pct(m_bi["最大回撤"])
    md_wk = parse_pct(m_wk["最大回撤"])

    logger.info(f"\n{'='*60}")
    if ar_wk > ar_bi + 0.02 and sr_wk > sr_bi:
        logger.info("✅ 每周调仓优于双周调仓（年化+夏普均有提升）")
    elif ar_wk > ar_bi and sr_wk < sr_bi:
        logger.info("⚠️ 每周调仓年化略高但夏普下降（收益增量被波动/成本抵消）")
    elif ar_wk < ar_bi and sr_wk < sr_bi:
        logger.info("❌ 每周调仓劣于双周调仓（年化+夏普双降）")
    else:
        logger.info("➡️ 差异不显著，维持双周即可")

    # 保存净值
    for freq in freqs:
        results[freq]["nav"].to_csv(f"logs/compare_freq_{freq}_nav.csv", header=["nav"])
    logger.info(f"\n净值已保存 → logs/compare_freq_*.csv")


if __name__ == "__main__":
    main()
