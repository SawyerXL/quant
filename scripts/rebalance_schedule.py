"""调仓日程唯一实现(2026-10-19, 日程三原则裁定 b: 统一来源)。

实盘语义(以实盘为准, 裁定 a):
  - 月末调仓 = 当月倒数第二个交易日
  - 月中调仓 = 当月交易日中点 **n//2−1**(非固定 15 日)
三处调用(2026-10-19 前各自实现且互不一致, 已实锤):
  ① 实盘链 daily_signal_a_v2.is_rebalance_day  = n//2−1 (9-14/10-19)
  ② A2 回测 run_backtest_a2._make_rebal_dates   = n//2   (9-15/10-20 晚一天)
  ③ 引擎 backtest_engine.make_rebal_dates       = 字面 15 日 + 自然月末
本模块为唯一来源, 三处必须 import, 不得再自带拷贝(闸门二十一精神)。
"""
from __future__ import annotations

from collections import defaultdict


def _by_month(calendar) -> dict[str, list[str]]:
    """calendar: list[str] 或可迭代日期 → {'YYYY-MM': [sorted 'YYYY-MM-DD', ...]}。"""
    out: dict[str, list[str]] = defaultdict(list)
    for d in calendar:
        s = str(d)[:10]
        out[s[:7]].append(s)
    for k in out:
        out[k].sort()
    return out


def month_mid_index(n: int) -> int:
    """月中调仓在当月交易日列表中的下标: n//2−1(实盘语义)。

    9 月 21 个交易日 → 9(9-14); 10 月 17 个交易日 → 7(10-19)。
    """
    return max(0, n // 2 - 1)


def month_end_index(n: int) -> int:
    """月末调仓下标: 倒数第二个交易日(为执行失败预留 1 天缓冲)。"""
    return -2 if n >= 2 else -1


def is_rebalance_day(today: str, calendar) -> bool:
    """今日是否双周调仓日(月中或月末倒数第二)。实盘链唯一判定入口。"""
    today = str(today)[:10]
    ym = today[:7]
    mds = _by_month(calendar).get(ym, [])
    if not mds:
        return False
    n = len(mds)
    return today == mds[month_mid_index(n)] or today == mds[month_end_index(n)]


def biweekly_dates(calendar) -> list[str]:
    """双周调仓日列表(月中 + 月末倒数第二), 升序去重。

    供回测使用, 与实盘 is_rebalance_day 逐日等价(同一实现)。
    """
    out: set[str] = set()
    for mds in _by_month(calendar).values():
        n = len(mds)
        if n == 0:
            continue
        if n >= 2:
            out.add(mds[month_mid_index(n)])
        out.add(mds[month_end_index(n)])
    return sorted(out)
