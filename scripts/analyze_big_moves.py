"""
A股大涨大跌的量化归因（上证指数 2012-2026）。

方法论声明（重要）：
  回测测不出"原因"。政策/消息/情绪是叙事，本机没有事件库，测不了。
  能测的是：可量化、且在事发前就能拿到的特征，有没有真实预测力。

  必须区分两件常被混为一谈的事：
    - 同期伴随：大跌当天成交量放大 —— 这是废话，事后才知道
    - 事前可测：昨天的波动率能否预测今天大跌 —— 这才有用
  所有特征一律 shift(1)，只用 T-1 及之前的信息预测 T 日。

用法: python scripts/analyze_big_moves.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from data.storage import load_daily

BIG = 2.0          # 大涨大跌阈值 %
START, END = "2012-01-01", "2026-08-25"


def build():
    d = load_daily("000001", START, END).sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    vol = pd.to_numeric(d["volume"], errors="coerce")   # amount在指数库里是0，用volume
    f = pd.DataFrame({"close": cl, "volume": vol}).dropna()
    f["ret"] = f["close"].pct_change() * 100

    # ── 事前特征：全部 shift(1)，T日预测只用到T-1收盘 ──
    f["ret20"] = (f["close"] / f["close"].shift(20) - 1).shift(1) * 100      # 前期涨幅
    f["vol20"] = f["ret"].rolling(20).std().shift(1)                          # 前期波动率
    f["vr"] = (f["volume"] / f["volume"].rolling(20).mean()).shift(1)         # 量比
    f["ma200"] = f["close"].rolling(200).mean()
    f["dist200"] = (f["close"] / f["ma200"] - 1).shift(1) * 100
    f["prev"] = f["ret"].shift(1)                                             # 昨日涨跌
    down = f["ret"] < 0
    cons = down.groupby((~down).cumsum()).cumsum()
    f["cons_down"] = cons.shift(1)                                            # 已连跌天数
    return f.dropna()


def sec(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def bucket_table(f, col, label, bins, target):
    """分组统计目标事件发生率，对比无条件基准。"""
    base = target.mean() * 100
    g = pd.cut(f[col], bins)
    print(f"\n  按 {label} 分组 → {'大跌' if '跌' in label or True else ''}发生率 (基准 {base:.1f}%)")
    print(f"  {'区间':<22}{'样本':>7}{'发生率':>9}{'vs基准':>10}")
    for k, idx in target.groupby(g, observed=False).groups.items():
        sub = target.loc[idx]
        if len(sub) < 30:
            continue
        p = sub.mean() * 100
        print(f"  {str(k):<22}{len(sub):>7}{p:>8.1f}%{p-base:>+9.1f}pp")


def main():
    f = build()
    print(f"上证指数 {f.index[0].date()} → {f.index[-1].date()}，{len(f)}个交易日")
    r = f["ret"]

    # ── 1. 大涨大跌有多少、怎么分布 ──
    sec("1. 大涨大跌的基本分布")
    up, dn = r >= BIG, r <= -BIG
    print(f"  大涨(≥+{BIG}%): {up.sum()}天 ({up.mean()*100:.1f}%)   "
          f"大跌(≤-{BIG}%): {dn.sum()}天 ({dn.mean()*100:.1f}%)")
    print(f"  日均{r.mean():+.3f}%  日波动{r.std():.2f}%  "
          f"最大单日 {r.max():+.2f}% ({r.idxmax().date()})  最小 {r.min():+.2f}% ({r.idxmin().date()})")
    yr = pd.DataFrame({"大涨": up, "大跌": dn, "年收益": r}).groupby(f.index.year).agg(
        {"大涨": "sum", "大跌": "sum", "年收益": lambda x: (1 + x / 100).prod() * 100 - 100})
    print(f"\n  {'年份':<7}{'大涨':>6}{'大跌':>6}{'合计':>6}{'年收益':>10}")
    for y, row in yr.iterrows():
        print(f"  {y:<7}{int(row['大涨']):>6}{int(row['大跌']):>6}"
              f"{int(row['大涨']+row['大跌']):>6}{row['年收益']:>9.1f}%")

    # ── 2. 聚集性：这是最强的可预测结构 ──
    sec("2. 波动聚集 —— 大幅波动不是随机撒开的")
    bigday = (r.abs() >= BIG)
    p_uncond = bigday.mean() * 100
    p_after = bigday[bigday.shift(1).fillna(False)].mean() * 100
    p_after_calm = bigday[~bigday.shift(1).fillna(True)].mean() * 100
    print(f"  无条件: 任一天是大波动日的概率      {p_uncond:.1f}%")
    print(f"  昨天是大波动日 → 今天也是的概率     {p_after:.1f}%   ({p_after/p_uncond:.2f}倍)")
    print(f"  昨天是平静日   → 今天是大波动的概率 {p_after_calm:.1f}%   ({p_after_calm/p_uncond:.2f}倍)")
    print(f"\n  → 波动聚集是真实且强的。但注意：它预测的是'会不会大动'，不是'往哪动'。")
    up_after_dn = (r > 0)[dn.shift(1).fillna(False)].mean() * 100
    print(f"     大跌次日上涨概率 {up_after_dn:.1f}% vs 无条件上涨概率 {(r>0).mean()*100:.1f}% —— 方向几乎没信息")

    # ── 3. 事前特征对"大跌"的预测力 ──
    sec("3. 事前特征能预测大跌吗（全部只用T-1信息）")
    for col, label, bins in [
        ("vol20", "前20日波动率", [0, 0.8, 1.2, 1.8, 2.5, 99]),
        ("ret20", "前20日涨幅%", [-99, -10, -3, 3, 10, 99]),
        ("dist200", "距MA200%", [-99, -15, -5, 5, 15, 99]),
        ("vr", "前日量比", [0, 0.8, 1.0, 1.3, 2.0, 99]),
    ]:
        bucket_table(f, col, label, bins, dn)

    # ── 4. 最有操作价值的：大跌之后该干什么 ──
    sec("4. 大跌之后 —— 抄底还是跑（这才是能操作的部分）")
    fwd = {n: (f["close"].shift(-n) / f["close"] - 1) * 100 for n in (1, 5, 20)}
    print(f"  {'情形':<34}{'样本':>6}{'次日':>9}{'5日':>9}{'20日':>9}{'20日胜率':>10}")

    def row(name, mask):
        m = mask & fwd[20].notna()
        if m.sum() < 8:
            print(f"  {name:<34}{m.sum():>6}  样本不足，不下结论")
            return
        print(f"  {name:<34}{m.sum():>6}{fwd[1][m].mean():>8.2f}%{fwd[5][m].mean():>8.2f}%"
              f"{fwd[20][m].mean():>8.2f}%{(fwd[20][m]>0).mean()*100:>9.1f}%")

    row("全样本(基准)", pd.Series(True, index=f.index))
    row("大跌当天", dn)
    row("  └ 大跌 + MA200上方", dn & (f["dist200"] > 0))
    row("  └ 大跌 + MA200下方", dn & (f["dist200"] < 0))
    row("  └ 大跌 + 缩量(量比<1)", dn & (f["vr"] < 1))
    row("  └ 大跌 + 放量(量比>1.5)", dn & (f["vr"] > 1.5))
    row("  └ 大跌 + 已连跌≥3天", dn & (f["cons_down"] >= 3))
    row("  └ 大跌 + 前20日已跌>10%", dn & (f["ret20"] < -10))
    row("  └ 大跌+连跌≥3+缩量 (恐慌底)", dn & (f["cons_down"] >= 3) & (f["vr"] < 1))
    print()
    row("大涨当天", up)
    row("  └ 大涨 + MA200上方", up & (f["dist200"] > 0))
    row("  └ 大涨 + MA200下方", up & (f["dist200"] < 0))
    row("  └ 大涨 + 前20日已涨>10%", up & (f["ret20"] > 10))

    # ── 5. 大涨大跌贡献了多少总收益 ──
    sec("5. 踏空成本 —— 大涨日占了多少收益")
    total = (1 + r / 100).prod()
    yrs = len(r) / 243
    def cagr(x): return (x ** (1 / yrs) - 1) * 100
    print(f"  全程持有            累计{(total-1)*100:>9.1f}%   年化{cagr(total):>6.2f}%")
    for k in (5, 10, 20, 30):
        top = r.nlargest(k).index
        v = (1 + r.drop(top) / 100).prod()
        print(f"  错过最大的{k:>2}个涨日   累计{(v-1)*100:>9.1f}%   年化{cagr(v):>6.2f}%")
    for k in (5, 10, 20, 30):
        bot = r.nsmallest(k).index
        v = (1 + r.drop(bot) / 100).prod()
        print(f"  躲过最大的{k:>2}个跌日   累计{(v-1)*100:>9.1f}%   年化{cagr(v):>6.2f}%")
    both = r.drop(r.nlargest(20).index.union(r.nsmallest(20).index))
    v = (1 + both / 100).prod()
    print(f"  涨跌各躲20天(不可能) 累计{(v-1)*100:>9.1f}%   年化{cagr(v):>6.2f}%")


if __name__ == "__main__":
    main()
