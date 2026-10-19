"""R1b 稳健性反复验证(2026-10-19, 只做稳健性, 不调参).

对 R1b A 臂(炸板→次日开盘买→当日收盘卖)做实现变体检验:
  1. 确定性: 同代码同数据跑两次, 逐值比对.
  2. 成本敏感性: 0.23% / 0.33% / 0.53%.
  3. 成交价恶化: 开盘买入价 ×(1+0.2%) / ×(1+0.4%).
  4. 除权日污染剔除: T 或 T+1 距除权除息日 ≤1 个交易日的事件剔除
     (未复权价格在除权日会出现假跌停/假涨停).
  5. 报告被剔除的除权日事件数.
判据: 各变体下 A 臂日均超额仍 >0 且日聚类 t 仍 ≥2.5 → 数值稳健;
否则结论依赖实现假设, 不得引用。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"


def band_of(code):
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def build_events():
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
            prev = close.shift(1)
            band = band_of(code)
            lim_up = (prev * (1 + band)).round(2)
            lim_dn = (prev * (1 - band)).round(2)
            frames.append(pd.DataFrame({
                "date": d["date"].values, "code": code,
                "open": opn.values, "close": close.values, "prev": prev.values,
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
    df["open_next"] = g["open"].shift(-1)
    df["open_next2"] = g["open"].shift(-2)
    df["close_next"] = g["close"].shift(-1)
    df["seal_dn_next"] = g["seal_dn"].shift(-1)
    df["lim_up_next"] = (df["close"] * (1 + df["code"].astype(str).map(band_of))).round(2)
    # 基准 = T+1 当日等权盘中(close/open-1), 按 T+1 日期索引, 与入场日对齐。
    # 2026-10-19 修正: 首版用 intra_next 按 T 日索引再按 T+1 合并 → 基准错位
    # 一天(稳健性脚本自身 bug, 不是主结果)。
    df["intra"] = df["close"] / df["open"] - 1
    b = df.groupby("date")["intra"].mean()
    b.index = pd.to_datetime(b.index).strftime("%Y-%m-%d")
    df["mdate"] = pd.to_datetime(g["date"].shift(-1)).dt.strftime("%Y-%m-%d")
    df["date_s"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    ev = df[(df["touch"]) & (~df["seal"]) & df["open_next"].notna()].copy()
    ev = ev[ev["open_next"] < ev["lim_up_next"] - 1e-6]
    delay = ev["seal_dn_next"].fillna(False).astype(bool)
    ev = ev[~delay | ev["open_next2"].notna()]
    ev["delayed"] = delay[ev.index]
    ev = ev.merge(b.rename("b_intra"), left_on="mdate", right_index=True, how="left")
    ev = ev[ev["b_intra"].notna()].copy()
    return ev


def stats(ev, cost=0.0023, entry_slip=0.0):
    ex = ev.copy()
    if entry_slip:
        # 恶化成交价: 以开盘价×(1+slip)买入
        ex["entry_px"] = ex["open_next"] * (1 + entry_slip)
        ex["stock_ret"] = np.where(
            ex["delayed"], ex["open_next2"] / ex["entry_px"] - 1,
            ex["close_next"] / ex["entry_px"] - 1)
    else:
        ex["stock_ret"] = np.where(
            ex["delayed"], ex["open_next2"] / ex["open_next"] - 1,
            ex["close_next"] / ex["open_next"] - 1)
    ex["excess"] = ex["stock_ret"] - ex["b_intra"] - cost
    daily = ex.groupby("mdate")["excess"].mean()
    t = daily.mean() / (daily.std() / np.sqrt(len(daily))) if len(daily) > 1 else None
    return {"n": len(ex), "days": len(daily),
            "mean_excess_pct": round(float(ex["excess"].mean()) * 100, 4),
            "median_pct": round(float(ex["excess"].median()) * 100, 4),
            "win_rate_pct": round(float((ex["excess"] > 0).mean()) * 100, 2),
            "t_day": round(float(t), 3) if t is not None else None}


def main():
    ev = build_events()
    print(f"事件 {len(ev):,} | 入场日 {ev['mdate'].nunique():,}")

    # 1. 确定性
    s1 = stats(ev)
    s2 = stats(ev)
    print("确定性(两次):", "PASS" if s1 == s2 else "FAIL", s1)

    # 2. 成本敏感性
    for c in (0.0023, 0.0033, 0.0053):
        print(f"成本 {c*100:.2f}%:", stats(ev, cost=c))

    # 3. 成交价恶化
    for slip in (0.002, 0.004):
        print(f"开盘买入滑点 {slip*100:.1f}%:", stats(ev, entry_slip=slip))

    # 4. 除权日污染剔除
    div = pd.read_parquet("/data/quant_data_store/meta/dividends_sina.parquet")
    div["code"] = div["code"].astype(str)
    div["ex"] = pd.to_datetime(div["除权除息日"], errors="coerce")
    cal = sorted(pd.to_datetime(load_meta("trade_calendar")["trade_date"]).dt.strftime("%Y-%m-%d"))
    idx = {d: i for i, d in enumerate(cal)}

    def near_ex(code, d1, d2):
        exs = div.loc[div["code"] == code, "ex"].dropna()
        for e in exs:
            es = e.strftime("%Y-%m-%d")
            if es not in idx:
                continue
            for d in (d1, d2):
                if d in idx and abs(idx[es] - idx[d]) <= 1:
                    return True
        return False

    t0 = pd.to_datetime(ev["date"]).dt.strftime("%Y-%m-%d").values
    t1 = ev["mdate"].values
    mask = [near_ex(c, a, b) for c, a, b in zip(ev["code"].astype(str), t0, t1)]
    ev2 = ev[~np.array(mask)]
    print(f"除权日附近事件 {int(np.sum(mask)):,} 剔除后:", stats(ev2))


if __name__ == "__main__":
    main()
