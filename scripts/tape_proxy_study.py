"""盘口“盖板/托板/内外盘”代理检验（前向，预注册 N=65）。

预注册：logs/prereg_tape_spoofing_forward_20261020.md。
数据：data_store/intraday/tick/{YYYYMMDD}.parquet（Windows 每日 15:10 采集）。
本脚本对单日/多日给出**描述统计**；正式判读须等 ≥40 个交易日。
用法：python scripts/tape_proxy_study.py [tick_dir]
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data_store/intraday/tick")
OUT_JSON = Path("logs/tape_proxy_pilot_20261020.json")
OUT_EV = Path("logs/tape_proxy_events_20261020.csv")
K_LEVEL = 10.0
K_DAY = 3.0
DEDUPE_S = 300
HORIZON_S = 1800
FAKE_S = 60


def load_day(path):
    d = pd.read_parquet(path)
    num = pd.to_numeric(d["time"], errors="coerce")
    dt = pd.to_datetime(num, unit="ms", utc=True)
    d["dt"] = dt.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    d = d[d["dt"].notna()].copy()
    d["tod"] = d["dt"].dt.strftime("%H:%M:%S")
    d = d[(d["tod"] >= "09:30:00") & (d["tod"] <= "15:00:00")]
    d = d[pd.to_numeric(d["lastPrice"], errors="coerce") > 0]
    for c in ["lastPrice", "tickvol", "a1", "b1"] + \
             [f"a{i}" for i in range(1, 6)] + [f"b{i}" for i in range(1, 6)] + \
             [f"av{i}" for i in range(1, 6)] + [f"bv{i}" for i in range(1, 6)]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def stock_events(g):
    g = g.sort_values("dt").reset_index(drop=True)
    if len(g) < 30:
        return [], None
    n = len(g)
    sec = (g["dt"].values - g["dt"].values[0]) / np.timedelta64(1, "s")
    lp = g["lastPrice"].values.astype(float)
    tv = g["tickvol"].fillna(0).values.astype(float)
    a1, b1 = g["a1"].values, g["b1"].values
    mid = np.where((a1 > 0) & (b1 > 0), (a1 + b1) / 2.0, lp)
    out = []
    last_ev = {"ask": -1e9, "bid": -1e9}
    for side, px, vol, lvls, med_day in (
            ("ask", a1 * 0 + g["a5"].values, g["av5"].values,
             g[[f"av{i}" for i in range(1, 5)]].values,
             np.nanmedian(g.loc[g["av5"] > 0, "av5"].values)),
            ("bid", g["b5"].values, g["bv5"].values,
             g[[f"bv{i}" for i in range(1, 5)]].values,
             np.nanmedian(g.loc[g["bv5"] > 0, "bv5"].values))):
        if not np.isfinite(med_day) or med_day <= 0:
            continue
        med_lvl = np.nanmedian(lvls, axis=1)
        cond = (vol >= K_LEVEL * med_lvl) & (vol >= K_DAY * med_day) & (vol > 0)
        prev = np.r_[False, cond[:-1]]
        consec = cond & prev
        diff_ok = np.r_[False, np.diff(sec) <= 10]
        ev_idx = np.flatnonzero(consec & diff_ok)
        for i in ev_idx:
            if sec[i] - last_ev[side] < DEDUPE_S:
                continue
            last_ev[side] = sec[i]
            j0 = int(np.searchsorted(sec, sec[i] + 0.5))
            j1 = int(np.searchsorted(sec, sec[i] + FAKE_S, side="right"))
            k60 = max(i, j1 - 1)
            touch = False
            if j1 > j0:
                if side == "ask":
                    touch = bool((lp[j0:j1] >= px[i] - 1e-6).any())
                else:
                    touch = bool((lp[j0:j1] <= px[i] + 1e-6).any())
            vol60 = float(tv[j0:j1].sum())
            after_vol = float(vol[k60])
            vanished = (after_vol <= 0.3 * vol[i]) and (not touch) and (vol60 == 0.0)
            executed = touch and vol60 > 0
            kind = "executed" if executed else ("vanished" if vanished else "other")
            k30 = int(np.searchsorted(sec, sec[i] + HORIZON_S, side="right")) - 1
            if k30 > i:
                ret30 = float(mid[k30] / mid[i] - 1)
                mae30 = float(np.min(mid[i:k30 + 1]) / mid[i] - 1)
            else:
                ret30, mae30 = np.nan, np.nan
            day_ret = float(lp[i] / g["lastClose"].iloc[0] - 1) \
                if g["lastClose"].iloc[0] > 0 else np.nan
            rng = float((g["high"].iloc[-1] - g["low"].iloc[-1]) /
                        g["lastClose"].iloc[0]) if g["lastClose"].iloc[0] > 0 else np.nan
            out.append({"code": g["code"].iloc[0], "side": side,
                        "t": str(g["dt"].iloc[i]), "minute": sec[i] / 60.0,
                        "wall_px": float(px[i]), "wall_vol": float(vol[i]),
                        "med_day": float(med_day), "kind": kind,
                        "touch60": touch, "vol60": vol60,
                        "ret30": ret30, "mae30": mae30,
                        "day_ret": day_ret, "day_range": rng,
                        "lure": bool((abs(day_ret) >= 0.02) or (rng >= 0.03))
                        if np.isfinite(day_ret) else False})
    return out, g


def main():
    files = sorted(SRC.glob("*.parquet"))
    print(f"files {len(files)}")
    if not files:
        print("no tick files")
        return
    events, imb = [], []
    for p in files:
        day = load_day(p)
        if day.empty:
            continue
        day_ev, grids = [], {}
        for code, g in day.groupby("code"):
            ev, gg = stock_events(g)
            day_ev.extend(ev)
            if gg is None:
                continue
            s = pd.Series(
                np.where((gg["a1"] > 0) & (gg["b1"] > 0),
                         (gg["a1"] + gg["b1"]) / 2, gg["lastPrice"]),
                index=gg["dt"]).resample("1min").last()
            grids[code] = s
            prev_a1 = gg["a1"].shift(1)
            prev_b1 = gg["b1"].shift(1)
            buy = (gg["lastPrice"] >= prev_a1).fillna(False)
            sell = (gg["lastPrice"] <= prev_b1).fillna(False)
            minute = ((gg["dt"] - gg["dt"].dt.normalize()).dt.total_seconds() // 60)
            bins = (minute // 5) * 5
            agg = pd.DataFrame({"bin": bins, "buy": np.where(buy, gg["tickvol"], 0),
                                "sell": np.where(sell, gg["tickvol"], 0)}).groupby("bin").sum()
            tot = agg["buy"] + agg["sell"]
            agg = agg[tot > 0]
            if len(agg):
                imb.extend([(code, int(b), float((r.buy - r.sell) / (r.buy + r.sell)))
                            for b, r in agg.iterrows()])
        # 当日分钟网格 -> 同一时刻的观察池等权 30 分钟收益（横截面超额基准）
        if day_ev and grids:
            d0 = pd.Timestamp(day_ev[0]["t"]).normalize()
            grid = pd.DataFrame(grids).reindex(
                pd.date_range(d0 + pd.Timedelta(hours=9, minutes=30),
                              d0 + pd.Timedelta(hours=15), freq="1min")).ffill()
            mret = (grid.shift(-30) / grid - 1).mean(axis=1)
            for r in day_ev:
                t0 = pd.Timestamp(r["t"]).floor("1min")
                m = mret.get(t0, np.nan)
                r["ex30"] = (r["ret30"] - m) if np.isfinite(r["ret30"]) else np.nan
        events.extend(day_ev)
    ev = pd.DataFrame(events)
    if len(ev):
        ev.to_csv(OUT_EV, index=False)

    def agg_ev(sub):
        if len(sub) == 0:
            return {"n": 0}
        return {"n": int(len(sub)),
                "ret30_mean_pct": float(np.nanmean(sub["ret30"]) * 100),
                "ex30_mean_pct": float(np.nanmean(sub["ex30"]) * 100),
                "mae30_mean_pct": float(np.nanmean(sub["mae30"]) * 100),
                "ex30_median_pct": float(np.nanmedian(sub["ex30"]) * 100)}

    out = {"prereg": "logs/prereg_tape_spoofing_forward_20261020.md",
           "files": [p.name for p in files], "n_events": int(len(ev)),
           "events": {}}
    if len(ev):
        for side in ("ask", "bid"):
            for kind in ("executed", "vanished", "other"):
                out["events"][f"{side}_{kind}"] = agg_ev(
                    ev[(ev.side == side) & (ev.kind == kind)])
        out["events"]["bid_lure_vanished"] = agg_ev(
            ev[(ev.side == "bid") & (ev.kind == "vanished") & ev.lure])
        out["events"]["ask_lure_vanished"] = agg_ev(
            ev[(ev.side == "ask") & (ev.kind == "vanished") & ev.lure])
    # 内外盘十分位（描述）
    if imb:
        im = pd.DataFrame(imb, columns=["code", "bin", "imb"])
        im["q"] = pd.qcut(im["imb"], 10, labels=False, duplicates="drop")
        out["imbalance"] = {str(int(q)): {"n": int(len(g)),
                                          "imb_mean": float(g["imb"].mean())}
                            for q, g in im.groupby("q")}
        out["imbalance_n"] = int(len(im))
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:3500])


if __name__ == "__main__":
    main()
