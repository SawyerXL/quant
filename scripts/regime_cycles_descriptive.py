"""牛熊周期描述性表（2026-10-23，Phase 1）。

只描述、不预测：用 20% 反转阈值把上证月度收盘切成峰谷，列出每段的
起止/涨跌幅/时长，以及**起点月份**的估值、流动性、供给、两融。
不用于交易，不产生 p 值；周期标签是事后描述，不能当预测特征。

用法: python scripts/regime_cycles_descriptive.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
PANEL = ROOT / "data_store" / "regime" / "panel_monthly.parquet"
OUT_MD = ROOT / "logs" / "regime_cycles_descriptive_20261023.md"
OUT_CSV = ROOT / "logs" / "regime_cycles_descriptive_20261023.csv"
THRESH = 0.20


def zigzag(s: pd.Series, thresh: float = THRESH):
    """20% 反转的峰谷；返回 [(date, price, kind)]，kind=peak/trough。

    2026-10-23 修：初始 trend=0 时必须分别跟踪 hi/lo——原实现用同一个 ext
    同时做峰/谷跟踪，下跌时 ext 被谷分支改写，导致 2001-2005 熊市与
    2005-2007 牛市整段漏检。
    """
    idx = list(s.index)
    vals = list(s.values.astype(float))
    pivots = []
    trend = 0  # 1=up, -1=down, 0=unknown
    hi_v, hi_i = vals[0], 0
    lo_v, lo_i = vals[0], 0
    for i in range(1, len(vals)):
        v = vals[i]
        if trend == 0:
            if v > hi_v:
                hi_v, hi_i = v, i
            if v < lo_v:
                lo_v, lo_i = v, i
            if v <= hi_v * (1 - thresh):
                pivots.append((idx[hi_i], hi_v, "peak"))
                trend, lo_v, lo_i = -1, v, i
            elif v >= lo_v * (1 + thresh):
                pivots.append((idx[lo_i], lo_v, "trough"))
                trend, hi_v, hi_i = 1, v, i
        elif trend > 0:
            if v > hi_v:
                hi_v, hi_i = v, i
            elif v <= hi_v * (1 - thresh):
                pivots.append((idx[hi_i], hi_v, "peak"))
                trend, lo_v, lo_i = -1, v, i
        else:
            if v < lo_v:
                lo_v, lo_i = v, i
            elif v >= lo_v * (1 + thresh):
                pivots.append((idx[lo_i], lo_v, "trough"))
                trend, hi_v, hi_i = 1, v, i
    # 最后一个未确认的极值
    if trend == 1:
        pivots.append((idx[hi_i], hi_v, "peak?"))
    elif trend == -1:
        pivots.append((idx[lo_i], lo_v, "trough?"))
    # 去掉重复/相邻同类
    out = []
    for p in pivots:
        if out and out[-1][2][:5] == p[2][:5]:
            continue
        out.append(p)
    return out


def _at(df, d):
    m = pd.Timestamp(d).to_period("M")
    mask = df.index.to_period("M") == m
    if not mask.any():
        return {}
    return df[mask].iloc[0]


def main():
    d = pd.read_parquet(PANEL)
    d["month"] = pd.to_datetime(d["month"])
    d = d.set_index("month").sort_index()
    s = d["sh_close"].dropna()
    pv = zigzag(s)
    rows = []
    for a, b in zip(pv, pv[1:]):
        d0, p0, k0 = a
        d1, p1, k1 = b
        rows.append({
            "start": str(d0)[:10], "end": str(d1)[:10],
            "kind": f"{k0}→{k1}", "months": (d1.year - d0.year) * 12 + d1.month - d0.month,
            "ret_pct": round((p1 / p0 - 1) * 100, 1),
            "start_price": round(p0, 1), "end_price": round(p1, 1),
        })
    df = pd.DataFrame(rows)
    # 起点指标
    ind_cols = ["pe_ttm", "pe_pct_10y", "erp", "m1_yoy", "m2_yoy",
                "m1_m2_gap", "cgb10y", "shibor3m", "lpr1y", "margin_bal",
                "margin_chg_3m", "sf_equity", "sf_equity_12m",
                "new_fund_equity", "sh_ma200_dist"]
    for c in ind_cols:
        df[c] = [round(float(_at(d, x).get(c, np.nan)), 2)
                 if pd.notna(_at(d, x).get(c, np.nan)) else np.nan
                 for x in df["start"]]
    df.to_csv(OUT_CSV, index=False)

    lines = [
        "# 上证牛熊周期描述性表（2026-10-23，20% 反转阈值）", "",
        "> 只描述、不预测。峰谷是事后标签；起点指标用于观察“什么样的状态之后"
        "出现了牛/熊”，不代表预测能力。", "",
        "| 起 | 止 | 类型 | 月数 | 涨跌幅 | 起点PE | PE分位 | ERP | M1-M2 | 10Y | "
        "两融3月变化 | 股票融资12M | MA200距离 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in df.iterrows():
        def f(x, n=1):
            return "—" if pd.isna(x) else f"{x:.{n}f}"
        lines.append(
            f"| {r['start'][:7]} | {r['end'][:7]} | {r['kind']} | {r['months']} | "
            f"{r['ret_pct']:+.1f}% | {f(r['pe_ttm'])} | {f(r['pe_pct_10y'],2)} | "
            f"{f(r['erp'])} | {f(r['m1_m2_gap'])} | {f(r['cgb10y'])} | "
            f"{f(r['margin_chg_3m'])} | {f(r['sf_equity_12m'],0)} | "
            f"{f(r['sh_ma200_dist'])} |")
    s2 = d["sh_close"].dropna()
    last_m, last_v = s2.index[-1], float(s2.iloc[-1])
    hi_v = float(s2.loc["2024-01":].max()); hi_m = s2.loc["2024-01":].idxmax()
    r_last = d.iloc[-1]
    lines += [
        "", "## 当前状态（2026-10）", "",
        f"- 上证 {last_v:.1f}；距 2024 以来高点 {hi_v:.1f}（{hi_m:%Y-%m}）"
        f"回撤 {100*(last_v/hi_v-1):+.1f}%——20% 阈值下尚未确认新熊段。",
        f"- PE {r_last.get('pe_ttm')}、ERP {r_last.get('erp'):.2f}%、"
        f"MA200 距离 {r_last.get('sh_ma200_dist'):.1f}%、"
        f"两融3月变化 {r_last.get('margin_chg_3m'):.1f}%。",
        "", "## 描述性观察（不下结论）", "",
        f"- 共识别 {len(df)} 段（20% 阈值，月度收盘）；未完成段以 `?` 标注。",
        "- 2005-2009 的 PE 分位缺失（10 年滚动窗需要 2010 年后才有 60 个观测），"
        "看原始 PE/ERP。",
        "- 社融/股票融资 2015-01 起、SHIBOR 2015-05 起、两融 2010-03 起、"
        "新基金 2023 起——早期周期的这些列是空的，不能把“缺失”读成“低”。",
        "- 周期表只用于提出假设：例如“PE 分位低+ERP 高+两融止跌”之后是否更常出现牛段。"
        "任何“是否显著”必须走 Phase 2 预注册 + OOS，不得从本表直接推断。",
        "", f"- 明细：`{OUT_CSV}`",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(df[["start", "end", "kind", "months", "ret_pct", "pe_ttm", "erp"]]
          .to_string(index=False))
    print("->", OUT_MD, OUT_CSV)


if __name__ == "__main__":
    main()
