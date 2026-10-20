"""入场时点检验 9:45/10:30/14:30/14:55（5 分钟线）。

预注册：logs/prereg_entry_timing_5m_20261020.md（判据出数前锁，N=64，执行层）。
数据：data_store/intraday/win5min/*.parquet（QMT 5 分钟，400 只 CSI800，约 1 年）。
用法：python scripts/entry_timing_5m_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

SRC = Path("data_store/intraday/win5min")
ARMS = {"0945": "09:45:00", "1030": "10:30:00",
        "1430": "14:30:00", "1455": "14:55:00"}
TODS = ["09:35:00", "09:45:00", "10:30:00", "14:30:00", "14:55:00", "15:00:00"]
COST = 0.0  # 时点检验不改成本；配对差同成本


def band_of(code):
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("688", "300", "301")):
        return 0.20
    return 0.10


def load_one(path):
    d = pd.read_parquet(path).reset_index(drop=True)
    x = d.copy()
    if "time" in d.columns:
        num = pd.to_numeric(d["time"], errors="coerce")
        if num.notna().mean() > 0.9 and num.max() > 1e11:
            # QMT tick/5m time 列=epoch 毫秒(UTC)，转北京时间
            x["dt"] = (pd.to_datetime(num, unit="ms", utc=True)
                       .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
        else:
            x["dt"] = pd.to_datetime(d["time"].astype(str),
                                     format="%Y%m%d%H%M%S", errors="coerce")
    else:
        raw = pd.Series(pd.Index(d.index).astype(str))
        x["dt"] = pd.to_datetime(raw, format="%Y%m%d%H%M%S", errors="coerce")
    x = x[x["dt"].notna()].copy()
    x["date"] = x["dt"].dt.normalize()
    x["tod"] = x["dt"].dt.strftime("%H:%M:%S")
    x = x[(x["tod"] >= "09:30:00") & (x["tod"] <= "15:00:00")]
    for c in ("open", "high", "low", "close", "volume", "amount"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def per_stock(code, x):
    # 只保留需要的 bar，按 tod 透视
    x = x[x["tod"].isin(TODS)]
    if x.empty:
        return None
    pc = x.pivot_table(index="date", columns="tod", values="close", aggfunc="last")
    pl = x.pivot_table(index="date", columns="tod", values="low", aggfunc="last")
    pv = x.pivot_table(index="date", columns="tod", values="volume", aggfunc="last")
    po = x.pivot_table(index="date", columns="tod", values="open", aggfunc="last")
    pa = x.pivot_table(index="date", columns="tod", values="amount", aggfunc="sum")
    out = pd.DataFrame(index=pc.index)
    out["code"] = code
    for a in ARMS:
        out["p" + a] = pc.get(ARMS[a])
        out["v" + a] = pv.get(ARMS[a])
    out["close"] = pc.get("15:00:00")
    out["open935"] = po.get("09:35:00")
    out["amount"] = pa.sum(axis=1)
    # 入场后当日最低价（严格晚于入场 bar）
    for a, tod in ARMS.items():
        after = [t for t in TODS if t > tod and t in pl.columns]
        out["low_after_" + a] = pl[after].min(axis=1) if after else np.nan
        out["mae_" + a] = out["low_after_" + a] / out["p" + a] - 1
    out = out.reset_index().rename(columns={"index": "date"})
    return out


def main():
    files = sorted(SRC.glob("*.parquet"))
    print(f"files {len(files)}")
    frames = []
    for i, p in enumerate(files, 1):
        try:
            x = load_one(p)
            s = per_stock(p.stem, x)
            if s is not None and len(s):
                frames.append(s)
        except Exception as e:
            print("ERR", p.name, str(e)[:120])
        if i % 50 == 0:
            print("loaded", i, "stocks", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"stock-days {len(df)}")

    # 前收/涨停、次日 open、1 日/20 日出场
    g = df.groupby("code", sort=False)
    df["prev_close"] = g["close"].shift(1)
    df["band"] = df["code"].map(band_of)
    df["limit_up"] = (df["prev_close"] * (1 + df["band"])).round(2)
    df["next_close"] = g["close"].shift(-1)
    df["next_open"] = g["open935"].shift(-1)
    df["exit20"] = g["close"].shift(-20)

    # 有效：四个时点价格/量齐全、入场 bar 有量、非涨停、出场存在
    valid = pd.Series(True, index=df.index)
    for a in ARMS:
        valid &= df["p" + a].notna() & (df["p" + a] > 0) & df["v" + a].fillna(0).gt(0)
    valid &= df["close"].notna()
    valid &= ~(df["limit_up"].notna() & (df["p1430"] >= df["limit_up"] - 1e-6))
    valid &= ~(df["limit_up"].notna() & (df["p0945"] >= df["limit_up"] - 1e-6))
    counts = {"raw": int(len(df)), "valid": int(valid.sum())}
    df = df[valid].copy()

    # 20 日均额排名（当日截面）
    df["amount20"] = df.groupby("code")["amount"].transform(
        lambda s: s.rolling(20, min_periods=15).mean())
    df["amt_rank"] = df.groupby("date")["amount20"].rank(ascending=False, method="min")

    # 收益
    for a in ARMS:
        df["r1_" + a] = df["close"] / df["p" + a] - 1
        df["r20_" + a] = df["exit20"] / df["p" + a] - 1
    df["gap_next"] = df["next_open"] / df["close"] - 1
    df["diff20_1430_0945"] = df["r20_1430"] - df["r20_0945"]
    df["diff20_1455_1430"] = df["r20_1455"] - df["r20_1430"]
    df["diff20_1030_0945"] = df["r20_1030"] - df["r20_0945"]
    df["diff1_1430_0945"] = df["r1_1430"] - df["r1_0945"]
    df["mae_diff_1430_0945"] = df["mae_1430"] - df["mae_0945"]

    def tstat(x):
        x = x.dropna()
        if len(x) < 2 or x.std() == 0:
            return None
        return float(x.mean() / (x.std() / np.sqrt(len(x))))

    def daily_t(col, sub=None):
        d = (sub if sub is not None else df)[["date", col]].dropna()
        dm = d.groupby("date")[col].mean()
        return tstat(dm), float(dm.mean() * 100) if len(dm) else None, int(len(dm))

    def describe(col, sub=None):
        s = (sub if sub is not None else df)[col].dropna()
        return {"n": int(len(s)), "mean_pct": float(s.mean() * 100),
                "median_pct": float(s.median() * 100),
                "win_pct": float((s > 0).mean() * 100),
                "sd_pct": float(s.std() * 100)}

    pool = df[df["amt_rank"] <= 30]
    oos = df[df["date"] >= "2026-07-01"]
    out = {"prereg": "logs/prereg_entry_timing_5m_20261020.md",
           "counts": counts,
           "n_stockdays": int(len(df)), "n_days": int(df["date"].nunique()),
           "n_codes": int(df["code"].nunique()),
           "arm_returns": {}, "paired": {}, "risk": {}}

    for a in ARMS:
        out["arm_returns"][a] = {
            "r1": describe("r1_" + a), "r20": describe("r20_" + a),
            "t20_day": daily_t("r20_" + a)[0],
            "mae_median_pct": float(df["mae_" + a].median() * 100)}

    for name, col in (("1430_vs_0945", "diff20_1430_0945"),
                      ("1455_vs_1430", "diff20_1455_1430"),
                      ("1030_vs_0945", "diff20_1030_0945"),
                      ("1430_vs_0945_1d", "diff1_1430_0945")):
        t_all, m_all, nd = daily_t(col)
        t_pool, m_pool, nd_pool = daily_t(col, pool)
        t_oos, m_oos, nd_oos = daily_t(col, oos)
        out["paired"][name] = {
            "all": {**describe(col), "t_day": t_all, "daily_mean_pct": m_all,
                    "n_days": nd},
            "pool30": {**describe(col, pool), "t_day": t_pool,
                       "daily_mean_pct": m_pool, "n_days": nd_pool},
            "oos": {**describe(col, oos), "t_day": t_oos,
                    "daily_mean_pct": m_oos, "n_days": nd_oos}}

    out["risk"] = {
        "mae_median_0945_pct": float(df["mae_0945"].median() * 100),
        "mae_median_1430_pct": float(df["mae_1430"].median() * 100),
        "mae_median_diff_pct": float(df["mae_diff_1430_0945"].median() * 100),
        "gap_next_mean_pct": float(df["gap_next"].mean() * 100),
    }

    p = out["paired"]["1430_vs_0945"]["all"]
    o = out["paired"]["1430_vs_0945"]["oos"]
    crit = {
        "1_mean_t": bool(p["mean_pct"] > 0 and p["t_day"] is not None
                         and p["t_day"] >= 2.5),
        "2_oos": bool(o["mean_pct"] > 0 and o["t_day"] is not None
                      and o["t_day"] >= 1.5),
        "3_econ": bool(p["mean_pct"] >= 0.10),
        "4_risk": bool(out["risk"]["mae_median_diff_pct"] >= -0.20),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"

    df.to_parquet("data_store/intraday/entry_timing_5m_panel.parquet", index=False)
    df.groupby("date")[["diff20_1430_0945", "diff20_1455_1430",
                        "diff20_1030_0945", "diff1_1430_0945"]].mean().to_csv(
        "logs/entry_timing_5m_daily.csv")
    with open("logs/entry_timing_5m_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:4000])


if __name__ == "__main__":
    main()
