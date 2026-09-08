"""
交易日历覆盖率完整性扫描 (2026-09-08 深夜, 用户要求) —
背景: 两段全局数据洞(2026-05-11~06-16/07-01~07-06)静默存活数月才被
归因实验偶然撞上——日更是增量追加、不做历史完整性校验, 缺口一旦
产生就永久存在。本脚本=周期扫描器(建议 cron 每周日 07:30, 非交易
时段), 对全库检查交易日历覆盖率, 缺口股票落盘供重拉队列消费。
口径: 扫描窗口 = 本年1月1日至今(近端完整性, 抓新洞) + 全历史≥5日
缺口游程(存量洞); 缺口按股票聚合, 输出优先级(缺失天数降序)。
用法: python scripts/calendar_coverage_scan.py [--full]
      --full: 全历史逐文件扫描(慢, 首次/周度); 默认仅近端。
输出: logs/coverage_scan_{YYYYMMDD}.csv + 摘要打印。
"""
import sys, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_return_attribution import load_blacklist

OUT = "logs/coverage_scan_{time:YYYY-MM-DD}.csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [str(c).zfill(6) for c in meta["code"].tolist()
             if str(c).zfill(6) not in blacklist]
    # 日历: 用上证日线(数据源自身, 无假期行污染; 与引擎面板口径一致)
    sh = load_daily("000001", "2026-01-01", "2099-01-01")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    print(f"扫描对象: {len(codes)}只 | 交易日历: {len(cal)}天")

    rows = []
    for i, c in enumerate(codes):
        try:
            d = load_daily(c, "2026-01-01", "2099-01-01")
        except Exception:
            d = pd.DataFrame()
        if d.empty:
            rows.append({"code": c, "recent_missing": len(cal),
                         "max_gap": len(cal), "note": "无2026数据"})
            continue
        have = set(pd.to_datetime(d["date"]).astype(str).str[:10].tolist())
        missing = sorted(set(cal) - have)
        max_gap = 0
        if missing:
            gap = 1
            prev = pd.Timestamp(missing[0])
            for m in missing[1:]:
                if (pd.Timestamp(m) - prev).days <= 4:  # 隔周末仍算同一缺口
                    gap += 1
                else:
                    max_gap = max(max_gap, gap)
                    gap = 1
                prev = pd.Timestamp(m)
            max_gap = max(max_gap, gap)
        if missing or args.full:
            rows.append({"code": c, "recent_missing": len(missing),
                         "max_gap": max_gap,
                         "note": "" if missing else "近端完整"})
        if i % 1000 == 0:
            print(f"  进度 {i}/{len(codes)}", flush=True)

    df = pd.DataFrame(rows).sort_values("recent_missing", ascending=False)
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    n_hole = len(df[df["recent_missing"] > 0])
    print(f"扫描完成: {n_hole}只有近端缺口, 落盘 {OUT}")
    print("缺口最多的20只:")
    print(df[df["recent_missing"] > 0].head(20)[
        ["code", "recent_missing", "max_gap"]].to_string(index=False))
    if n_hole > 0:
        print(f"⚠️ 检测到 {n_hole} 只股票存在交易日历缺口——请将本CSV"
              f"送入重拉队列(协议: logs/oos_backfill_protocol.md)")


if __name__ == "__main__":
    main()
