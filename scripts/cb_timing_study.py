"""CB 腿趋势过滤研究（N=69）。

预注册：logs/prereg_cb_timing_20261021.md（判据出数前锁）。
主口径：CB 指数 MA200 日频信号，t−1 收盘信号 → t 日持有策略/现金；
对照：nav_yi 信号、月末信号、半仓、MA100/MA250（诊断）。
用法：python scripts/cb_timing_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from run_backtest_a import CASH_YIELD

COST = 0.001
OUT = Path("logs/cb_timing_20261021.json")


def met(r):
    r = r.dropna()
    if len(r) < 20:
        return {"n": len(r)}
    nav = (1 + r).cumprod()
    years = len(r) / 252.0
    ann = nav.iloc[-1] ** (1 / years) - 1
    sr = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    return {"n": int(len(r)), "ann_pct": round(float(ann) * 100, 2),
            "sharpe": round(float(sr), 3), "dd_pct": round(float(dd) * 100, 2)}


def overlay(r_base, sig, cash):
    sig = sig.reindex(r_base.index).ffill().fillna(0.0)
    switch = sig.diff().abs().fillna(0.0)
    return sig * r_base + (1 - sig) * cash - COST * switch, int((switch > 0).sum()), sig


def main():
    nav = pd.read_csv("logs/cb_eligibility_arms_nav.csv",
                      index_col=0, parse_dates=True)
    r_base = nav["nav_V3_N10"].pct_change().dropna()
    idx = pd.read_parquet("data_store/meta/cb_index.parquet")
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.set_index("date")["close"].astype(float).sort_index()
    idx = idx.reindex(r_base.index, method="ffill")
    cash = float((1 + CASH_YIELD) ** (1 / 252) - 1)

    out = {"prereg": "logs/prereg_cb_timing_20261021.md",
           "n_days": int(len(r_base)), "start": str(r_base.index.min().date()),
           "end": str(r_base.index.max().date()), "cost": COST,
           "cash_yield": CASH_YIELD, "arms": {}}
    base = met(r_base)
    out["base"] = base
    print("base", base)

    def add(name, r, sig=None, switches=None):
        m = met(r)
        m["switches"] = switches
        m["time_in"] = round(float(sig.mean()) * 100, 1) if sig is not None else None
        out["arms"][name] = m
        print(name, m)

    for ma in (200, 100, 250):
        sig = (idx > idx.rolling(ma, min_periods=min(120, ma)).mean()
               ).astype(float).shift(1)
        r, sw, s = overlay(r_base, sig, cash)
        add(f"idx_MA{ma}", r, s, sw)

    sig_yi = (nav["nav_yi"] > nav["nav_yi"].rolling(200, min_periods=120).mean()
              ).astype(float).shift(1)
    r, sw, s = overlay(r_base, sig_yi, cash)
    add("yi_MA200", r, s, sw)
    split0 = "2019-01-01"
    out["IS_yi"] = met(r[r.index < split0])
    out["OOS_yi"] = met(r[r.index >= split0])
    out["OOS_yi_base"] = met(r_base[r_base.index >= split0])
    yi_ma = nav["nav_yi"].rolling(200, min_periods=120).mean()
    out["current_yi"] = {"date": str(nav.index[-1].date()),
                         "nav": float(nav["nav_yi"].iloc[-1]),
                         "ma200": float(yi_ma.iloc[-1]),
                         "signal_on": bool(nav["nav_yi"].iloc[-1] > yi_ma.iloc[-1])}

    # 月末信号：上月末信号 → 下月生效
    sig_m = (idx > idx.rolling(200, min_periods=120).mean()).astype(float)
    sig_m = sig_m.resample("ME").last().shift(1).reindex(sig_m.index, method="ffill")
    r, sw, s = overlay(r_base, sig_m, cash)
    add("idx_MA200_monthly", r, s, sw)

    # 半仓（信号关闭时 0.5 策略 + 0.5 现金）
    sig = (idx > idx.rolling(200, min_periods=120).mean()).astype(float).shift(1)
    r_half = sig * r_base + (1 - sig) * (0.5 * r_base + 0.5 * cash)
    add("idx_MA200_half", r_half, sig, int((sig.diff().abs() > 0).sum()))

    # IS/OOS
    split = "2019-01-01"
    out["IS_base"] = met(r_base[r_base.index < split])
    out["OOS_base"] = met(r_base[r_base.index >= split])
    for name in ("idx_MA200",):
        sig = (idx > idx.rolling(200, min_periods=120).mean()).astype(float).shift(1)
        r, sw, s = overlay(r_base, sig, cash)
        out["IS_overlay"] = met(r[r.index < split])
        out["OOS_overlay"] = met(r[r.index >= split])
        break

    p = out["arms"]["idx_MA200"]
    o = out["OOS_overlay"]
    b = out["OOS_base"]
    crit = {
        "1_sharpe": p["sharpe"] >= base["sharpe"] + 0.05,
        "2_dd": p["dd_pct"] >= base["dd_pct"] + 2.0,
        "3_ann": p["ann_pct"] >= base["ann_pct"] - 1.0,
        "4_oos": o["sharpe"] >= b["sharpe"] and o["dd_pct"] >= b["dd_pct"] + 1.0,
        "5_switches": p["switches"] <= 60,
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"

    # 当前信号
    idx_last = idx.dropna()
    ma = idx_last.rolling(200, min_periods=120).mean()
    out["current"] = {"date": str(idx_last.index[-1].date()),
                      "close": float(idx_last.iloc[-1]),
                      "ma200": float(ma.iloc[-1]),
                      "signal_on": bool(idx_last.iloc[-1] > ma.iloc[-1])}
    print("criteria", crit, out["verdict"])
    print("current", out["current"])
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
