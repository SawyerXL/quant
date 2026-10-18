"""大陆可买美国行业指数基金(石油/医药)对比: 新浪全史+拆分校准+名称核验。"""
from pathlib import Path
import akshare as ak
import pandas as pd
import numpy as np

CAND = [
    # 石油
    "sz162411", "sz162719", "sz160416", "sh501018", "sz161129", "sz160723",
    # 医药
    "sz161126", "sz161127", "sh513290", "sz159502",
    # 参考(宽基/消费)
    "sh513100", "sh513500", "sz162415",
]
END = pd.Timestamp("2026-10-16")

names = {}
for cat in ("ETF基金", "LOF基金"):
    try:
        d = ak.fund_etf_category_sina(symbol=cat)
        for _, r in d.iterrows():
            names[str(r["代码"])] = str(r["名称"])
    except Exception as e:
        print("name list err", cat, e)

def load_adjusted(sym):
    d = ak.fund_etf_hist_sina(symbol=sym)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    c = pd.to_numeric(d.set_index("date")["close"], errors="coerce").dropna()
    c = c[c.index <= END]
    r = c.pct_change()
    for dt in r[r < -0.15].index:  # 份额折算(场内基金日涨跌限10%, <-15%即折算)
        prev = c.shift(1).loc[dt]
        c.loc[c.index < dt] = c.loc[c.index < dt] * (c.loc[dt] / prev)
    return c

def ann(sub):
    return (sub.iloc[-1] / sub.iloc[0]) ** (365.25 / max((sub.index[-1] - sub.index[0]).days, 1)) - 1

rows = []
series = {}
for sym in CAND:
    try:
        c = load_adjusted(sym)
        if len(c) < 30:
            print(sym, "数据不足", len(c))
            continue
        series[sym] = c
        first, last = c.index[0], c.index[-1]
        rec = {"code": sym, "name": names.get(sym, "?"), "first": str(first.date()),
               "years": round((last - first).days / 365.25, 1),
               "自成立年化": round(float(ann(c)) * 100, 2)}
        for label, days in (("近1年", 365), ("近3年", 365 * 3), ("近5年", 365 * 5)):
            start = END - pd.Timedelta(days=days)
            sub = c[c.index >= start]
            rec[label] = round(float(ann(sub)) * 100, 2) if len(sub) > 20 and (sub.index[0] - start).days <= 10 else None
        sub5 = c[c.index >= END - pd.Timedelta(days=365 * 5)]
        rec["近5年回撤"] = round(float((sub5 / sub5.cummax() - 1).min()) * 100, 1) if len(sub5) > 20 else None
        sub3 = c[c.index >= END - pd.Timedelta(days=365 * 3)]
        rec["近3年波动"] = round(float(sub3.pct_change().std() * np.sqrt(252)) * 100, 1) if len(sub3) > 20 else None
        rows.append(rec)
    except Exception as e:
        print(sym, "ERR", type(e).__name__, str(e)[:80])

df = pd.DataFrame(rows)
print("\n=== 自成立/近1/3/5年(拆分校准后, %) ===")
print(df.to_string(index=False))

# 共同窗口
for start in (pd.Timestamp("2021-01-04"), pd.Timestamp("2023-11-02")):
    print(f"\n=== 共同窗口 {start.date()} → {END.date()} ===")
    out = []
    for sym, c in series.items():
        if c.index[0] > start + pd.Timedelta(days=10):
            continue
        sub = c[c.index >= start]
        sub = sub[sub.index <= END]
        if len(sub) < 20:
            continue
        mdd = float((sub / sub.cummax() - 1).min())
        vol = float(sub.pct_change().std() * np.sqrt(252))
        out.append((names.get(sym, "?"), sym, ann(sub) * 100, mdd * 100, vol * 100))
    out.sort(key=lambda x: -x[2])
    for name, sym, a, m, v in out:
        print(f"{name:<22}{sym}  年化 {a:+7.2f}%  回撤 {m:6.1f}%  波动 {v:5.1f}%")

import json
Path("/root/quant/logs/us_sector_funds_20261018.json").write_text(
    json.dumps({"generated_at": str(pd.Timestamp.now()),
                "source": "sina fund_etf_hist_sina, 拆分校准",
                "rows": df.to_dict(orient="records")}, ensure_ascii=False, indent=1))
print("\n已保存 logs/us_sector_funds_20261018.json")
