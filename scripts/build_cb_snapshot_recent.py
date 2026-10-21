"""当月 PIT 快照补齐（2026-10-21，CB 月度调仓配套）。

月度执行=**快照日之后的首个交易日**（1 日休市→当月首个交易日；1 日交易→
次一交易日）；快照日期=每月 1 日（build_snapshots 的 freq="MS"；1 日休市取
上月末收盘，1 日交易取 1 日收盘）。快照数据在快照日收盘后才可用，故 1 日
当天不建。周更只在周一 06:30 跑全量：若执行日不是周一，当月快照要到下周一
才存在，月度调仓会延后。本脚本每日 08:10 检查——**当月快照不存在才补建**，
存在则跳过（避免每日重建改写 PIT 快照）。
周一 06:30 周更可能仍在跑：检测到 build_cb_data 进程则跳过，由周更补上；
失败次日自动重试。08:10 在 14:28 信号之前。

用法:
  python scripts/build_cb_snapshot_recent.py            # 生产
  python scripts/build_cb_snapshot_recent.py --shadow   # 影子表验证(不写生产)
"""
import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger

from data.storage import load_meta
from monitoring.alerts import send_alert
from build_cb_data import build_snapshots

logger.add("logs/cb_snapshot_recent_{time:YYYY-MM-DD}.log",
           rotation="1 day", retention="365 days")


def _latest_snap(table="cb_snapshots"):
    snaps = load_meta(table)
    if snaps is None or snaps.empty:
        return None
    return pd.to_datetime(snaps["snap_date"]).max()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", action="store_true",
                    help="写 cb_snapshots_recent_test，不碰生产表")
    ap.add_argument("--force", action="store_true",
                    help="忽略'当月已存在'与周更进程检查（仅排障/影子用）")
    args = ap.parse_args()
    today = date.today()
    first = pd.Timestamp(today.replace(day=1))
    out_name = "cb_snapshots_recent_test" if args.shadow else "cb_snapshots"

    cal = load_meta("trade_calendar")
    cal_dates = set(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")) \
        if cal is not None and not cal.empty else set()
    if str(today) not in cal_dates:
        logger.info(f"{today} 非交易日, 跳过")
        return 0
    # 2026-10-21 无前视口径: 快照日(1日)的收盘数据在当日收盘后才可用
    # (1日休市时=上月末收盘)。执行日必须是快照日**之后**的首个交易日:
    # 1日休市→当月首个交易日; 1日交易→次一交易日。信号端用"新快照出现"
    # 触发, 自然落在同一天。
    if pd.Timestamp(today) <= first:
        logger.info(f"月初1日当天({today}): 快照数据(1日收盘)尚未生成, "
                    f"次一交易日再补")
        return 0
    if not args.shadow and not args.force:
        latest = _latest_snap("cb_snapshots")
        if latest is not None and latest >= first:
            logger.info(f"当月快照已存在({latest.date()}), 跳过")
            return 0
    if not args.force:
        # 周一 06:30 全量周更可能仍在跑；并发写同一张表会互相覆盖。
        probe = subprocess.run(["pgrep", "-f", "build_cb_data.py"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            logger.warning("build_cb_data 进程在跑(周更), 本次跳过; 次日仍缺则补建")
            return 0

    logger.info(f"补建当月快照: start={first.date()} end={today} -> {out_name}")
    try:
        build_snapshots(start=first.strftime("%Y-%m-%d"),
                        end=today.strftime("%Y-%m-%d"),
                        freq="MS", append=True, out_name=out_name)
    except Exception as e:
        logger.exception(f"当月快照补建失败: {e}")
        send_alert(f"CB当月快照补建失败({today}): {e}", level="warning")
        return 1

    latest = _latest_snap(out_name)
    if latest is None or latest < first:
        msg = f"补建后仍无当月快照(最新{latest})"
        logger.error(msg)
        send_alert(f"[CB] {msg}", level="warning")
        return 1
    snaps = load_meta(out_name)
    n = int((pd.to_datetime(snaps["snap_date"]) == latest).sum())
    logger.info(f"✓ 当月快照 {latest.date()}: {n} 只")
    if n == 0:
        send_alert(f"[CB] 当月快照 {latest.date()} 为空", level="warning")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
