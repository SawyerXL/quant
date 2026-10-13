"""北向因子检验(协议=二十八轮登记, 判据出数前锁)。

X1=成交总额(官方口径全程) / X2=净买额(仅改革前真实口径段)。
对象=次日上证收益, shift(1), 拆 gap/intraday, 三窗(全窗/改革前/改革后),
极端分位(TOP/BOT 10%)与全样本双报。判定=跨过"美股三指数+铜+涨停情绪"
有效门槛才算新因子, 小样本不写铁律。产出: logs/northbound_factor_test.json
"""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/quant")
from data.storage import load_meta  # noqa

REFORM = "2024-08-18"  # 披露改革日: 之后净买额=东财估算, 官方只留成交总额
OUT = Path("/root/quant/logs/northbound_factor_test.json")


def tstat(r, n):
    if n <= 3 or abs(r) >= 1:
        return np.nan
    return r * np.sqrt((n - 2) / (1 - r * r))


def block(name, df, x, y):
    d = df[[x, y]].dropna()
    n = len(d)
    if n < 30:
        return {"name": name, "n": n, "note": "样本<30 不判读"}
    r = np.corrcoef(d[x], d[y])[0, 1]
    return {"name": name, "n": int(n), "corr": round(float(r), 4),
            "t": round(float(tstat(r, n)), 2)}


def main():
    import akshare as ak
    h = ak.stock_hsgt_hist_em(symbol="北向资金")
    h["日期"] = pd.to_datetime(h["日期"])
    for c in ["当日成交净买额", "买入成交额", "卖出成交额"]:
        h[c] = pd.to_numeric(h[c], errors="coerce")
    h["成交总额"] = h["买入成交额"] + h["卖出成交额"]
    # 交叉验证: 改革后 EM 成交总额 vs MCP 官方(10-13 north_money=261,883百万=2,618.8亿)
    last = h.iloc[-1]
    xcheck = {"em_total_yi": round(float(last["成交总额"]), 1),
              "mcp_official_yi": 2618.8, "date": str(last["日期"].date())}

    # 上证日线(开/收) 2014+
    idx = ak.stock_zh_index_daily(symbol="sh000001")
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").reset_index(drop=True)

    df = h.merge(idx, left_on="日期", right_on="date", how="inner")
    df = df.sort_values("日期").reset_index(drop=True)
    df["x1"] = df["成交总额"].pct_change()
    df["x2_ratio"] = df["当日成交净买额"] / df["成交总额"]
    df["x2_raw"] = df["当日成交净买额"]
    df["ret_close"] = df["close"].shift(-1) / df["close"] - 1
    df["ret_gap"] = df["open"].shift(-1) / df["close"] - 1
    df["ret_intra"] = df["close"].shift(-1) / df["open"].shift(-1) - 1
    # 分位
    for x in ["x1", "x2_ratio"]:
        df[f"{x}_top"] = (df[x] >= df[x].quantile(0.9)).astype(float)
        df[f"{x}_bot"] = (df[x] <= df[x].quantile(0.1)).astype(float)

    wins = {
        "全窗 2014-2026": df,
        "改革前 ~2024-08-17": df[df["日期"] < REFORM],
        "改革后 2024-08-18~": df[df["日期"] >= REFORM],
    }
    out = {"xcheck": xcheck, "blocks": []}
    for wn, w in wins.items():
        for x, label in [("x1", "成交总额环比"), ("x2_ratio", "净买额/成交总额")]:
            for y, yl in [("ret_close", "T+1收盘"), ("ret_gap", "T+1跳空"), ("ret_intra", "T+1盘中")]:
                out["blocks"].append(block(f"{wn} | {label} → {yl}", w, x, y))
    # 极端分位: TOP10% 次日 vs BOT10% 次日 (方向差)
    for wn, w in wins.items():
        for x, label in [("x1", "成交总额"), ("x2_ratio", "净买额占比")]:
            top = w[w[f"{x}_top"] == 1]["ret_close"]
            bot = w[w[f"{x}_bot"] == 1]["ret_close"]
            if len(top) >= 10 and len(bot) >= 10:
                out["blocks"].append({
                    "name": f"{wn} | {label} TOP10%−BOT10% 次日收盘差",
                    "n_top": int(len(top)), "n_bot": int(len(bot)),
                    "mean_top_pct": round(float(top.mean()) * 100, 3),
                    "mean_bot_pct": round(float(bot.mean()) * 100, 3),
                    "spread_pct": round(float(top.mean() - bot.mean()) * 100, 3),
                    "t": round(float(
                        (top.mean() - bot.mean())
                        / np.sqrt(top.var() / len(top) + bot.var() / len(bot))), 2),
                })
    import json
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for b in out["blocks"]:
        print(json.dumps(b, ensure_ascii=False))
    print("已保存", OUT)


if __name__ == "__main__":
    main()
