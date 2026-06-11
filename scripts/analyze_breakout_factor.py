"""
突破新高单因子分析。
在 CSI 800 股票池中，对不同回望期新高信号统计：
  - 1个月（20交易日）持有期的胜率
  - 平均收益、中位数收益、信息系数（IC）
  - 分年度表现

运行：python scripts/analyze_breakout_factor.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta
sys.path.insert(0, str(Path(__file__).parent))
from run_backtest_a import load_panels

logger.add("logs/analyze_breakout.log", rotation="1 day")

BACKTEST_START = "2019-01-01"
BACKTEST_END   = "2025-12-31"
HOLD_DAYS      = 20          # 持有期：约1个月
LOOKBACKS      = [20, 60, 126, 250]   # 测试四种"新高"定义


def run_analysis():
    logger.info("=" * 60)
    logger.info(f"突破新高单因子分析  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"持有期: {HOLD_DAYS} 交易日（约1个月）")
    logger.info("=" * 60)

    # ── 加载数据 ────────────────────────────────────────
    csi800 = load_meta("csi800")
    codes  = sorted(csi800["code"].tolist())
    logger.info("加载价格矩阵...")
    panel, _ = load_panels(codes, BACKTEST_START, BACKTEST_END)
    panel = panel.ffill()   # 填补停牌日空值
    logger.info(f"价格矩阵: {panel.shape[0]} 天 × {panel.shape[1]} 只")

    all_dates = panel.index
    results   = {}

    for lb in LOOKBACKS:
        logger.info(f"\n── 测试 {lb} 日新高 ──")
        signals   = []   # (date, code, signal_price, fwd_ret, year)

        for i in range(lb, len(all_dates) - HOLD_DAYS):
            date = all_dates[i]

            # 当日收盘价
            today_close  = panel.iloc[i]
            # 过去 lb 日最高价（不含今日）
            prev_high    = panel.iloc[i - lb : i].max()
            # 前向收益（20天后）
            fwd_close    = panel.iloc[i + HOLD_DAYS]
            fwd_ret      = (fwd_close / today_close - 1)

            # 突破新高：今日收盘 > 过去 lb 日最高价
            breakout_mask = (today_close > prev_high) & today_close.notna() & fwd_ret.notna()
            breakout_codes = breakout_mask[breakout_mask].index.tolist()

            for code in breakout_codes:
                signals.append({
                    "date":    date,
                    "code":    code,
                    "fwd_ret": float(fwd_ret[code]),
                    "year":    date.year,
                })

        if not signals:
            logger.warning(f"  {lb}日新高：无信号")
            continue

        df = pd.DataFrame(signals)
        n_total   = len(df)
        win_rate  = (df["fwd_ret"] > 0).mean()
        avg_ret   = df["fwd_ret"].mean()
        med_ret   = df["fwd_ret"].median()
        std_ret   = df["fwd_ret"].std()
        t_stat    = avg_ret / (std_ret / np.sqrt(n_total)) if std_ret > 0 else 0

        logger.info(f"  信号总数: {n_total:,}")
        logger.info(f"  胜率:     {win_rate:.1%}")
        logger.info(f"  平均收益: {avg_ret:.2%}  中位数: {med_ret:.2%}  标准差: {std_ret:.2%}")
        logger.info(f"  t统计量:  {t_stat:.2f}  (|t|>2 = 显著)")

        # 分年度
        logger.info(f"  分年度胜率:")
        for yr, grp in df.groupby("year"):
            yr_win = (grp["fwd_ret"] > 0).mean()
            yr_avg = grp["fwd_ret"].mean()
            logger.info(f"    {yr}  胜率:{yr_win:.1%}  均收益:{yr_avg:.2%}  ({len(grp):,}次)")

        results[lb] = {
            "n": n_total, "win_rate": win_rate,
            "avg_ret": avg_ret, "med_ret": med_ret, "t_stat": t_stat,
        }

    # ── 汇总对比 ────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("汇总对比（持有20日）")
    logger.info(f"{'回望期':>6}  {'信号数':>8}  {'胜率':>7}  {'均收益':>8}  {'中位数':>8}  {'t统计':>7}")
    logger.info("-" * 60)
    for lb, r in results.items():
        logger.info(
            f"{lb:>4}日  {r['n']:>8,}  {r['win_rate']:>7.1%}  "
            f"{r['avg_ret']:>8.2%}  {r['med_ret']:>8.2%}  {r['t_stat']:>7.2f}"
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    run_analysis()
