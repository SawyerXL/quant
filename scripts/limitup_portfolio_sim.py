"""R1b 组合层模拟 + 容量/冲击成本(2026-10-19, 判据出数前锁).

信号: A 臂=T日炸板(触板未封), 剔ST/上市<60日/非正价。
执行: T+1 开盘买(排除一字板); T+1 收盘卖; T+1 跌停封死→T+2 开盘卖。
组合: 每个入场日等权当日全部事件, 单票上限 5% 资金; 未用资金不计息。
成本: 0.23%(双边0.13%+滑点0.1%) + 冲击成本(预注册模型:
      impact = 0.10% × sqrt(参与率/1%), 参与率=仓位额/ADV20)。
资金档: 100万(项目) / 500万 / 2000万。
判据(出数前锁):
  1. 组合日均超额 >0 且日聚类 t ≥ 2.5; OOS(2023-26) 同向且 t ≥ 2.5。
  2. 100万档冲击成本 ≤ 0.02%/事件。
  3. 模拟 NAV 最大回撤 ≤ 15%(账户熔断线)。
  4. 容量: 报告各资金档日均超额(含冲击)仍 >0 的最大资金档。
注: 顺延出场(跌停)样本的完整收益计入入场日(时序近似, 单独报数量)。
用法: python scripts/limitup_portfolio_sim.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"
COST = 0.0023
CAP_CAPITAL = (1_000_000, 5_000_000, 20_000_000)
CAP_5PCT = 0.05


def band_of(code):
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def main():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st_map = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    list_map = dict(zip(meta["code"], meta.get("list_date", pd.Series("", index=meta.index)).astype(str).str[:10]))
    codes = sorted(c for c in meta["code"] if not st_map.get(c, False))

    frames = []
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date")
            close = pd.to_numeric(d["close"], errors="coerce")
            opn = pd.to_numeric(d["open"], errors="coerce")
            high = pd.to_numeric(d["high"], errors="coerce")
            amt = pd.to_numeric(d.get("amount", pd.Series(np.nan, index=d.index)), errors="coerce")
            prev = close.shift(1)
            band = band_of(code)
            lim_up = (prev * (1 + band)).round(2)
            lim_dn = (prev * (1 - band)).round(2)
            frames.append(pd.DataFrame({
                "date": d["date"].values, "code": code,
                "open": opn.values, "close": close.values, "prev": prev.values,
                "amt": amt.values,
                "touch": ((high >= lim_up - 1e-6) & prev.notna()).values,
                "seal": ((close >= lim_up - 1e-6) & prev.notna()).values,
                "seal_dn": ((close <= lim_dn + 1e-6) & prev.notna()).values,
            }))
        except Exception:
            continue

    df = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    df["code"] = df["code"].astype("category")
    df = df[(df["open"] > 0) & (df["close"] > 0) & (df["prev"] > 0)]
    ld = df["code"].astype(str).map(list_map)
    age = (pd.to_datetime(df["date"]) - pd.to_datetime(ld, errors="coerce")).dt.days
    df = df[age.fillna(9999) >= 60]

    g = df.groupby("code", sort=False)
    df["adv20"] = g["amt"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    df["open_next"] = g["open"].shift(-1)
    df["open_next2"] = g["open"].shift(-2)
    df["close_next"] = g["close"].shift(-1)
    df["seal_dn_next"] = g["seal_dn"].shift(-1)
    df["lim_up_next"] = (df["close"] * (1 + df["code"].astype(str).map(band_of))).round(2)
    df["date_next"] = pd.to_datetime(g["date"].shift(-1)).dt.strftime("%Y-%m-%d")
    df["intra"] = df["close"] / df["open"] - 1
    b_intra = df.groupby("date", observed=True)["intra"].mean()
    b_intra.index = pd.to_datetime(b_intra.index).strftime("%Y-%m-%d")

    ev = df[(df["touch"]) & (~df["seal"]) & df["open_next"].notna()].copy()
    ev = ev[ev["open_next"] < ev["lim_up_next"] - 1e-6]        # 排除一字板
    delay = ev["seal_dn_next"].fillna(False).astype(bool)
    ev = ev[~delay | ev["open_next2"].notna()]
    ev["delayed"] = delay[ev.index]
    ev["stock_ret"] = np.where(
        ev["delayed"], ev["open_next2"] / ev["open_next"] - 1,
        ev["close_next"] / ev["open_next"] - 1)
    ev["mdate"] = ev["date_next"]
    ev = ev.merge(b_intra.rename("b_intra"), left_on="mdate", right_index=True, how="left")
    ev = ev[ev["b_intra"].notna()]
    ev["year"] = pd.to_datetime(ev["date"]).dt.year
    ev["seg"] = np.where(ev["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
    print(f"事件 {len(ev):,} | 入场日 {ev['mdate'].nunique():,} | 顺延 {int(ev['delayed'].sum()):,}")

    out = {}
    for cap in CAP_CAPITAL:
        rows = []
        for mdate, day in ev.groupby("mdate"):
            n = len(day)
            w = min(1.0 / n, CAP_5PCT)
            # 冲击: 参与率 = 仓位额/ADV20。**单位**: 本地 amount=万元(库契约),
            # 故先把仓位额(元)换成万元再比; 2026-10-19 首跑实锤单位错导致
            # 参与率虚高 1e4 倍(783%)、冲击成本假 1.47%/笔。
            pos_amt_wan = cap * w / 1e4
            part = pos_amt_wan / day["adv20"].replace(0, np.nan)
            impact = 0.001 * np.sqrt(part / 0.01)
            impact = impact.clip(upper=0.02).fillna(0.02)   # 缺ADV按最差2%
            stock_ret_net = day["stock_ret"] - COST - impact.values
            port_ret = float((w * stock_ret_net).sum())
            bench_ret = float(day["b_intra"].mean()) * (w * n)   # 基准乘部署比例
            rows.append({"mdate": mdate, "ret": port_ret, "excess": port_ret - bench_ret,
                         "n": n, "deployed": w * n, "impact_mean": float(impact.mean()),
                         "part_mean": float(part.mean()) if part.notna().any() else None})
        r = pd.DataFrame(rows).set_index("mdate")
        r.index = pd.to_datetime(r.index)
        r = r.sort_index()
        nav = (1 + r["ret"]).cumprod()
        ann = nav.iloc[-1] ** (252 / len(r)) - 1
        shp = r["ret"].mean() / r["ret"].std() * np.sqrt(252) if r["ret"].std() else None
        dd = (nav / nav.cummax() - 1).min()
        t_all = r["excess"].mean() / (r["excess"].std() / np.sqrt(len(r)))
        seg = {}
        for s, ss in r.groupby(np.where(r.index.year <= 2022, "IS_2019_2022", "OOS_2023_2026")):
            seg[s] = {"mean_excess_pct": ss["excess"].mean() * 100,
                      "t": ss["excess"].mean() / (ss["excess"].std() / np.sqrt(len(ss)))
                      if len(ss) > 1 else None}
        out[f"cap_{cap}"] = {
            "days": len(r), "ann_pct": ann * 100, "sharpe": shp, "max_dd_pct": dd * 100,
            "mean_daily_ret_pct": r["ret"].mean() * 100,
            "mean_daily_excess_pct": r["excess"].mean() * 100, "t_excess": t_all,
            "avg_events": float(r["n"].mean()), "avg_deployed_pct": float(r["deployed"].mean() * 100),
            "avg_impact_cost_pct": float(r["impact_mean"].mean() * 100),
            "avg_participation_pct": float(r["part_mean"].dropna().mean() * 100)
            if r["part_mean"].notna().any() else None,
            "segments": seg,
        }

    print(pd.DataFrame(out).T.to_string())
    import json
    Path("logs/limitup_portfolio_sim.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已保存 logs/limitup_portfolio_sim.json")


if __name__ == "__main__":
    main()
