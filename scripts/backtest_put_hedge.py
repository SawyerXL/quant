"""
熊市 Put 对冲可行性回测 v1（2026-08-31）。

简化模型（如实标注）:
- 标的: 510050(50ETF), 期权: 每月买入持有1个月ATM Put(不日内delta调整)
- 权利金率 = 0.4×IV×sqrt(30/365), IV用市场真实QVIX(50ETF期权隐含波动率)
- 到期回收 = max(K-S_T,0)/K × 权利金率; 未计期权交易成本(~权利金的1%)
- 组合 = 100% ETF 名义 + Put保险(权利金占净值比例动态扣除)

方案:
  A 裸ETF(对照)
  B 全程滚动对冲(每月初买1月ATM Put)
  C regime触发: 仅当ETF在MA200下方时买Put
  D 恐慌触发: 仅当ETF前5日累计跌>5%时买Put
窗口: 2015-02-09(QVIX起点)→2026-08-28
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak


def load():
    q = ak.index_option_50etf_qvix()
    q["date"] = pd.to_datetime(q["date"])
    iv = q.set_index("date")["close"].astype(float) / 100.0
    d = ak.fund_etf_hist_sina(symbol="sh510050")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    j = pd.DataFrame({"px": d["close"].astype(float)})
    j["iv"] = iv.reindex(j.index).ffill().bfill()
    j = j[j.index >= pd.Timestamp("2015-02-09")].copy()
    j["ret"] = j["px"].pct_change()
    j["ma200"] = j["px"].rolling(200).mean()
    j["ret5"] = (j["px"] / j["px"].shift(5) - 1)
    # 每月首个交易日标记
    ym = j.index.to_series().dt.to_period("M")
    j["month_start"] = ~ym.duplicated()
    return j


def simulate(j, mode):
    nav = 1.0
    hedge = None   # {"strike": K, "prem_pct": p}
    records = []
    for i, (dt, row) in enumerate(j.iterrows()):
        ret = row["ret"] if not pd.isna(row["ret"]) else 0.0
        nav *= (1 + ret)
        # 到期回收(本月买入的Put, 在下月第一个交易日到期)
        if hedge is not None and i > 0 and row["month_start"]:
            # ATM Put到期收益 = 标的跌幅(权利金已在买入时扣除, Put有1/prem的杠杆)
            payoff_rate = max(hedge["strike"] - row["px"], 0.0) / hedge["strike"]
            nav *= (1 + payoff_rate)
            hedge = None
        # 买入决策(到期回收后, 当日重新决策)
        if hedge is None and row["month_start"] and not pd.isna(row["iv"]) and row["iv"] > 0:
            buy = False
            if mode == "B":
                buy = True
            elif mode == "C":
                buy = not pd.isna(row["ma200"]) and row["px"] < row["ma200"]
            elif mode == "D":
                buy = not pd.isna(row["ret5"]) and row["ret5"] < -0.05
            if buy:
                prem = 0.4 * row["iv"] * np.sqrt(30 / 365)
                nav *= (1 - prem)
                hedge = {"strike": row["px"], "prem_pct": prem}
        records.append(nav)
    return pd.Series(records, index=j.index)


def metrics(nav):
    r = nav.pct_change().dropna()
    days = (nav.index[-1] - nav.index[0]).days
    ann = (nav.iloc[-1] ** (365 / days) - 1)
    dd = (nav / nav.cummax() - 1).min()
    calmar = ann / abs(dd) if dd < 0 else float("nan")
    return ann, dd, calmar


def main():
    j = load()
    print(f"窗口 {j.index[0].date()} → {j.index[-1].date()}, {len(j)}天")
    print(f"{'方案':<24}{'年化':>9}{'最大回撤':>10}{'Calmar':>9}")
    for mode, label in [("A", "裸ETF(对照)"), ("B", "全程对冲"), ("C", "regime触发(MA200下)"), ("D", "恐慌触发(5日跌>5%)")]:
        nav = simulate(j, mode)
        ann, dd, cal = metrics(nav)
        print(f"{label:<24}{ann*100:>+8.2f}%{dd*100:>9.2f}%{cal:>9.2f}")
    # 急跌月明细(全程对冲B的保护)
    print("\n急跌月保护明细(方案B全程对冲):")
    jm = j[j["month_start"]].copy()
    jm["ret_m"] = j["px"].pct_change()
    for dt, row in jm.iterrows():
        if not pd.isna(row["ret_m"]) and row["ret_m"] < -0.03:
            prem = 0.4 * row["iv"] * np.sqrt(30 / 365) if not pd.isna(row["iv"]) else 0
            nxt = j.index[j.index > dt][:21]
            if len(nxt) == 0:
                continue
            worst = (j["px"].loc[nxt].min() / row["px"] - 1)
            payoff = max(-worst, 0) if worst < 0 else 0
            print(f"  {str(dt)[:10]} 月跌{row['ret_m']*100:+.1f}% IV={row['iv']*100:.0f}% "
                  f"保费{prem*100:.1f}% 月内最差{worst*100:.1f}% 回收≈{payoff*100:.1f}%")


if __name__ == "__main__":
    main()
