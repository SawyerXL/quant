"""
重大政策/消息事件研究（事件日期经网络搜索确认，本地数据交叉验证）。

事件清单来源: WebSearch(印花税9次/汇金7次/国九条3次/924/贸易战/疫情/熔断)。
D = 事件后首个可交易日。每事件拆三段:
  gap   = 昨收→今开 (跳空, 开盘前已定价)
  intra = 今开→今收 (盘中, 开盘追进才吃得到)
  fwd20 = D收盘起20交易日
方法论语境: 与外盘信号同一套 —— 事件当天涨多少不重要, 重要的是
"开盘追进去还能赚多少(intra)" 和 "之后20日还有没有持续性"。

用法: python scripts/event_study.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from data.storage import load_daily

# (事件名, 发生日期, 方向: +利多 / -利空冲击)
EVENTS = [
    # 印花税
    ("印花税6‰→3‰", "1991-10-10", 1), ("印花税3‰→5‰", "1997-05-12", -1),
    ("印花税5‰→4‰", "1998-06-12", 1), ("印花税4‰→2‰", "2001-11-16", 1),
    ("印花税2‰→1‰", "2005-01-23", 1), ("印花税1‰→3‰(530)", "2007-05-30", -1),
    ("印花税3‰→1‰", "2008-04-24", 1), ("印花税改单边", "2008-09-19", 1),
    ("印花税减半", "2023-08-28", 1),
    # 汇金增持(公告次日生效)
    ("汇金增持#1", "2008-09-19", 1), ("汇金增持#2", "2009-10-12", 1),
    ("汇金增持#3", "2011-10-11", 1), ("汇金增持#4", "2012-10-10", 1),
    ("汇金增持#5", "2013-06-13", 1), ("汇金增持#6(ETF)", "2015-07-06", 1),
    ("汇金增持#7", "2023-10-12", 1),
    # 国九条(周五收盘后发布, 次一交易日生效)
    ("国九条#1", "2004-02-02", 1), ("国九条#2", "2014-05-12", 1),
    ("国九条#3", "2024-04-15", 1),
    # 其他
    ("924一揽子政策", "2024-09-24", 1),
    ("贸易战301备忘录", "2018-03-23", -1),
    ("疫情后首日", "2020-02-03", -1),
    ("熔断#1", "2016-01-04", -1), ("熔断#2", "2016-01-07", -1),
]


def main():
    d = load_daily("000001", "1990-01-01", "2026-08-26").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    d["open"] = pd.to_numeric(d["open"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["open", "close"])

    # 事件日 → 首个交易日
    idx = d.index
    for i, (name, day, sign) in enumerate(EVENTS):
        pos = idx.searchsorted(pd.Timestamp(day))
        EVENTS[i] = (name, idx[min(pos, len(idx) - 1)], sign)

    print(f"{'事件':<22}{'D':>12}{'跳空':>8}{'盘中':>8}{'当日':>8}{'+5日':>8}{'+20日':>9}{'20日胜率':>9}")
    rows = []
    for name, D, sign in EVENTS:
        gap = (d["open"].loc[D] / d["close"].shift(1).loc[D] - 1) * 100
        day = (d["close"].loc[D] / d["close"].shift(1).loc[D] - 1) * 100
        intra = (d["close"].loc[D] / d["open"].loc[D] - 1) * 100
        fwd5 = (d["close"].shift(-5).loc[D] / d["close"].loc[D] - 1) * 100
        fwd20 = (d["close"].shift(-20).loc[D] / d["close"].loc[D] - 1) * 100
        wr = "+" if fwd20 > 0 else "-"
        print(f"{name:<22}{str(D)[:10]:>12}{gap:>+7.2f}%{intra:>+7.2f}%{day:>+7.2f}%"
              f"{fwd5:>+7.2f}%{fwd20:>+8.2f}%{wr:>9}")
        rows.append({"name": name, "sign": sign, "gap": gap, "intra": intra,
                     "day": day, "fwd5": fwd5, "fwd20": fwd20})

    f = pd.DataFrame(rows)
    print("\n" + "=" * 70)
    print("聚合：利多政策事件 vs 利空冲击事件")
    print("=" * 70)
    base5 = (d["close"].shift(-5) / d["close"] - 1).dropna().mean() * 100
    base20 = (d["close"].shift(-20) / d["close"] - 1).dropna().mean() * 100
    print(f"无条件基准(任意日): +5日 {base5:+.2f}%  +20日 {base20:+.2f}%\n")
    for sign, label in [(1, "利多政策(印花税降/汇金/国九条/924)"), (-1, "利空冲击(印花税升/贸易战/疫情/熔断)")]:
        s = f[f["sign"] == sign]
        print(f"{label}: {len(s)}次")
        print(f"  事件日: 跳空{s['gap'].mean():+.2f}% + 盘中{s['intra'].mean():+.2f}% = 当日{s['day'].mean():+.2f}%")
        print(f"   → 盘中段占比 {s['intra'].sum()/s['day'].sum()*100:.0f}% (开盘追进能吃到多少)")
        print(f"  持续: +5日{s['fwd5'].mean():+.2f}% (vs基准{base5:+.2f}%)  "
              f"+20日{s['fwd20'].mean():+.2f}% (vs基准{base20:+.2f}%)  胜率{(s['fwd20']>0).mean()*100:.0f}%")
        print()


if __name__ == "__main__":
    main()
