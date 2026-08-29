"""
QMT主策略 A/B 增强测试（2026-08-29，个人账户8月经验驱动的候选改动）。

个人8月归因: 已实现收益+3.56万(止盈/减仓动作)贡献全部alpha,
持有不设防(万泰-3.4万)贡献全部亏损。→ 给QMT的候选经验:
  A. 更积极的止盈节奏(TP 20/40 vs 现网30/60)
  B. 调仓频率(weekly/monthly vs 现网biweekly)
  C. 对照: TP关闭
注: 过热过滤/入场过滤/追踪止损已在2026-07-08消融测试证明有害, 不再测。

用法: python scripts/backtest_qmt_ab_202608.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2022-01-01", "2026-08-28"
MIN_BARS = 250


def load_panel():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            df = load_daily(code, START, END)
            if df.empty:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            cl = pd.to_numeric(df["close"], errors="coerce").dropna()
            amt = pd.to_numeric(df.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= MIN_BARS:
                prices[code] = cl
                if len(amt) >= MIN_BARS:
                    amounts[code] = amt
        except Exception:
            pass
    print(f"Panel: {len(prices)}只")
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(amounts).sort_index()


def main():
    panel, ap = load_panel()
    idx = load_meta("csi800_index")
    idx_c = None
    if not idx.empty:
        idx_c = idx.set_index("date")["close"].sort_index()
        idx_c.index = pd.to_datetime(idx_c.index)

    variants = [
        ("基线(现网: 双周/TP30/60)", DEFAULT_CONFIG),
        ("周频调仓", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "rebalance_freq": "weekly"})),
        ("月频调仓", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "rebalance_freq": "monthly"})),
        ("积极止盈TP20/40", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "take_profit_1": 0.20, "take_profit_2": 0.40})),
        ("止盈关闭(对照)", BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "enable_take_profit": False})),
    ]

    print(f"\n{'配置':<24}{'年化':>9}{'夏普':>8}{'最大回撤':>10}{'交易数':>8}")
    results = {}
    for name, cfg in variants:
        rebal = make_rebal_dates(sorted(load_meta("trade_calendar")["trade_date"].astype(str).tolist()), cfg.rebalance_freq)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        cm = calc_metrics(nav)
        ar = float(str(cm["年化收益率"]).strip("%")) / 100
        sr = float(cm["夏普比率"])
        dd = float(str(cm["最大回撤"]).strip("%")) / 100
        nt = m.get("trades", 0)
        print(f"{name:<24}{ar*100:>+8.2f}%{sr:>8.2f}{dd*100:>9.2f}%{int(nt):>8}")
        results[name] = (nav, ar, sr, dd)

    print("\n对照: 近4.5年(2022-2026-08)样本, 含2022熊市+2024/2025牛熊+2026 BEAR")
    print("注: 现网QMT实盘是TOP30, 此处回测池子按DEFAULT的TOP60")


if __name__ == "__main__":
    main()
