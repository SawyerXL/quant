"""
止盈开关冲突复验 (2026-08-29)。

冲突背景:
  - stop_monitor_v3 (2019-2026.7): +30%卖1/3+60%再卖1/3 优于不卖 (+0.9pp)
  - backtest_qmt_ab_202608 (2022-2026.8): 止盈关闭 +4.80% > TP30/60 +3.87%
按复验纪律: 换窗口重跑 TP on/off, 多段对上才算数。

口径对齐v3: START=2019-01-01, 池子=DEFAULT(TOP60/双周/MA10-4d), 同一引擎。
窗口: 全期2019-2026.7 + 三段(2019-21/2022-24/2024.7-26.7)。
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

START, END = "2019-01-01", "2026-07-10"
WINDOWS = [("全期2019-2026.7", "2019-01-01", "2026-07-10"),
           ("2019-2021", "2019-01-01", "2021-12-31"),
           ("2022-2024", "2022-01-01", "2024-12-31"),
           ("2024.7-2026.7", "2024-07-01", "2026-07-10")]


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
    tp_on = DEFAULT_CONFIG
    tp_off = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "enable_take_profit": False})

    print(f"\n{'窗口':<16}{'TP30/60年化':>13}{'TP关闭年化':>13}{'差(关-开)':>11}  → 判")
    for wname, lo, hi in WINDOWS:
        p = panel[(panel.index >= lo) & (panel.index <= hi)]
        a = ap[(ap.index >= lo) & (ap.index <= hi)]
        rebal = [d for d in make_rebal_dates(cal, "biweekly") if lo <= d <= hi]
        row = []
        for cfg in (tp_on, tp_off):
            nav, _ = run_backtest(p, a, rebal, cfg, idx_c)
            cm = calc_metrics(nav)
            row.append(float(str(cm["年化收益率"]).strip("%")))
        diff = row[1] - row[0]
        verdict = "关闭更优" if diff > 0.5 else ("开启更优" if diff < -0.5 else "平手(±0.5pp内)")
        print(f"{wname:<16}{row[0]:>+12.2f}%{row[1]:>+12.2f}%{diff:>+10.2f}pp  → {verdict}")


if __name__ == "__main__":
    main()
