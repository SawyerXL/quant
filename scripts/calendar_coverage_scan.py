"""
交易日历覆盖率完整性扫描（2026-09-08 立项；2026-10-25 修假阳性）

背景：两段全局数据洞（2026-05-11~06-16 / 07-01~07-06）静默存活数月才被
归因实验偶然撞上——日更增量追加、不做历史完整性校验。本脚本=周期扫描器
（建议 cron 每周日 07:30，非交易时段），对全库检查交易日历覆盖率。

2026-10-25 修复（首次运行 3537 "缺口" 中前 20 全是 2026-09/10 新上市）：
- 只统计 [first_bar, last_bar] 内的缺失；上市前/退市后不算缺口；
- 输出 first_bar/last_bar/active_missing，另报"窗口初已上市"的成熟票缺口；
- `--full` 真正读全史（旧版只读 2026，名不副实）；
- 文件名 `{time:...}` 字面量改为实际日期；
- 停牌与数据洞无法区分 → 标 candidate，人工核，不自动重拉。

口径：
- 默认窗口 2026-01-01~今天；--full 2000-01-01~今天。
- 日历源 = SH000001 自身交易日；**盲区：指数自身缺一天，日历也会缺，
  本扫描发现不了**（需独立交易日历源；2007-10 洞即此类）。
- 黑名单（price_blacklist）排除。
输出：logs/coverage_scan_YYYYMMDD.csv + 摘要打印。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist


def _max_gap(missing: list[str]) -> int:
    if not missing:
        return 0
    gap = best = 1
    prev = pd.Timestamp(missing[0])
    for m in missing[1:]:
        cur = pd.Timestamp(m)
        if (cur - prev).days <= 4:  # 隔周末仍算同一缺口
            gap += 1
        else:
            best = max(best, gap)
            gap = 1
        prev = cur
    return max(best, gap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]
    start = "2000-01-01" if args.full else "2026-01-01"
    sh = load_daily("SH000001", start, "2099-01-01")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    print(f"扫描对象: {len(codes)}只 | 窗口: {start}~今天 | 交易日历: {len(cal)}天")

    rows = []
    for i, c in enumerate(codes):
        try:
            d = load_daily(c, start, "2099-01-01")
        except Exception:
            d = pd.DataFrame()
        if d.empty:
            rows.append({"code": c, "first_bar": None, "last_bar": None,
                         "active_missing": None, "max_gap": None,
                         "stale_end": None, "max_close_2026": None,
                         "price_bad": False, "note": "窗口内无数据"})
            continue
        d = d.copy()
        dates = sorted(set(pd.to_datetime(d["date"]).astype(str).str[:10]))
        first, last = dates[0], dates[-1]
        have = set(dates)
        active_cal = [x for x in cal if first <= x <= last]
        missing = [x for x in active_cal if x not in have]
        stale_end = len(cal) > 10 and last < cal[-11]
        max_close = float(pd.to_numeric(d["close"], errors="coerce").max())
        price_bad = (max_close > 1000 and c not in {"600519"})
        note = []
        if stale_end:
            note.append("末端停滞(疑退市/长停)")
        if missing and not stale_end:
            note.append("候选缺口(停牌需人工核)")
        if price_bad:
            note.append("价格身份异常")
        if not missing and not price_bad and not stale_end:
            note.append("完整")
        if missing or price_bad or stale_end or args.full:
            rows.append({
                "code": c, "first_bar": first, "last_bar": last,
                "active_missing": len(missing), "max_gap": _max_gap(missing),
                "stale_end": bool(stale_end),
                "max_close_2026": round(max_close, 2),
                "price_bad": bool(price_bad), "note": ";".join(note),
            })
        if i % 1000 == 0:
            print(f"  进度 {i}/{len(codes)}", flush=True)

    df = pd.DataFrame(rows)
    df["_sort"] = pd.to_numeric(df["active_missing"], errors="coerce").fillna(-1)
    df = df.sort_values(["_sort", "code"], ascending=[False, True]).drop(columns="_sort")
    out = Path(f"logs/coverage_scan_{date.today():%Y%m%d}.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    hole = df[pd.to_numeric(df["active_missing"], errors="coerce").fillna(0) > 0]
    no_data = df[df["note"].str.contains("无数据", na=False)]
    mature_cutoff = "2025-12-31" if args.full else (cal[0] if cal else "2026-01-01")
    mature = hole[hole["first_bar"].notna() & (hole["first_bar"] <= mature_cutoff)]
    print(f"扫描完成: 候选缺口 {len(hole)} 只（已排除上市前/退市后；停牌需人工核）")
    print(f"  其中窗口初已上市(<= {mature_cutoff}) 的成熟票: {len(mature)} 只")
    print(f"  窗口内无数据: {len(no_data)} 只")
    print("缺口最多的20只（active_missing）:")
    print(hole.head(20)[["code", "first_bar", "last_bar", "active_missing",
                         "max_gap", "stale_end"]].to_string(index=False))
    if len(hole):
        print(f"⚠️ 候选缺口已落盘 {out}；先人工核停牌，再按 "
              f"logs/oos_backfill_protocol.md 处理；不要直接全量重拉。")
    print(f"输出: {out}")


if __name__ == "__main__":
    main()
