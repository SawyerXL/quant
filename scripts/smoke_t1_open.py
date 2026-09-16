"""T+1 开盘执行冒烟（2026-09-16）：A3 完整栈, exec_t1_open 开/关对照。
2 路径×2 模式, 主窗。若 diff 落 §9 预期带(Δ<2.5pp)则机制可信, 再跑全量。"""
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
from backtest_return_attribution import WINDOWS, BASE, load_blacklist

START, END = WINDOWS["main"]
PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def build_panels():
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices, opens, amounts = {}, {}, {}
    for code in codes:
        try:
            d = load_daily(code, load_start, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            if "price_break_flag" in d.columns:
                d.loc[d["price_break_flag"] == 1, "close"] = float("nan")
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            op = pd.to_numeric(d.get("open"), errors="coerce")
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
                opens[code] = op.reindex(cl.index)
                if len(amt) >= 200:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    open_panel = pd.DataFrame(opens).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    sh = load_daily("SH000001", load_start, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    open_panel = open_panel[open_panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, open_panel, ap, cal


def main():
    panel, open_panel, ap, cal = build_panels()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    base_dates = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    paths = []
    for off in range(PATHS):
        shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                   if idx.get(d, 0) + off < len(cal)]
        paths.append([d for d in shifted if START <= d <= END])

    for mode, cfg_ov in [("close执行", {}),
                         ("t1_open执行", {"exec_t1_open": True})]:
        anns = []
        for dates in paths:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    **cfg_ov, "commission": 0.0013})
            nav, info = run_backtest(panel, ap, dates, cfg, ic,
                                     open_panel=open_panel)
            nav = nav[nav.index >= pd.Timestamp(START)]
            anns.append(float(calc_metrics(nav)["年化_float"]))
        print(f"{mode}: 路径均值 {np.mean(anns)*100:+.2f}% (各路径: "
              f"{[f'{a*100:+.2f}' for a in anns]})", flush=True)


if __name__ == "__main__":
    main()
