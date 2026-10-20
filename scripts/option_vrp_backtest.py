"""卫星候选2：指数期权定义风险 Put 价差（VRP）回测（2026-10-20）。

预注册: logs/prereg_option_vrp_20261020.md (出数前锁; 风险预算 20% 修正)
- 月度: 到期=第三个星期五; 入场=上一到期后首个交易日收盘; 出场=到期日收盘(结算近似)。
- 卖出 5% OTM Put, 买入 10% OTM Put (同月); 定义风险。
- 卫星 20 万; 月风险预算 20%(4万); 张数=floor(4万/最大损失), 1~3 张。
- 成本: 15元/张/腿 + 1 tick(0.2点×100) 滑点/腿。
用法: python scripts/option_vrp_backtest.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import akshare as ak

START, END = "2019-12-01", "2026-10-19"
SATELLITE = 200_000.0
RISK_BUDGET = 0.20 * SATELLITE   # 4万
MAX_LOTS = 3
FEE_LEG = 15.0
SLIP_PTS = 0.2


def third_friday(y, m):
    fr = []
    for day in range(1, 32):
        try:
            x = pd.Timestamp(year=y, month=m, day=day)
        except ValueError:
            break
        if x.weekday() == 4:
            fr.append(x)
    return fr[2]


def load_csi300():
    d = ak.stock_zh_index_daily(symbol="sh000300")
    d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float).sort_index()
    return s[(s.index >= START) & (s.index <= END)]


_CACHE = {}


def opt_px(sym, dt):
    if sym not in _CACHE:
        try:
            d = ak.option_cffex_hs300_daily_sina(symbol=sym)
            d["date"] = pd.to_datetime(d["date"])
            _CACHE[sym] = d.sort_values("date")
        except Exception:
            _CACHE[sym] = None
    d = _CACHE[sym]
    if d is None or d.empty:
        return None
    d = d[d["date"] <= dt]
    if d.empty or (dt - d["date"].iloc[-1]).days > 5:
        return None
    return float(d["close"].iloc[-1])


def main():
    s = load_csi300()
    cal = sorted(s.index.tolist())
    ma200 = s.rolling(200).mean()
    print(f"CSI300 {s.index[0].date()}~{s.index[-1].date()} n={len(s)}")

    months = pd.period_range("2019-12", "2026-10", freq="M")
    rows = []
    counts = {"months": 0, "no_k1": 0, "no_k2": 0, "no_prev": 0, "used": 0}
    for mp in months:
        y, m = mp.year, mp.month
        exp = third_friday(y, m)
        # 到期日: 若 not trading, 取之后首个交易日
        exp_td = next((d for d in cal if d >= exp), None) if exp <= cal[-1] else None
        if exp_td is None:
            continue
        # 上一月到期
        pm = mp - 1
        pexp = third_friday(pm.year, pm.month)
        ptd = next((d for d in cal if d >= pexp), None)
        if ptd is None:
            counts["no_prev"] += 1; continue
        entry = next((d for d in cal if d > ptd), None)
        if entry is None or entry >= exp_td:
            counts["no_prev"] += 1; continue
        counts["months"] += 1
        S = float(s.loc[entry])
        prefix = f"io{y % 100:02d}{m:02d}"
        k1 = int(round(S * 0.95 / 50) * 50)
        k2 = int(round(S * 0.90 / 50) * 50)
        p1 = None; k1u = None
        for dk in (0, 50, -50, 100, -100, 150, -150):
            p1 = opt_px(f"{prefix}P{k1 + dk}", entry)
            if p1 is not None:
                k1u = k1 + dk; break
        if p1 is None:
            counts["no_k1"] += 1; continue
        p2 = None; k2u = None
        # 优先更虚值(=更低行权价, 更低最大损失); 未挂牌再往浅找
        for dk in (0, -50, -100, -150, -200, 50, 100, 150):
            p2 = opt_px(f"{prefix}P{k2 + dk}", entry)
            if p2 is not None:
                k2u = k2 + dk; break
        if p2 is None or k2u >= k1u:
            counts["no_k2"] += 1; continue
        # 成本: 两腿手续费+滑点(点)
        cost = 2 * (FEE_LEG + SLIP_PTS * 100)
        net_prem_pts = p1 - p2
        width = k1u - k2u
        max_loss = (width - net_prem_pts) * 100 + cost
        if max_loss <= 0:
            max_loss = 1.0
        if max_loss > RISK_BUDGET:
            counts["too_risky"] = counts.get("too_risky", 0) + 1
            continue
        lots = int(min(MAX_LOTS, max(1, RISK_BUDGET // max_loss)))
        S_exp = float(s.loc[exp_td])
        payoff_pts = max(k1u - S_exp, 0) - max(k2u - S_exp, 0)
        pnl_per = (net_prem_pts - payoff_pts) * 100 - cost
        pnl = pnl_per * lots
        rows.append({"expiry": exp_td.strftime("%Y-%m-%d"),
                     "entry": entry.strftime("%Y-%m-%d"),
                     "contract": prefix, "S_entry": S, "S_expiry": S_exp,
                     "K1": k1u, "K2": k2u, "P1": p1, "P2": p2,
                     "net_prem_pts": net_prem_pts, "max_loss": max_loss,
                     "lots": lots, "payoff_pts": payoff_pts,
                     "pnl": pnl, "sat_ret_pct": pnl / SATELLITE * 100,
                     "ma200_ok": bool(S > float(ma200.loc[entry])) if ma200.loc[entry] == ma200.loc[entry] else None})
        counts["used"] += 1
    df = pd.DataFrame(rows)
    print("counts", counts)
    if df.empty:
        print("无有效交易"); return
    df["year"] = pd.to_datetime(df["expiry"]).dt.year
    df["seg"] = np.where(df["year"] <= 2022, "IS", "OOS")
    nav = SATELLITE + df["pnl"].cumsum()
    dd = (nav / nav.cummax() - 1).min()
    ret = df["pnl"] / SATELLITE
    n = len(df)
    ann = (nav.iloc[-1] / SATELLITE) ** (12 / n) - 1 if nav.iloc[-1] > 0 else -1
    t = ret.mean() / (ret.std() / np.sqrt(n)) if n > 1 and ret.std() > 0 else None

    def segstats(sub):
        if len(sub) < 2:
            return {"n": int(len(sub)), "mean_pct": float(sub["sat_ret_pct"].mean()) if len(sub) else None,
                    "t": None}
        r = sub["pnl"] / SATELLITE
        return {"n": int(len(sub)), "mean_pct": float(r.mean() * 100),
                "t": float(r.mean() / (r.std() / np.sqrt(len(r)))) if r.std() > 0 else None,
                "win_pct": float((sub["pnl"] > 0).mean() * 100)}

    out = {"prereg": "logs/prereg_option_vrp_20261020.md",
           "window": [df["expiry"].min(), df["expiry"].max()],
           "counts": counts, "n": int(n), "lots_mean": float(df["lots"].mean()),
           "ann_pct": float(ann * 100), "t_monthly": t,
           "max_dd_pct": float(dd * 100),
           "worst_month_pct": float(ret.min() * 100),
           "win_pct": float((df["pnl"] > 0).mean() * 100),
           "mean_month_pct": float(ret.mean() * 100),
           "all": segstats(df), "IS": segstats(df[df.seg == "IS"]),
           "OOS": segstats(df[df.seg == "OOS"]),
           "bull": segstats(df[df.ma200_ok == True]), "bear": segstats(df[df.ma200_ok == False]),
           "trades": df.to_dict("records")}
    # 判据
    oos = out["OOS"]
    crit = {
        "1_mean_t": bool(ret.mean() > 0 and t is not None and t >= 2.0),
        "2_oos": bool(oos.get("mean_pct", -99) > 0 and oos.get("t") is not None and oos["t"] >= 1.0),
        "3_worst_month": bool(out["worst_month_pct"] >= -20.0),
        "4_maxdd": bool(out["max_dd_pct"] >= -50.0),
        "5_cost_50": None,  # 成本敏感性在下方单独算
    }
    # 成本+50%
    cost2 = 1.5 * (2 * (FEE_LEG + SLIP_PTS * 100))
    df2 = df.copy()
    df2["pnl50"] = df2["pnl"] - (cost2 - (2 * (FEE_LEG + SLIP_PTS * 100))) * df2["lots"]
    nav2 = SATELLITE + df2["pnl50"].cumsum()
    ann2 = (nav2.iloc[-1] / SATELLITE) ** (12 / n) - 1 if nav2.iloc[-1] > 0 else -1
    crit["5_cost_50"] = bool(ann2 > 0)
    out["ann_cost50_pct"] = float(ann2 * 100)
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(v is True for v in crit.values()) else "FAIL"
    print(json.dumps({k: v for k, v in out.items() if k != "trades"}, ensure_ascii=False, indent=1, default=str))
    with open("/root/quant/logs/option_vrp_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    df.to_csv("/root/quant/logs/option_vrp_20261020.csv", index=False)


if __name__ == "__main__":
    main()
