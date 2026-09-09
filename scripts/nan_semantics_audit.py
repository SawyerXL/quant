"""
NaN 段语义抽样验证 (2026-09-08 晚, 用户Q2决策"不修洞改为定义语义"的执行件) —
验证本地库 NaN≥5 段(3331只)的真实语义构成: 停牌 / 未上市 / 退市 / 数据洞。
判别法: 本地 NaN 段的日期范围内, baostock 日线有真实行情行 → 数据洞
(股票当时在交易, 本地漏了); baostock 段内无行 → 按位置分类:
  段全在 baostock 首行之前 → 未上市(或 baostock 覆盖缺口)
  段全在 baostock 末行之后 → 终止上市(对照 t7 退市候选表)
  其余(在 baostock 覆盖范围内却无行) → 真停牌/无交易日
抽样: 按 NaN 游程长度分层 30 只(短5-10日/中10-30日/长>30日 各10只),
每只验证其全部 NaN 游程(上限50段)。
用法: python scripts/nan_semantics_audit.py > logs/nan_semantics_audit.log 2>&1
输出: logs/nan_semantics_audit.csv
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_return_attribution import WINDOWS, load_blacklist

START, END = WINDOWS["main"]
LOAD_START = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
BS_DIR = Path("/data/quant_data_store/baostock/daily")


def build_panel():
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices = {}
    for code in codes:
        try:
            d = load_daily(code, LOAD_START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    sh = load_daily("SH000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    return panel


def nan_runs(s: pd.Series):
    """返回 [(start_date, end_date, length)] 的 NaN 连续段列表(≥5日)"""
    mask = s.isna()
    runs, start = [], None
    for dt, v in mask.items():
        if v and start is None:
            start = dt
        elif not v and start is not None:
            if (dt - start).days >= 5:
                runs.append((start, dt, len(s[start:dt])))
            start = None
    if start is not None and (mask.index[-1] - start).days >= 5:
        runs.append((start, mask.index[-1], len(s[start:])))
    return runs


def classify_run(code, d0, d1):
    """对照 baostock 判别 NaN 段语义"""
    p = BS_DIR / f"{code}.parquet"
    if not p.exists():
        return "baostock无文件"
    try:
        bs = pd.read_parquet(p)
    except Exception:
        return "baostock读失败"
    if bs.empty:
        return "baostock空"
    bd = pd.to_datetime(bs["date"])
    first, last = bd.min(), bd.max()
    seg = bs[(bd >= d0) & (bd <= d1)]
    if len(seg) > 0:
        nz = (pd.to_numeric(seg["volume"], errors="coerce").fillna(0) > 0).sum()
        if nz > 0:
            return f"数据洞(baostock有{nz}个真实交易日)"
        return "停牌(baostock有行但零量)"
    if d1 < first:
        return "未上市(段在baostock首行前)"
    if d0 > last:
        return "终止上市(段在baostock末行后)"
    return "真停牌(baostock覆盖内无行)"


def main():
    panel = build_panel()
    print(f"面板: {panel.shape[0]}日 × {panel.shape[1]}只, {START}~{END}")
    all_runs = {}
    for c in panel.columns:
        runs = nan_runs(panel[c])
        if runs:
            all_runs[c] = runs
    print(f"有≥5日NaN游程的股票: {len(all_runs)}只")
    # 按最长游程分层抽样
    longest = sorted(all_runs.items(), key=lambda kv: -max(r[2] for r in kv[1]))
    short = [(c, r) for c, r in all_runs.items() if max(x[2] for x in r) <= 10][:10]
    mid = [(c, r) for c, r in all_runs.items() if 10 < max(x[2] for x in r) <= 30][:10]
    long_ = longest[:10]
    sample = short + mid + long_
    rows, counts = [], {}
    for c, runs in sample:
        for d0, d1, ln in runs[:50]:
            verdict = classify_run(c, d0, d1)
            counts[verdict] = counts.get(verdict, 0) + 1
            rows.append({"code": c, "start": str(d0.date()), "end": str(d1.date()),
                         "len": ln, "verdict": verdict})
            if len(rows) % 200 == 0:
                print(f"  已验证{len(rows)}段...")
    df = pd.DataFrame(rows)
    df.to_csv("logs/nan_semantics_audit.csv", index=False, encoding="utf-8-sig")
    print(f"\n=== 30只抽样验证结果: {len(df)}个NaN段 ===")
    tot = len(df)
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}段 ({v/tot*100:.1f}%)")
    print(f"\n落盘: logs/nan_semantics_audit.csv")


if __name__ == "__main__":
    main()
