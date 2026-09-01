"""
时点运气检验分解（2026-09-01）：10条平移路径的年度收益分解。

why: 极差16.64pp远超2pp判读线, 且+6~+9日单调爬升不像纯随机噪声。
     下结论前必须回答: 极差来自哪些年份——全期均匀=真时点方差;
     单年爆发=样本脆弱(回测复验纪律第3条)。
口径: 年末NAV chain-linked, 与timing_luck.py同引擎同配置同面板。
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
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-08-28"


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
    print(f"Panel: {len(prices)}只, {panel.shape[0]}天", flush=True)

    # 数据完整性检查(回测复验纪律强制项)
    rets = panel.pct_change()
    dirty = (rets.abs() > 0.5)
    n_dirty = int(dirty.sum().sum())
    print(f"脏跳(>50%): {n_dirty}个", flush=True)

    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base_rebal = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]

    years = list(range(2019, 2027))
    year_end = {}          # off -> {year: 年末NAV}
    ann = {}               # off -> 年化
    for off in range(10):
        idx_map = {d: i for i, d in enumerate(cal)}
        shifted = []
        for d in base_rebal:
            i = idx_map.get(d, 0) + off
            if i < len(cal):
                shifted.append(cal[i])
        rebal = [d for d in shifted if START <= d <= END]
        nav, _ = run_backtest(panel, ap, rebal, DEFAULT_CONFIG, ic)
        cm = calc_metrics(nav)
        ann[off] = float(cm["年化_float"])
        ye = nav.groupby(nav.index.year).last().to_dict()
        year_end[off] = ye
        print(f"+{off}日: 年化{ann[off]*100:+.2f}% 末NAV{nav.iloc[-1]:.3f}",
              flush=True)

    # 年度链式收益表: 每格=(该年末NAV/上年末NAV-1)×100, 2019基准1.0
    print(f"\n{'偏移':<6}" + "".join(f"{y:>9}" for y in years) +
          f"{'全期':>9}", flush=True)
    for off in range(10):
        prev = 1.0
        row = []
        for y in years:
            v = year_end[off].get(y)
            if v is not None:
                row.append(f"{(v/prev-1)*100:>+8.1f}%")
                prev = v
            else:
                row.append(f"{'':>9}")
        row.append(f"{(prev-1)*100:>+8.1f}%")
        print(f"+{off}日 " + "".join(row), flush=True)

    # 每年度10路径间极差(年末NAV口径), 定位极差来源
    print(f"\n{'年份':<8}{'10路径末NAV极差':>18}{'最小':>10}{'最大':>10}",
          flush=True)
    for y in years:
        vals = [year_end[off][y] for off in range(10) if y in year_end[off]]
        if len(vals) == 10:
            print(f"  {y}    {(max(vals)/min(vals)-1)*100:>+14.1f}%   "
                  f"{min(vals):>10.3f}{max(vals):>10.3f}", flush=True)


if __name__ == "__main__":
    main()
