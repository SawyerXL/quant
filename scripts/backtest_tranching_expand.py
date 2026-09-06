"""
摊平扩组权衡（2026-09-06，研究冻结期四项工程之一——唯一实证有效的杠杆）。

问题: 2组(50万/组)只消19%方差(ρ=0.63); 3组(33.3万)/4组(25万)消更多方差
但加重 floor-to-lot 伤害。输出权衡曲线供用户定夺。
口径: pool30×lot×降档3%(部署口径), 10路径, 3档组资金{50/33.3/25万}。
输出: 各档资金的10路径极差/均值/2-3-4组摊平年化/跳票数/回撤。
数据: 每日收益序列落 logs/tranched_paths_{cap}w.parquet 供CB相关性复用。
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

START, END = "2019-01-01", "2026-08-28"
OUT = Path(__file__).parent.parent / "logs"
CAPS = [500_000.0, 333_333.0, 250_000.0]


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
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def path(off):
        shifted = [cal[idx.get(d, 0) + off] for d in base
                   if idx.get(d, 0) + off < len(cal)]
        return [d for d in shifted if START <= d <= END]

    for cap in CAPS:
        w = int(cap / 10000)
        anns, rets, skips = [], [], []
        for off in range(10):
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), "pool_size": 30,
                                    "lot_size": 100, "initial_capital": cap})
            nav, info = run_backtest(panel, ap, path(off), cfg, ic)
            anns.append(float(calc_metrics(nav)["年化_float"]))
            rets.append(nav.pct_change().dropna())
            skips.append(info["lot_skips"])
        j = pd.concat(rets, axis=1).dropna()
        j.columns = [f"p{o}" for o in range(10)]
        j.to_parquet(OUT / f"tranched_paths_{w}w.parquet")
        spread = (max(anns) - min(anns)) * 100
        print(f"{w}万/组: 路径均值{np.mean(anns)*100:+.2f}% 极差{spread:.2f}pp "
              f"跳票{sum(skips)} | "
              f"2组摊平{_ens(j, [0,5])*100:+.2f}% 3组{_ens(j, [0,3,7])*100:+.2f}% "
              f"4组{_ens(j, [0,2,5,7])*100:+.2f}%",
              flush=True)


def _ens(j, cols):
    e = (1 + j.iloc[:, list(cols)].mean(axis=1)).cumprod()
    return float(calc_metrics(e)["年化_float"])


if __name__ == "__main__":
    main()
