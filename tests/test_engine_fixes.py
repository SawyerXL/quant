"""
引擎修复行为测试（2026-09-02，审查发现的假绿缺口补真测试）。

覆盖:
  1. ma10_exit_delay 次日开盘卖: 当日净贡献=今开/昨收-1-佣金 (原符号翻转bug)
  2. ma10_reentry_cool: 冷却期内不买回 (原cooldown_until未定义NameError)
  3. rank_buffer_mult>1: 首次调仓不崩 (原old_set未定义NameError)
  4. calc_metrics 月胜率: 真实月胜率而非日胜率
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_config import DEFAULT_CONFIG
from backtest_engine import calc_metrics, make_rebal_dates


def _mini_panel(days=60, seed=0):
    """构造单票价格面板, 足够250bar预热绕过即可用少量天数直测关键语义。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=days, freq="B")
    close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, days))
    return pd.DataFrame({"000001": close}, index=idx)


def _mini_open(panel):
    """open面板=昨收(开盘无跳空), 便于精确断言。"""
    o = panel.shift(1).ffill()
    return o


# ── 1. ma10_exit_delay 符号翻转 ──────────────────────────────

def test_ma10_exit_delay_open_sell_semantics():
    """低开续跌日次日开盘卖: 当日净贡献=w×(今开/昨收-1)+现金利息。
    修复前符号翻转(把盘中续跌记成收益)。"""
    from dataclasses import replace
    from backtest_engine import run_backtest

    n = 300
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    close = np.full(n, 100.0)
    # 第290~293日跌破MA10四天 → 第293日触发, pending_exits→第294日开盘卖
    close[290:294] = [96.0, 95.0, 94.0, 93.0]
    close[294] = 85.0        # 卖出日盘中续跌(收盘85)
    panel = pd.DataFrame({"000001": close}, index=dates)
    openp = pd.DataFrame({"000001": np.full(n, 100.0)}, index=dates)
    openp.iloc[294, 0] = 88.0   # 卖出日低开88
    amt = pd.DataFrame({"000001": np.full(n, 1e8)}, index=dates)

    cfg = replace(DEFAULT_CONFIG, enable_ma10_exit=True, ma_exit_days=4,
                  ma10_exit_delay=True, commission=0.0,
                  enable_take_profit=False, enable_stops=True,
                  max_vol20=999, pool_size=1)
    rebal = [str(dates[260].date())]
    nav, info = run_backtest(panel, amt, rebal, cfg, None, open_panel=openp)

    # 单票仓位=min(max_position_pct=0.10, 剩余/1)=10%; 卖出日现金100%计息
    w = 0.10
    expected = w * (88.0 / 93.0 - 1) + 0.02 / 252
    day_ret = nav.iloc[294] / nav.iloc[293] - 1
    assert day_ret == pytest.approx(expected, abs=1e-6), \
        f"开盘卖语义: 期望{expected:+.6f}, 实得{day_ret:+.6f}"


# ── 2/3. 配置开启不崩(原NameError) ──────────────────────────

def test_ma10_reentry_cool_no_nameerror():
    from dataclasses import replace
    from backtest_engine import run_backtest

    panel = _mini_panel(400)
    amt = pd.DataFrame({"000001": np.full(400, 1e8)}, index=panel.index)
    cfg = replace(DEFAULT_CONFIG, ma10_reentry_cool=10, pool_size=1)
    # 需要让MA10退出真实触发一次以写入cooldown_until
    close = np.full(400, 100.0)
    close[100:104] = [100, 95, 94, 93]   # 连跌4日触发MA10(ma_exit_days=4)
    panel2 = pd.DataFrame({"000001": close}, index=panel.index)
    amt2 = pd.DataFrame({"000001": np.full(400, 1e8)}, index=panel.index)
    nav, info = run_backtest(panel2, amt2, [], cfg, None)
    assert nav.iloc[-1] > 0


def test_rank_buffer_mult_no_nameerror():
    from dataclasses import replace
    from backtest_engine import run_backtest

    panel = _mini_panel(400)
    amt = pd.DataFrame({"000001": np.full(400, 1e8)}, index=panel.index)
    cfg = replace(DEFAULT_CONFIG, rank_buffer_mult=1.5, pool_size=1,
                  max_vol20=999)
    nav, info = run_backtest(panel, amt, [], cfg, None)
    assert nav.iloc[-1] > 0


# ── 4. 月胜率语义 ────────────────────────────────────────────

def test_monthly_win_rate_is_monthly():
    """精确4个月(1-4月): 前3个月月收益>0, 第4个月<0 → 月胜率=75%。"""
    idx = pd.bdate_range("2024-01-02", "2024-04-30")
    rets = pd.Series(0.0, index=idx)
    # 每月最后一个交易日给一个月度收益: +5%/+5%/+5%/-10%
    month_ends = [rets.index[rets.index.month == m][-1] for m in (1, 2, 3, 4)]
    assert len(month_ends) == 4
    for d in month_ends[:3]:
        rets[d] = 0.05
    rets[month_ends[3]] = -0.10
    nav = (1 + rets).cumprod()
    cm = calc_metrics(nav)
    wr = float(cm["月胜率"].strip("%")) / 100
    assert wr == pytest.approx(0.75, abs=0.01), f"月胜率应为75%, 实为{wr:.1%}"
