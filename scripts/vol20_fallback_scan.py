"""
vol20 fallback 统计（2026-09-13 晚，D2 放行前补充项①）——
inv_vol 加权的 fallback 面量化：调仓日落入 <10 有效样本的 (票,日) 数量，
交叉断裂标记（price_break_flag→NaN→有效样本减少→fallback 或 vol 低估）。
D2 结论据此判断是否继承数据质量偏差。

只读，不写库。旧库诊断——数字全部标 `数值待确认`，待干净库重跑。
用法: timeout 40m ionice -c 3 nice -n 10 .venv/bin/python \
      scripts/vol20_fallback_scan.py
输出: logs/vol20_fallback_scan.json
"""
import json
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
from backtest_engine import make_rebal_dates

START, END = WINDOWS["main"]
OUT = Path("logs/vol20_fallback_scan.json")


def main():
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []

    prices, breaks = {}, {}
    for code in codes:
        try:
            d = load_daily(code, load_start, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce")
            if len(cl) >= 200:
                if "price_break_flag" in d.columns:
                    fl = pd.to_numeric(d["price_break_flag"],
                                       errors="coerce").fillna(0.0)
                    cl = cl.where(fl != 1)   # 与消费端同语义: 断裂日→NaN
                    breaks[code] = fl
                prices[code] = cl
        except Exception:
            pass

    panel = pd.DataFrame(prices).sort_index()
    bp = pd.DataFrame(breaks).sort_index() if breaks else pd.DataFrame()
    sh = load_daily("SH000001", load_start, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    bp = bp[bp.index.isin(pd.to_datetime(cal))] if not bp.empty else bp

    dates = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    rows, total_fb, total_fb_brk = [], 0, 0
    for d in dates:
        i = idx[d]
        win = panel.iloc[max(0, i - 21):i]
        valid = (~win.isna()).sum(axis=0)          # 每票窗口内非NaN天数
        fb = valid[valid < 10]                     # fallback 票
        n_stocks = len(panel.columns)
        if not bp.empty:
            bwin = bp.iloc[max(0, i - 21):i]
            brk_cnt = bwin.sum(axis=0)
            fb_brk = int((fb.index.map(brk_cnt) > 0).sum())
        else:
            fb_brk = 0
        total_fb += len(fb)
        total_fb_brk += fb_brk
        rows.append({"date": d, "stocks": n_stocks,
                     "fallback": int(len(fb)),
                     "fallback_with_break": fb_brk})

    total_cells_brk = int(bp.sum().sum()) if not bp.empty else 0
    res = {"window": [START, END], "rebal_dates": len(dates),
           "library_label": "旧库诊断, 数值待确认, 待干净库重跑",
           "break_cells_total": total_cells_brk,
           "fallback_events_total": total_fb,
           "fallback_events_with_break": total_fb_brk,
           "per_date": rows}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"调仓日 {len(dates)} 个, 断裂单元 {total_cells_brk} 个")
    print(f"fallback 事件 {total_fb} 次, 其中窗口含断裂日 {total_fb_brk} 次")
    print(f"落盘: {OUT}")


if __name__ == "__main__":
    main()
