"""
英伟达财报事件研究（财报日期经网络搜索确认，多来源交叉）。

时间链: 财报美东D盘后16:20(北京D+1凌晨04:20) → 美股盘后交易 →
        北京D+1 09:30 A股开盘前已知 → A股D+1反应。

测三件事:
  1. 财报日D当天美股(SOX/纳指): "提前反应"(财报前情绪定价)
  2. 财报次一美东交易日D+1美股: 财报实际反应("财报后必跌魔咒")
  3. A股北京D+1: 上证/科创50/曙光/雅克, 拆跳空+盘中(跳空=受影响度量)

样本13个季度(2023-05~2026-05), 小样本只给描述统计。
用法: python scripts/nvda_earnings_study.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily

# (财报日美东, 财季)
EARNINGS = [
    ("2023-05-24", "Q1FY24"), ("2023-08-23", "Q2FY24"), ("2023-11-21", "Q3FY24"),
    ("2024-02-21", "Q4FY24"), ("2024-05-22", "Q1FY25"), ("2024-08-28", "Q2FY25"),
    ("2024-11-20", "Q3FY25"), ("2025-02-26", "Q4FY25"), ("2025-05-28", "Q1FY26"),
    ("2025-08-27", "Q2FY26"), ("2025-11-19", "Q3FY26"), ("2026-02-25", "Q4FY26"),
    ("2026-05-20", "Q1FY27"),
]


def us_series(sym):
    df = ak.index_us_stock_sina(symbol=sym).sort_values("date")
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["close"].astype(float)
    s = s[~s.index.duplicated(keep="last")]
    return s.pct_change() * 100


def a_frame(code):
    d = load_daily(code, "2023-01-01", "2026-08-26").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    d["open"] = pd.to_numeric(d["open"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    f = pd.DataFrame({
        "ret": d["close"].pct_change() * 100,
        "gap": (d["open"] / d["close"].shift(1) - 1) * 100,
        "intra": (d["close"] / d["open"] - 1) * 100,
    }).dropna()
    return f


def stats(s, label):
    s = s.dropna()
    if len(s) == 0:
        print(f"  {label:<16} 无样本")
        return
    print(f"  {label:<16} n={len(s):>2}  均值{s.mean():>+7.2f}%  中位{s.median():>+7.2f}%  "
          f"正{s[(s>0)].count()}/{len(s)}  ({s.min():+.2f}~{s.max():+.2f})")


def main():
    sox = us_series(".SOX")
    ndx = us_series(".IXIC")
    a = {c: a_frame(c) for c in ["000001", "000688", "603019", "002409"]}

    print("=" * 74)
    print("1. 财报日D当天美股表现（'提前反应'）")
    print("=" * 74)
    for name, s in [("SOX", sox), ("纳指", ndx)]:
        vals = [s.get(pd.Timestamp(d)) for d, _ in EARNINGS]
        stats(pd.Series(vals), f"{name}财报日当天")
    print(f"  对照: SOX任意日均值 {sox.mean():+.2f}%, 纳指 {ndx.mean():+.2f}%\n")

    print("=" * 74)
    print("2. 财报次一美东交易日（财报实际反应）")
    print("=" * 74)
    for name, s in [("SOX", sox), ("纳指", ndx)]:
        vals = []
        for d, q in EARNINGS:
            i = s.index.searchsorted(pd.Timestamp(d))
            if i < len(s) - 1:
                vals.append(s.iloc[i + 1])
        stats(pd.Series(vals), f"{name}财报次日")

    print("\n" + "=" * 74)
    print("3. A股反应（北京D+1，财报在开盘前已知）")
    print("=" * 74)
    for code, name in [("000001", "上证"), ("000688", "科创50"), ("603019", "中科曙光"), ("002409", "雅克科技")]:
        f = a[code]
        r, g, it = [], [], []
        for d, q in EARNINGS:
            # 财报美东D盘后 = 北京D+1凌晨 → 北京D+1是第一个可交易A股日
            i = f.index.searchsorted(pd.Timestamp(d))
            if i < len(f) - 1:
                r.append(f["ret"].iloc[i + 1])
                g.append(f["gap"].iloc[i + 1])
                it.append(f["intra"].iloc[i + 1])
        print(f"--- {name} {code}  (对照任意日: 收{f['ret'].mean():+.2f}% = 跳空{f['gap'].mean():+.2f} + 盘中{f['intra'].mean():+.2f})")
        stats(pd.Series(r), "收→收")
        stats(pd.Series(g), "跳空(受影响)")
        stats(pd.Series(it), "盘中(后续)")


if __name__ == "__main__":
    main()
