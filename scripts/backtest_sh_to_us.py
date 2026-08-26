"""
上证当日 → 美股当晚（2026-08-26 首测，待复验）。

机制：A股 15:00 收盘 → 美股 21:30 开盘，领先 6.5 小时，共享全球风险偏好。
口径：上证 D 日收盘涨跌 vs 美股 D 日（美东同日期）当晚收盘涨跌，同日对齐。

首测结果（2012-2026, 3412 样本）：
  上证→纳斯达克 corr 0.140 t=8.24 / 上证→标普 corr 0.135 t=7.98
  上证涨>2% → 纳指+0.492%/标普+0.408%/SOX+0.710%; 跌>2% → -0.525%/-0.429%/-0.742%
用法: python scripts/backtest_sh_to_us.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily


def tstat(x, y):
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(m) < 100:
        return np.nan, np.nan
    c = m["x"].corr(m["y"])
    return c, c * np.sqrt(len(m) - 2) / np.sqrt(1 - c * c)


def main():
    d = load_daily("000001", "2012-01-01", "2026-08-26").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    sh = pd.Series(pd.to_numeric(d["close"], errors="coerce").pct_change() * 100).dropna()

    for name, sym in [("SOX", ".SOX"), ("标普500", ".INX"), ("纳斯达克", ".IXIC")]:
        df = ak.index_us_stock_sina(symbol=sym).sort_values("date")
        df["date"] = pd.to_datetime(df["date"])
        us = df.set_index("date")["close"].astype(float).pct_change() * 100
        j = pd.DataFrame({"sh": sh, "us": us}).dropna()
        c, t = tstat(j["sh"], j["us"])
        print(f"上证当日 → {name}当晚  样本{len(j)}  corr{c:+.3f}  t{t:+.2f}  (无条件 {j['us'].mean():+.3f}%)")
        for lo, lab in [(1, "涨>1%"), (2, "涨>2%"), (-1, "跌>1%"), (-2, "跌>2%")]:
            m = j["sh"] >= lo if lo > 0 else j["sh"] <= lo
            sub = j["us"][m]
            if len(sub) >= 30:
                print(f"    上证{lab}: {len(sub):>4}次 → {name}当晚 {sub.mean():+.3f}%")


if __name__ == "__main__":
    main()
