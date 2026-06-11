"""
Track B「三位一体」策略回测。

用法：
  python scripts/backtest_trinity.py --layer regime              # 仅回测Regime择时
  python scripts/backtest_trinity.py --layer full --start 2019-01-01 --end 2025-12-31
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from loguru import logger
from data.storage import load_meta, load_daily
from config.strategy_params.trinity import REGIME, PORTFOLIO
from strategies.trinity.regime import RegimeGate
from strategies.trinity.portfolio import TrinityPortfolio

COMMISSION = PORTFOLIO["commission"]
INIT_CAPITAL = PORTFOLIO["capital"]
RF_ANNUAL = 0.025


def run_regime_backtest(start: str, end: str):
    """回测 Regime Gate 择时效果。"""
    import akshare as ak
    bench_sym = REGIME["benchmark_index"]
    df = ak.stock_zh_index_daily(symbol=bench_sym)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    close = df["close"][(df.index >= start) & (df.index <= end)]

    gate = RegimeGate()
    nav_bh = [1.0]
    nav_regime = [1.0]
    positions = []

    for i, (dt, p) in enumerate(close.items()):
        if i == 0:
            positions.append(1.0)
            continue
        dt_str = dt.strftime("%Y-%m-%d")
        regime = gate.evaluate(dt_str)
        pos = regime["position_cap"]
        positions.append(pos)
        ret = float(p / close.iloc[i - 1] - 1)
        bh = nav_bh[-1] * (1 + ret)
        rg = nav_regime[-1] * (1 + ret * pos)
        nav_bh.append(bh)
        nav_regime.append(rg)

    nav_bh_s = pd.Series(nav_bh, index=close.index)
    nav_rg_s = pd.Series(nav_regime, index=close.index)

    def metrics(nav_s):
        daily = nav_s.pct_change().dropna()
        total = nav_s.iloc[-1] - 1
        yrs = max(len(daily) / 252, 0.5)
        ann = (1 + total) ** (1 / yrs) - 1
        vol = daily.std() * np.sqrt(252)
        rf_d = RF_ANNUAL / 252
        sr = (daily.mean() - rf_d) / daily.std() * np.sqrt(252) if daily.std() > 0 else 0
        mdd = (nav_s / nav_s.cummax() - 1).min()
        return total, ann, vol, sr, mdd

    t_bh, a_bh, v_bh, s_bh, d_bh = metrics(nav_bh_s)
    t_rg, a_rg, v_rg, s_rg, d_rg = metrics(nav_rg_s)

    print(f"\n{'='*60}")
    print(f"  Regime Gate 择时回测  {bench_sym}  {start}→{end}")
    print(f"{'='*60}")
    print(f"  {'指标':<16} {'买入持有':>12} {'Regime择时':>12}")
    print(f"  {'总收益':<16} {t_bh:>+11.1%} {t_rg:>+11.1%}")
    print(f"  {'年化收益':<16} {a_bh:>+11.1%} {a_rg:>+11.1%}")
    print(f"  {'年化波动':<16} {v_bh:>11.1%} {v_rg:>11.1%}")
    print(f"  {'夏普比率':<16} {s_bh:>11.2f} {s_rg:>11.2f}")
    print(f"  {'最大回撤':<16} {d_bh:>11.1%} {d_rg:>11.1%}")
    print(f"  {'防御天数占比':<16} {'—':>12} {sum(1 for p in positions if p < 0.6)/len(positions):>11.1%}")

    # 回撤段规避统计
    bear_phases = 0
    for p in positions:
        if p < 0.5:
            bear_phases += 1
    print(f"  半仓以下天数: {bear_phases} ({bear_phases/len(positions):.1%})")
    print(f"{'='*60}\n")


def run_full_backtest(start: str, end: str):
    """完整三层策略回测。"""
    info = load_meta("stock_info_full")
    if info.empty:
        logger.error("stock_info_full 为空")
        return

    csi = load_meta("csi800")
    codes = sorted(csi["code"].tolist())[:800]
    from run_backtest_a import load_panels

    panel, ap = load_panels(codes, start, end)
    if panel.empty:
        logger.error("价格矩阵为空")
        return

    from strategies.trinity.portfolio import TrinityPortfolio

    pf = TrinityPortfolio()
    # 生成调仓日（每月两次：月中+月末）
    rebal_dates = []
    for yr in range(int(start[:4]), int(end[:4]) + 1):
        for mo in range(1, 13):
            md = panel[panel.index.year == yr]
            mm = md[md.index.month == mo]
            if len(mm) < 5:  continue
            mid = mm.index[len(mm) // 2]
            rebal_dates.append(mid)
            rebal_dates.append(mm.index[-1])
    rebal_dates = sorted(set(d.strftime("%Y-%m-%d") for d in rebal_dates))

    cur_holdings = []
    nav_total = INIT_CAPITAL
    nav_series = [1.0]
    annual_rets = {}
    daily_nav = pd.Series(1.0, index=panel.index)

    for dt_str in rebal_dates:
        if dt_str < start or dt_str > end:
            continue
        try:
            sig = pf.select(panel, ap, info, dt_str, cur_holdings)
        except Exception as e:
            logger.warning(f"{dt_str} 选股失败: {e}")
            continue
        cur_holdings = sig["holdings"]

    # 简化：只报告选股数量（完整PnL追踪需更多代码）
    print(f"\n  Track B 回测  {start}→{end}")
    print(f"  调仓日: {len(rebal_dates)}")
    print(f"  注: 完整收益追踪见 daily_signal_b.py 纸面交易\n")


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", default="regime", choices=["regime", "full"])
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2025-12-31")
    args = parser.parse_args()

    if args.layer == "regime":
        run_regime_backtest(args.start, args.end)
    else:
        run_full_backtest(args.start, args.end)
