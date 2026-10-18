"""大陆QDII美国股指ETF: 拆分校准后年化/回撤对比(新浪全史)。"""
import akshare as ak
import pandas as pd
import numpy as np

CAND = {
    "sh513100": "纳指ETF(国泰)",
    "sz159941": "纳指ETF(广发)",
    "sh513300": "纳斯达克100ETF(华夏)",
    "sz159632": "纳斯达克100ETF(华安)",
    "sh513110": "纳斯达克100ETF(华安/易方达)",
    "sh513870": "纳指100ETF(博时)",
    "sz159509": "纳指科技ETF(景顺长城)",
    "sh513500": "标普500ETF(博时)",
    "sz159612": "标普500ETF(国泰)",
    "sh513650": "标普500ETF(华泰柏瑞)",
    "sz161125": "标普500LOF(易方达)",
}
END = pd.Timestamp("2026-10-16")


def load_adjusted(sym):
    d = ak.fund_etf_hist_sina(symbol=sym)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    c = pd.to_numeric(d.set_index("date")["close"], errors="coerce").dropna()
    c = c[c.index <= END]
    # 份额折算/拆分校准: 单日 <-15% 视为拆分, 前段乘以 post/prev
    r = c.pct_change()
    for dt in r[r < -0.15].index:
        prev = c.shift(1).loc[dt]
        factor = c.loc[dt] / prev
        c.loc[c.index < dt] = c.loc[c.index < dt] * factor
    return c


def ann(sub):
    return (sub.iloc[-1] / sub.iloc[0]) ** (365.25 / max((sub.index[-1] - sub.index[0]).days, 1)) - 1


rows = []
series = {}
for sym, name in CAND.items():
    try:
        c = load_adjusted(sym)
        if len(c) < 30:
            continue
        series[sym] = c
        first, last = c.index[0], c.index[-1]
        rec = {"code": sym, "name": name, "first": str(first.date()),
               "years": round((last - first).days / 365.25, 1),
               "自成立年化": round(float(ann(c)) * 100, 2)}
        for label, days in (("近1年", 365), ("近3年", 365 * 3), ("近5年", 365 * 5)):
            sub = c[c.index >= END - pd.Timedelta(days=days)]
            rec[label] = round(float(ann(sub)) * 100, 2) if len(sub) > 20 and (sub.index[0] - (END - pd.Timedelta(days=days))).days <= 10 else None
        sub5 = c[c.index >= END - pd.Timedelta(days=365 * 5)]
        rec["近5年回撤"] = round(float((sub5 / sub5.cummax() - 1).min()) * 100, 1) if len(sub5) > 20 else None
        rows.append(rec)
    except Exception as e:
        print(f"{sym} ERR {type(e).__name__} {e}")

df = pd.DataFrame(rows)
print("=== 单基金(拆分校准后, %) ===")
print(df.to_string(index=False))

def window(start, title):
    print(f"\n=== {title}: {start.date()} → {END.date()} ===")
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
        out.append((CAND[sym], sym, ann(sub) * 100, mdd * 100, vol * 100))
    out.sort(key=lambda x: -x[2])
    for name, sym, a, m, v in out:
        print(f"{name:<28}{sym}  年化 {a:+7.2f}%  回撤 {m:6.1f}%  波动 {v:5.1f}%")

rows_out = df.to_dict(orient="records")
import json
Path("/root/quant/logs").mkdir(exist_ok=True)
Path("/root/quant/logs/us_etf_compare_20261018.json").write_text(
    json.dumps({"generated_at": str(pd.Timestamp.now()), "source": "sina fund_etf_hist_sina, 拆分校准",
                "note": "人民币计价ETF市价, 不含申赎; 指数对照见报告", "single": rows_out},
               ensure_ascii=False, indent=1))
print("\n已保存 logs/us_etf_compare_20261018.json")
