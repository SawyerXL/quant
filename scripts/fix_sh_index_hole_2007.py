"""修复本地 SH000001 2007-10-15/16/17 缺失（2026-10-25）。

取证：
- 本地 2000-2026 与官方全史逐日比对，唯一缺口 = 2007-10-15/16/17
  （2007 峰值 6124 那三天）。
- 因缺 3 天，本地 2007-10-18 的 pct_chg 按 10-12 收盘计算（-1.32%），
  实际应为 5825.282/6036.281-1 = -3.4956%。
- 双源核对一致：Sina `stock_zh_index_daily('sh000001')` 与
  baostock `sh.000001`（open/high/low/close/volume/amount 全一致）。

写入纪律：先备份；只补 3 行 + 修正 10-18 pct_chg；amount 沿用本地 SH 的
0.0 约定（官方额写入日志）；写后跑完整性/身份/连续性断言。
"""
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
P = ROOT / "data_store" / "daily" / "2007" / "SH000001.parquet"
BK = ROOT / "data_store" / "backup_index_hole_20261025"

# Sina/baostock 双源一致值（2026-10-25 核对）
ROWS = [
    ("2007-10-15", 5934.775, 6039.042, 5866.131, 6030.086, 9870744200),
    ("2007-10-16", 6056.946, 6124.044, 6040.713, 6092.057, 8815215900),
    ("2007-10-17", 6057.428, 6088.892, 5982.203, 6036.281, 7131258800),
]


def main():
    d = pd.read_parquet(P)
    d["date"] = d["date"].astype(str)
    print("before rows", len(d))
    BK.mkdir(parents=True, exist_ok=True)
    shutil.copy2(P, BK / "SH000001_2007.parquet")
    add = pd.DataFrame([{
        "date": dt, "code": "000001", "open": o, "high": h, "low": lo,
        "close": c, "volume": v, "amount": 0.0, "pct_chg": 0.0}
        for dt, o, h, lo, c, v in ROWS])
    d = d[~d["date"].isin(add["date"])]
    d = pd.concat([d, add], ignore_index=True).sort_values("date")
    # 重算 2007-10 窗口 pct，修 10-18（缺 3 天导致的 -1.32%）
    m = d["date"].str.startswith("2007-10")
    sub = d[m].copy()
    prev = d.loc[d["date"] < "2007-10-01"].tail(1)["close"].iloc[0]
    pcts = []
    for c in sub["close"]:
        pcts.append(round((float(c) / float(prev) - 1) * 100, 4))
        prev = c
    d.loc[m, "pct_chg"] = pcts
    d = d.reset_index(drop=True)
    d.to_parquet(P, index=False)
    print("after rows", len(d))
    # 断言
    x = d[(d["date"] >= "2007-10-12") & (d["date"] <= "2007-10-19")]
    print(x.to_string(index=False))
    assert len(x) == 6, "10-12..10-19 应有 6 行"
    assert abs(float(x[x.date == "2007-10-16"]["close"].iloc[0]) - 6092.057) < 1e-6
    assert abs(float(x[x.date == "2007-10-18"]["pct_chg"].iloc[0]) - (-3.4956)) < 0.01
    print("OK backup", BK / "SH000001_2007.parquet")


if __name__ == "__main__":
    sys.exit(main())
