# -*- coding: utf-8 -*-
"""pTrade 100万回测同口径复验 (2026-09-08)
Linux 约束引擎, 对齐 pTrade sim 口径: N=60 / MA10=4d / vol20≤5% /
000906五档(无thresh_shift) / 熔断B 15%(带恢复腿) / lot100 / 100万单组 / 2019-01-01~2026-08-28
对照: 同配置 halt=none (引擎熔断恢复腿 vs pTrade 锁存的差异隔离)
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

LO, HI = "2019-01-01", "2026-08-28"
BASE = {"pool_size": 60, "lot_size": 100, "initial_capital": 1_000_000.0,
        "ma200_thresh_shift": 0.0, "halt_dd_limit": 0.15}


def load_window():
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, LO, HI)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx_c = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    sh = load_daily("SH000001", "2014-06-01", "2026-08-28")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if LO <= d <= HI]
    return panel, ap, idx_c, rebal


def main():
    panel, ap, idx_c, rebal = load_window()
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只", flush=True)
    variants = [
        ("B", 0.0, "对齐sim: shift=0, 熔断B 15%+恢复腿"),
        ("none", 0.0, "对照: shift=0, 无熔断"),
        ("B", -0.03, "现网: shift=-0.03(已采纳), 熔断B 15%+恢复腿"),
    ]
    for mode, shift, label in variants:
        cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                "halt_mode": mode,
                                "ma200_thresh_shift": shift})
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        if not isinstance(nav, pd.Series):
            nav = nav.iloc[:, 0]
        cm = calc_metrics(nav)
        final = float(nav.iloc[-1])
        print(f"{label:>36}: 年化{cm['年化收益率']} 夏普{cm['夏普比率']} "
              f"回撤{cm['最大回撤']} 期末nav {final:.3f} "
              f"累计 {final-1:+.2%}",
              flush=True)
        tr = m.get("halt_triggers", [])
        print(f"   熔断触发: {tr}", flush=True)
        nav.to_csv(f'/root/quant/data_store/ptrade_backtest_1m/'
                   f'engine_nav_{mode}_{shift}.csv')


if __name__ == "__main__":
    main()
