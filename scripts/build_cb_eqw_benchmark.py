"""CB 等权盘中基准 + 连续性审计（2026-10-19, CB条款事件研究数据层）.

为什么: 事件入场是 T+1 开盘、T+5 收盘, 基准必须同窗同口径; CB 指数只有
收盘价, 无法给开盘入场做同窗基准。用逐券日线自建等权开→收基准, 且只使用
CB 自身数据链（符合"CB 腿不依赖股票日线库"架构原则）。

输出:
  data_store/meta/cb_eqw_oc.parquet — date, mean_oc, n_bonds
  logs/cb_daily_continuity_20261019.csv — 逐券连续性摘要(审计用)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import save_meta

START, END = "2019-01-01", "2026-09-30"
DAILY = Path("data_store/convertible_bonds/daily")


def main():
    files = sorted(DAILY.glob("*.parquet"))
    frames, audit = [], []
    for p in files:
        try:
            d = pd.read_parquet(p)
            if d.empty or not {"date", "open", "close"}.issubset(d.columns):
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"]).dt.normalize()
            d = d.sort_values("date")
            for c in ("open", "close"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
            d = d[(d["open"] > 0) & (d["close"] > 0)]
            d = d[(d["date"] >= START) & (d["date"] <= END)]
            if d.empty:
                continue
            d["oc"] = d["close"] / d["open"] - 1
            d["cc"] = d["close"].pct_change()
            dirty = int((d["cc"].abs() > 0.5).sum())
            audit.append({"code": p.stem, "rows": len(d),
                          "start": d["date"].min().date(), "end": d["date"].max().date(),
                          "dirty_cc_gt50pct": dirty,
                          "min_close": float(d["close"].min()),
                          "max_close": float(d["close"].max())})
            frames.append(d[["date", "open", "close", "oc"]].assign(code=p.stem))
        except Exception as e:
            audit.append({"code": p.stem, "rows": 0, "error": str(e)[:80]})

    df = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "code"])
    bm = (df.groupby("date")["oc"].agg(mean_oc="mean", n_bonds="count")
          .reset_index().sort_values("date"))
    save_meta("cb_eqw_oc", bm)
    # 同窗买入持有基准所需的开/收盘面板(date × code)
    opens = df.pivot(index="date", columns="code", values="open").reset_index()
    closes = df.pivot(index="date", columns="code", values="close").reset_index()
    save_meta("cb_open_panel", opens)
    save_meta("cb_close_panel", closes)
    print(f"panels: opens {opens.shape}, closes {closes.shape}")
    pd.DataFrame(audit).to_csv("logs/cb_daily_continuity_20261019.csv", index=False)

    print(f"daily files: {len(files)} | used: {len(frames)} | rows: {len(df):,}")
    print(f"benchmark days: {len(bm)} | date {bm['date'].min().date()}→{bm['date'].max().date()}")
    print(f"n_bonds per day: min {bm['n_bonds'].min()} / median {bm['n_bonds'].median():.0f} / max {bm['n_bonds'].max()}")
    a = pd.DataFrame(audit)
    print(f"dirty |close-to-close|>50% rows: {int(a['dirty_cc_gt50pct'].fillna(0).sum())} "
          f"across {int((a['dirty_cc_gt50pct'].fillna(0) > 0).sum())} bonds")
    print("worst bonds:\n", a.nlargest(5, "dirty_cc_gt50pct")[
        ["code", "rows", "dirty_cc_gt50pct", "min_close", "max_close"]].to_string(index=False))


if __name__ == "__main__":
    main()
