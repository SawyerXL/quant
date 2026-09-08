# -*- coding: utf-8 -*-
"""引擎2020年敞口诊断: 为什么2020-03空仓 — 逐日 实际权重/目标档/净值"""
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

LO, HI = "2019-01-01", "2020-12-31"


def main():
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
    sh = load_daily("000001", "2014-06-01", "2020-12-31")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if LO <= d <= HI]

    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(),
                            "pool_size": 60, "lot_size": 100,
                            "initial_capital": 1_000_000.0,
                            "ma200_thresh_shift": 0.0,
                            "halt_mode": "B", "halt_dd_limit": 0.15,
                            "diag_exposure": True})
    nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
    cm = calc_metrics(nav)
    print(f"窗口 {LO}~{HI}: 年化{cm['年化收益率']} 回撤{cm['最大回撤']} "
          f"触发{m.get('halt_triggers')}")
    ex = pd.DataFrame(m["exposure_ts"], columns=["date", "w", "target", "nav"])
    ex["date"] = pd.to_datetime(ex["date"])
    ex = ex.set_index("date")
    print("\n2020年敞口(周频抽样): 实际权重/目标档/净值")
    print(ex["2020-01-01":"2020-06-30"].iloc[::5].round(3).to_string())
    print("\n首次交易前的敞口(2019-12下旬):")
    print(ex["2019-12-15":"2019-12-31"].round(3).to_string())


if __name__ == "__main__":
    main()
