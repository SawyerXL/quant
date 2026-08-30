"""
池子规模统一回测 (2026-08-30, 文档待办1: 实盘TOP30 vs 回测TOP60不一致)。

同时复验: 拥挤度过滤(v2.1)在TOP30池子上是否同样有效 ——
过滤的原始A/B全部基于TOP60, 实盘口径的过滤必须有独立证据。

变体 × 窗口: 全期2019-2026.8 + 近段2022-2026.8
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"
WINDOWS = [("全期2019-2026.8", "2019-01-01", "2026-08-28"),
           ("近段2022-2026.8", "2022-01-01", "2026-08-28")]


def cfg(pool, vol):
    return BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_size": pool,
                             "max_vol20": vol})


def main():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天")

    idx = load_meta("csi800_index")
    idx_c = None
    if not idx.empty:
        idx_c = idx.set_index("date")["close"].sort_index()
        idx_c.index = pd.to_datetime(idx_c.index)

    cal = sorted(load_meta("trade_calendar")["trade_date"].astype(str).tolist())

    variants = [
        ("TOP30 无过滤(旧实盘)", cfg(30, 999)),
        ("TOP30 +过滤(候选实盘)", cfg(30, 5.0)),
        ("TOP40 +过滤", cfg(40, 5.0)),
        ("TOP60 +过滤(现回测默认)", cfg(60, 5.0)),
        ("TOP80 +过滤", cfg(80, 5.0)),
    ]

    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n{'='*70}\n窗口 {wname}\n{'='*70}")
        print(f"{'配置':<24}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
        for name, c in variants:
            nav, _ = run_backtest(p, a, rebal, c, idx_c)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<24}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%")


if __name__ == "__main__":
    main()
