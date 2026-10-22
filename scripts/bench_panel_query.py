"""Polars 面板查询 vs 逐票 pandas 的基准（2026-10-22）。

同一任务：取成交额 TOP30 候选池近 LOOKBACK 日的 date×code 成交额面板，
比较 data.panel_query.amount_panel 与 backtest_data 逐票 load_daily 的耗时/结果。
只读，不写库。
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from data.panel_query import amount_panel
from data.storage import load_daily, load_meta

LOOKBACK_DAYS = 45


def pandas_panel(codes: list[str], start: str, end: str) -> pd.DataFrame:
    frames = {}
    for c in codes:
        try:
            d = load_daily(c, start, end)
            if d is None or d.empty or "amount" not in d:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            s = pd.to_numeric(d.set_index("date")["amount"], errors="coerce").dropna()
            if len(s) >= 10:
                frames[c] = s
        except Exception:
            continue
    return pd.DataFrame(frames).sort_index() if frames else pd.DataFrame()


def main() -> int:
    meta = load_meta("stock_info_full")
    codes = sorted({str(c).zfill(6) for c in meta["code"].tolist()}) if meta is not None else []
    end = date.today().isoformat()
    start = (pd.Timestamp(end) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    t0 = time.time()
    df_pd = pandas_panel(codes, start, end)
    t_pd = time.time() - t0

    t0 = time.time()
    df_pl = amount_panel(start, end, codes).to_pandas().set_index("date").sort_index()
    t_pl = time.time() - t0

    # 对齐比较（pandas 索引为字符串，polars 亦为字符串）
    common_idx = df_pd.index.intersection(df_pl.index)
    common_col = df_pd.columns.intersection(df_pl.columns)
    diff = float("nan")
    if len(common_idx) and len(common_col):
        a = df_pd.loc[common_idx, common_col].astype(float)
        b = df_pl.loc[common_idx, common_col].astype(float)
        diff = float((a - b).abs().max().max())

    print(f"codes={len(codes)} window={start}..{end}")
    print(f"pandas : {df_pd.shape} {t_pd:.2f}s")
    print(f"polars : {df_pl.shape} {t_pl:.2f}s  speedup={t_pd / t_pl if t_pl > 0 else float('nan'):.1f}x")
    print(f"common {len(common_idx)}x{len(common_col)} max_abs_diff={diff:.6g}")
    ok = diff < 1e-6 if diff == diff else False
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
