"""离岸窗口因子检验(协议=三十轮登记, 判据出数前锁)。

信号=海外中国 ETF(FXI/KWEB/ASHR)美盘隔夜收益; 增量残差=ETF 收益−β̂×SPY
(滚动252日β̂, 截至T-1无前视)。对象=上证 T+1 开盘/收盘/盘中(拆gap)。
判定=增量价值跨过既有"美股三指数"因子才算新因子。产出:
logs/offshore_china_factor_test.json
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/quant")
from data.storage import load_meta  # noqa

OUT = Path("/root/quant/logs/offshore_china_factor_test.json")


def tstat(r, n):
    if n <= 3 or abs(r) >= 1:
        return np.nan
    return r * np.sqrt((n - 2) / (1 - r * r))


def block(name, d, x, y):
    dd = d[[x, y]].dropna()
    n = len(dd)
    if n < 60:
        return {"name": name, "n": int(n), "note": "样本<60 不判读"}
    r = np.corrcoef(dd[x], dd[y])[0, 1]
    return {"name": name, "n": int(n), "corr": round(float(r), 4),
            "t": round(float(tstat(r, n)), 2)}


def main():
    import akshare as ak
    sh = ak.stock_zh_index_daily(symbol="sh000001")
    sh["date"] = pd.to_datetime(sh["date"])
    cal = load_meta("trade_calendar")
    cal_dates = set(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d"))
    sh = sh[sh["date"].dt.strftime("%Y-%m-%d").isin(cal_dates)].sort_values("date")

    etfs = {}
    for sym in ["FXI", "KWEB", "ASHR", "SPY"]:
        d = ak.stock_us_daily(symbol=sym, adjust="")
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date").reset_index(drop=True)
        d["ret"] = d["close"].pct_change()
        s = d.set_index("date")["ret"]
        s.index = pd.to_datetime(s.index, errors="coerce")
        etfs[sym] = s[~s.index.isna()]

    # 对齐: A股交易日 T 的隔夜收益 = 美盘 T 日的收益(美盘在A股T日15:00后开)
    rows = []
    for _, r in sh.iterrows():
        t = r["date"]
        row = {"t": t, "open": r["open"], "close": r["close"]}
        for sym, s in etfs.items():
            sub = s[s.index == t]
            row[sym] = float(sub.iloc[0]) if len(sub) else np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    # β̂ 在美盘稠密日序列上滚动252日估计(避免A股日稀疏合并导致的NaN传播), 截至 T-1
    us_dates = sorted({pd.Timestamp(x) for s in etfs.values() for x in s.index})
    usf = pd.DataFrame(index=us_dates)
    for sym, s in etfs.items():
        usf[sym] = s.reindex(us_dates)
    for sym in ["FXI", "KWEB", "ASHR"]:
        beta_us = usf[sym].rolling(252).corr(usf["SPY"]) * \
            usf[sym].rolling(252).std() / usf["SPY"].rolling(252).std()
        beta_lag = beta_us.shift(1)
        beta_map = beta_lag.reindex(df["t"]).ffill()
        # to_numpy: beta_map 按 df 行序对齐(其索引=df.t 的 datetime, 与 RangeIndex 相乘会全 NaN)
        df[f"{sym}_resid"] = df[sym] - beta_map.to_numpy() * df["SPY"]
    df["sh_ret"] = df["close"].shift(-1) / df["close"] - 1
    df["sh_gap"] = df["open"].shift(-1) / df["close"] - 1
    df["sh_intra"] = df["close"].shift(-1) / df["open"].shift(-1) - 1

    out = {"blocks": []}
    for sym in ["FXI", "KWEB", "ASHR"]:
        for x, xl in [(sym, f"{sym}原始"), (f"{sym}_resid", f"{sym}增量残差")]:
            for y, yl in [("sh_ret", "T+1收盘"), ("sh_gap", "T+1跳空"), ("sh_intra", "T+1盘中")]:
                out["blocks"].append(block(f"{xl} → {yl}", df, x, y))
    # 嵌套增量: 回归 sh_gap ~ SPY + resid (F 检验风格: resid 的偏 t)
    import statsmodels.api as sm
    for sym in ["FXI", "KWEB", "ASHR"]:
        d = df[["sh_gap", "SPY", f"{sym}_resid"]].dropna()
        if len(d) < 60:
            continue
        X = sm.add_constant(d[["SPY", f"{sym}_resid"]])
        m = sm.OLS(d["sh_gap"], X).fit()
        out["blocks"].append({
            "name": f"嵌套: T+1跳空 ~ SPY + {sym}残差",
            "n": int(len(d)),
            "spy_t": round(float(m.tvalues["SPY"]), 2),
            "resid_t": round(float(m.tvalues[f"{sym}_resid"]), 2),
            "resid_coef": round(float(m.params[f"{sym}_resid"]), 5),
        })
    # 极端分位
    for sym in ["FXI", "KWEB", "ASHR"]:
        d = df.dropna(subset=[sym, "sh_ret"])
        if len(d) < 200:
            continue
        q90, q10 = d[sym].quantile(0.9), d[sym].quantile(0.1)
        top = d[d[sym] >= q90]["sh_ret"]
        bot = d[d[sym] <= q10]["sh_ret"]
        out["blocks"].append({
            "name": f"{sym} TOP10%−BOT10% T+1收盘差",
            "n_top": int(len(top)), "n_bot": int(len(bot)),
            "mean_top_pct": round(float(top.mean()) * 100, 3),
            "mean_bot_pct": round(float(bot.mean()) * 100, 3),
            "spread_pct": round(float(top.mean() - bot.mean()) * 100, 3),
        })
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for b in out["blocks"]:
        print(json.dumps(b, ensure_ascii=False))
    print("已保存", OUT)


if __name__ == "__main__":
    main()
