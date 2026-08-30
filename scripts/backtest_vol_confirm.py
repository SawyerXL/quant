"""
波动率过滤稳健性复验 (2026-08-30, 用户要求"反复确认没问题再改动")。

确认项:
  1. 全期2019-2026.8完整基线(上轮输出被截断的部分)
  2. 阈值网格 4/4.5/5/5.5/6% —— 5%不该是孤立尖点
  3. 严格口径(vol20不含调仓日当天收益) vs 含当天 —— 结论不依赖look-ahead
窗口: 2019-2022 / 全期2019-2026.8 / 近段2022-2026.8 / 近段2024.7-2026.8
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
WINDOWS = [("2019-2022", "2019-01-01", "2021-12-31"),
           ("全期2019-2026.8", "2019-01-01", "2026-08-28"),
           ("近段2022-2026.8", "2022-01-01", "2026-08-28"),
           ("近段2024.7-2026.8", "2024-07-01", "2026-08-28")]


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

    variants = [("基线(无过滤)", DEFAULT_CONFIG)]
    for th in (4.0, 4.5, 5.0, 5.5, 6.0):
        variants.append((f"vol过滤{th:g}%严格", BacktestConfig(
            **{**DEFAULT_CONFIG.to_dict(), "max_vol20": th, "vol20_use_today": False})))
    variants.append(("vol过滤5%含当天", BacktestConfig(
        **{**DEFAULT_CONFIG.to_dict(), "max_vol20": 5.0, "vol20_use_today": True})))

    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        print(f"\n{'='*70}\n窗口 {wname}\n{'='*70}")
        print(f"{'配置':<18}{'年化':>9}{'夏普':>8}{'最大回撤':>10}")
        for name, cfg in variants:
            nav, _ = run_backtest(p, a, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            ar = float(str(cm["年化收益率"]).strip("%"))
            sr = float(cm["夏普比率"])
            dd = float(str(cm["最大回撤"]).strip("%"))
            print(f"{name:<18}{ar:>+8.2f}%{sr:>8.2f}{dd:>9.2f}%")


if __name__ == "__main__":
    main()
