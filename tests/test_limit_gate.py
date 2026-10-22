"""涨跌停闸单元测试 (2026-09-29, F区P1 代码准备件)。
测: ①limit_status 板块幅度/封板判定/停牌昨收回退 ②引擎冒烟: limit_gate
默认False 与 True 的行为差异(涨停买跳过/跌停卖顺延) ③零行为变更断言
(gate=False 与旧引擎逐位一致——由 ② 的基线臂保证)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pandas as pd
import numpy as np

from backtest_engine import limit_status


def _panel(closes):
    return pd.DataFrame({"000001": closes["000001"],
                         "300001": closes["300001"],
                         "688001": closes["688001"]},
                        index=pd.date_range("2026-01-01", periods=len(closes["000001"])))


def test_limit_status_boards():
    # 主板10%: 昨收10.00 → 涨停11.00 跌停9.00
    p = _panel({"000001": [10.0, 11.0], "300001": [10.0, 10.0], "688001": [10.0, 10.0]})
    assert limit_status(p, 1, "000001") == "up"
    p2 = _panel({"000001": [10.0, 9.0], "300001": [10.0, 10.0], "688001": [10.0, 10.0]})
    assert limit_status(p2, 1, "000001") == "down"
    # 创业板20%: 11.99 未触板(涨停=12.00)
    p3 = _panel({"000001": [10.0, 10.0], "300001": [10.0, 11.99], "688001": [10.0, 10.0]})
    assert limit_status(p3, 1, "300001") is None
    # 科创板20%: 12.00 触板
    p4 = _panel({"000001": [10.0, 10.0], "300001": [10.0, 10.0], "688001": [10.0, 12.0]})
    assert limit_status(p4, 1, "688001") == "up"
    # 停牌昨收回退: [10, NaN, 11.0] 昨收=10 → 涨停
    p5 = _panel({"000001": [10.0, np.nan, 11.0], "300001": [10.0, 10.0, 10.0],
                  "688001": [10.0, 10.0, 10.0]})
    assert limit_status(p5, 2, "000001") == "up"


def test_limit_gate_smoke():
    """引擎冒烟: gate=True 时涨停买入被跳过、触发计数>0; gate=False 基线不报。"""
    from backtest_engine import run_backtest
    from backtest_config import DEFAULT_CONFIG
    from dataclasses import replace

    n = 40
    idx = pd.date_range("2026-01-01", periods=n)
    closes = {c: (100.0 + np.sin(np.arange(n) / 3) * 5).round(2)
              for c in ["000001", "000002", "000003"]}
    # 冲突①: 000003 在首个调仓日(i=5)涨停——当日新买入候选(池内第一)
    closes["000003"][5] = round(closes["000003"][4] * 1.10, 2)
    # 冲突②: 000001 在第三调仓日(i=15)跌停, 且此时已跌出 selected[:2]
    # (amount 自 i=10 掉到 0.2e8 → 20日均值在 i=15 落后 000002, 轮出)
    closes["000001"][15] = round(closes["000001"][14] * 0.90, 2)
    panel = pd.DataFrame(closes, index=idx)
    amounts = pd.DataFrame(1e8, index=idx, columns=list(closes))
    amounts["000003"] = 3e8
    amounts.loc[idx[:10], "000001"] = 2e8
    amounts.loc[idx[10:], "000001"] = 0.2e8
    amounts.loc[idx[10:], "000002"] = 2.5e8

    # 覆盖调仓日: 每5日一次
    rebal = [str(d.date()) for d in idx[5::5]]

    cfg_base = replace(DEFAULT_CONFIG, pool_size=2, min_bars=5,
                       lot_size=0, enable_stops=False, limit_gate=False)
    nav_base, res_base = run_backtest(panel, amounts, rebal, cfg_base, None)
    cfg_gate = replace(cfg_base, limit_gate=True)
    nav_gate, res_gate = run_backtest(panel, amounts, rebal, cfg_gate, None)

    # 买侧: 涨停买入被跳过; 卖侧: 跌停轮出被顺延——两计数都须触发
    assert res_gate["limit_up_blocked"] >= 1, "涨停跳过未触发(0触发=未验证)"
    assert res_gate["limit_down_blocked"] >= 1, "跌停顺延未触发(0触发=未验证)"
    assert res_base["limit_up_blocked"] == 0, "基线不得报涨跌停触发"
    assert res_base["limit_down_blocked"] == 0, "基线不得报涨跌停触发"
    # 零行为变更: 两臂 NAV 在首个调仓日(i=5)之前逐位一致
    common = nav_base.index[:5]
    assert (nav_base.loc[common] == nav_gate.loc[common]).all(), \
        "limit_gate 不应改变无冲突日的行为"
    # 闸门产生 NAV 差异(基线买涨停股/卖跌停股, gate 臂跳过/顺延)
    assert not np.isclose(nav_base.iloc[-1], nav_gate.iloc[-1]), \
        "涨跌停闸应产生 NAV 差异(否则闸门无效果)"


if __name__ == "__main__":
    test_limit_status_boards()
    print("limit_status 板块判定 PASS")
    test_limit_gate_smoke()
    print("引擎冒烟(涨停跳过/零行为变更) PASS")
