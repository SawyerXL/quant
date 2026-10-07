"""A3 三跑制 (2026-10-07 晚执行, 判据=logs/limit_gate_preregistration.md 第四节)。

三跑同数据(10-05 快照重建后):
  ①基线(DEFAULT, 两项全关) ②+现金1.4%(cash_yield 2%→1.4%) ③+limit_gate
归因三层: 基线 vs 1.381/1.382=漂移(已记漂移日志第3/4行) / 现金=算术对照 /
limit_gate=残差(按判读矩阵)。启用逻辑=判读矩阵归因过关; 0触发=实现bug先查。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from dataclasses import replace

import pandas as pd
import numpy as np
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates

START = "2019-01-01"


def main():
    # CB 末端派生 END(两腿窗口一致, 与配比脚本同源)
    _cb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
    END = str(_cb.index[-1].date())

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist()
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
    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]

    runs = [
        ("①基线(两项全关)", DEFAULT_CONFIG),
        ("②+现金1.4%", replace(DEFAULT_CONFIG, cash_yield=0.014)),
        ("③+limit_gate", replace(DEFAULT_CONFIG, cash_yield=0.014,
                                 limit_gate=True)),
    ]
    out = {}
    print(f"{'臂':<20}{'年化':>9}{'夏普':>8}{'回撤':>10}{'涨停跳过':>9}{'跌停顺延':>9}"
          f"{'换手分解(轮换/切档/MA10卖)':>28}")
    for name, cfg in runs:
        nav, res = run_backtest(panel, ap, rebal, cfg, ic)
        r = nav.pct_change().dropna()
        ann = (nav.iloc[-1] ** (365 / ((nav.index[-1] - nav.index[0]).days)) - 1)
        sr = r.mean() / r.std() * np.sqrt(252)
        dd = (nav / nav.cummax() - 1).min()
        print(f"{name:<20}{ann*100:>+8.2f}%{sr:>8.3f}{dd*100:>+9.2f}%"
              f"{res['limit_up_blocked']:>9}{res['limit_down_blocked']:>9}"
              f"  {res['rebal_sells']:.2f}/{res['tier_sells']:.2f}/{res['exit_ma10_sells']:.2f}")
        out[name] = nav

    n0, n1, n2 = [out[k] for k, _ in runs]
    print("\n[归因] 基线 vs 1.381(09-28) = 漂移已记; 现金 delta; limit_gate delta:")
    for i in (1, 2):
        a0 = (out[runs[0][0]].iloc[-1] ** (365 / ((out[runs[0][0]].index[-1] - out[runs[0][0]].index[0]).days)) - 1)
        ai = (out[runs[i][0]].iloc[-1] ** (365 / ((out[runs[i][0]].index[-1] - out[runs[i][0]].index[0]).days)) - 1)
        print(f"  {runs[i][0]}: Δ年化 vs ① = {ai*100-a0*100:+.2f}pp")
    pd.DataFrame({k: v for k, v in out.items()}).to_csv("logs/a3_triple_nav.csv")


if __name__ == "__main__":
    main()
