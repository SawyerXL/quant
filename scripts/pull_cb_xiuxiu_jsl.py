"""拉取 JSL 权威下修史（2026-10-21，修 N=71 分桶的 G2 双向盲区）。

G2 事件表记录**所有**转股价变动（含分红/送转调整），"降幅>5% 即下修"
会把 10 转 4 这类机械调整误计为下修；同时可能漏计早期下修。JSL
`bond_cb_adj_logs_jsl` 只收录下修（含底价），作权威口径。
输入 = logs/cb_xiuxiu_buckets_20261021.csv 的选入券（220 只）。
输出 logs/cb_xiuxiu_jsl_events_20261021.parquet（G2 同构: code/change_date/bef/aft）
+ 失败清单。用法: python scripts/pull_cb_xiuxiu_jsl.py
"""
import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd

OUT = Path("logs/cb_xiuxiu_jsl_events_20261021.parquet")
FAIL = Path("logs/cb_xiuxiu_jsl_fail_20261021.txt")
SLEEP = 0.25
RETRY = 3


def pull(code: str):
    for k in range(RETRY):
        try:
            df = ak.bond_cb_adj_logs_jsl(symbol=code)
            if df is None:
                raise RuntimeError("None")
            return df
        except Exception as e:  # noqa: BLE001
            if k == RETRY - 1:
                raise
            time.sleep(1.5 * (k + 1))
    return pd.DataFrame()


def main() -> int:
    codes = sorted(pd.read_csv("logs/cb_xiuxiu_buckets_20261021.csv",
                               dtype={"code": str})["code"].astype(str)
                   .str.zfill(6).unique())
    rows, fails = [], []
    for i, c in enumerate(codes):
        try:
            df = pull(c)
            for _, r in df.iterrows():
                eff = pd.to_datetime(r["新转股价生效日期"], errors="coerce")
                before = pd.to_numeric(r["下修前转股价"], errors="coerce")
                after = pd.to_numeric(r["下修后转股价"], errors="coerce")
                if pd.isna(eff) or pd.isna(before) or pd.isna(after):
                    continue
                rows.append({"code": c, "change_date": eff.date().isoformat(),
                             "bef": float(before), "aft": float(after),
                             "sh_date": str(r["股东大会日"])})
        except Exception as e:  # noqa: BLE001
            fails.append(f"{c}\t{type(e).__name__}\t{str(e)[:120]}")
        if (i + 1) % 20 == 0:
            print(f"{i+1}/{len(codes)} rows={len(rows)} fails={len(fails)}",
                  flush=True)
        time.sleep(SLEEP)
    ev = pd.DataFrame(rows, columns=["code", "change_date", "bef", "aft", "sh_date"])
    ev.to_parquet(OUT, index=False)
    FAIL.write_text("\n".join(fails), encoding="utf-8")
    print(f"done codes={len(codes)} events={len(ev)} bonds={ev['code'].nunique()} "
          f"fails={len(fails)} -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
