"""CB 月度调仓验证器（2026-10-21，11-02 首次月度调仓配套）。

只读检查（不交易）：
1. 当天是否为"快照日之后的首个交易日"（预期调仓日；否则跳过）。
2. 当月快照是否存在（cb_snapshots 最新 >= 当月1日；当月行数 >0）。
3. signal_cb_latest.json：signal_date=今天、schedule=monthly_snapdelay、
   rebalance=True、snap_date=latest_snapshot=当月1日、holdings=25。
4. 实盘 CB 持仓快照（logs/qmt_positions_latest.json）日期=今天、CB 只数；
   不足 25 只记警告（执行摩擦），0/缺失记失败。
5. 失败 → 非零退出 + 告警。

闸门十六：每次运行先跑 --selftest（已知失败用例：rebalance=False /
旧 snap_date 必须被抓到；抓不到 → SELFTEST_FAIL，验证器自身失效）。
用法:
  python scripts/verify_cb_monthly_rebalance.py            # 当日检查（非调仓日跳过）
  python scripts/verify_cb_monthly_rebalance.py --date 2026-11-02 --expect-snap 2026-11-01
  python scripts/verify_cb_monthly_rebalance.py --selftest
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_meta
from monitoring.alerts import send_alert

SIGNAL = Path("data_store/meta/signal_cb_latest.json")
POSITIONS = Path("logs/qmt_positions_latest.json")
CB_PREFIX = ("110", "111", "113", "118", "123", "127", "128")
TOP_N = 25


def check_signal(date_str: str, expect_snap: str,
                 signal: dict) -> tuple[list[str], list[str]]:
    """信号字段检查（纯函数，selftest 用）。holdings<25 只警告（临期/停牌
    剔除是事件，缺券数由 signal.excluded 解释）；0 只才失败。"""
    fails, warns = [], []
    if str(signal.get("signal_date", ""))[:10] != date_str:
        fails.append(f"signal_date={signal.get('signal_date')} != {date_str}")
    if signal.get("schedule") != "monthly_snapdelay":
        fails.append(f"schedule={signal.get('schedule')} != monthly_snapdelay")
    if not signal.get("rebalance"):
        fails.append("rebalance != True（预期月度调仓日未触发）")
    if str(signal.get("snap_date", ""))[:10] != expect_snap:
        fails.append(f"snap_date={signal.get('snap_date')} != {expect_snap}")
    if str(signal.get("latest_snapshot", ""))[:10] != expect_snap:
        fails.append(f"latest_snapshot={signal.get('latest_snapshot')} != {expect_snap}")
    n = len(signal.get("holdings", []))
    if n == 0:
        fails.append("holdings=0")
    elif n < TOP_N:
        warns.append(f"holdings={n} < {TOP_N}（剔除事件，需核对 excluded）")
    if signal.get("interim_deployment"):
        warns.append("interim_deployment=True（调仓日部分部署？）")
    return fails, warns


def selftest() -> int:
    good = {"signal_date": "2026-11-02", "schedule": "monthly_snapdelay",
            "rebalance": True, "snap_date": "2026-11-01",
            "latest_snapshot": "2026-11-01", "holdings": [str(i) for i in range(TOP_N)]}
    bad = dict(good, rebalance=False)
    stale = dict(good, snap_date="2026-10-01", latest_snapshot="2026-10-01")
    if not check_signal("2026-11-02", "2026-11-01", good)[0]:
        print("SELFTEST_OK (good case passes)")
    else:
        print("SELFTEST_FAIL: good case flagged"); return 2
    if not check_signal("2026-11-02", "2026-11-01", bad)[0]:
        print("SELFTEST_FAIL: rebalance=False not caught"); return 2
    if not check_signal("2026-11-02", "2026-11-01", stale)[0]:
        print("SELFTEST_FAIL: stale snap_date not caught"); return 2
    print("SELFTEST_OK (2 known failures caught)")
    return 0


def expected_rebalance_day(expect_snap: str) -> str | None:
    cal = load_meta("trade_calendar")
    if cal is None or cal.empty:
        return None
    ds = sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d").unique())
    for d in ds:
        if d > expect_snap:
            return d
    return None


def check_snapshot(expect_snap: str) -> tuple[list[str], int]:
    fails, n = [], 0
    snaps = load_meta("cb_snapshots")
    if snaps is None or snaps.empty:
        return ["cb_snapshots 为空"], 0
    snaps = snaps.copy()
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    latest = snaps["snap_date"].max()
    if str(latest.date())[:10] < expect_snap:
        fails.append(f"最新快照 {latest.date()} < 当月 {expect_snap}（08:10 补建失败？）")
    n = int((snaps["snap_date"] == pd.Timestamp(expect_snap)).sum())
    if n == 0:
        fails.append(f"当月快照 {expect_snap} 行数=0")
    return fails, n


def check_positions(date_str: str) -> tuple[list[str], list[str], int]:
    fails, warns = [], []
    if not POSITIONS.exists():
        return ["无实盘持仓快照 logs/qmt_positions_latest.json"], warns, 0
    try:
        d = json.loads(POSITIONS.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"持仓快照读取失败: {e}"], warns, 0
    exp = str(d.get("exported_at", ""))[:10]
    if exp and exp < date_str:
        warns.append(f"持仓快照日期 {exp} 早于 {date_str}")
    pos = d.get("positions", {})
    cb = [c for c, v in pos.items()
          if isinstance(v, dict) and v.get("volume", 0) > 0
          and str(c)[:3] in CB_PREFIX]
    if not cb:
        fails.append("持仓快照无 CB 持仓")
    elif len(cb) < TOP_N:
        warns.append(f"CB 持仓 {len(cb)} < {TOP_N}（执行摩擦/部分成交，需对账）")
    return fails, warns, len(cb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--expect-snap", default=None,
                    help="默认=检查日的当月1日")
    ap.add_argument("--force", action="store_true", help="非预期调仓日也检查")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    # 闸门十六：每次先跑已知失败用例
    st = selftest()
    if st != 0:
        print("SELFTEST_FAIL: 验证器自身失效，本次结果不可信")
        send_alert("[CB月度验证] SELFTEST_FAIL：验证器失效", level="warning")
        return 2

    date_str = str(args.date)[:10]
    expect_snap = args.expect_snap or f"{date_str[:7]}-01"
    expect_day = expected_rebalance_day(expect_snap)
    if not args.force and expect_day and date_str != expect_day:
        print(f"非预期调仓日（预期 {expect_day}，今天 {date_str}）→ 跳过")
        return 0
    if expect_day is None:
        print("无法从交易日历计算预期调仓日")
        return 1

    fails, n_snap = check_snapshot(expect_snap)
    signal = {}
    if SIGNAL.exists():
        try:
            signal = json.loads(SIGNAL.read_text(encoding="utf-8"))
        except Exception as e:
            fails.append(f"信号读取失败: {e}")
    else:
        fails.append(f"无信号文件 {SIGNAL}")
    warns = []
    if signal:
        sf, sw = check_signal(date_str, expect_snap, signal)
        fails += sf
        warns += sw
    pf, pw, n_cb = check_positions(date_str)
    fails += pf
    warns += pw

    print(f"CB 月度调仓验证 {date_str}（快照 {expect_snap}，预期调仓日 {expect_day}）")
    print(f"  快照行数={n_snap}, CB持仓={n_cb}, "
          f"signal_date={signal.get('signal_date')}, "
          f"rebalance={signal.get('rebalance')}, "
          f"snap_date={signal.get('snap_date')}")
    for w in warns:
        print(f"  WARN: {w}")
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        send_alert("[CB月度验证] FAIL: " + " | ".join(fails), level="warning")
        return 1
    print("  PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
