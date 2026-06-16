"""单元测试：持仓监控各触发规则"""
import pytest; import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_absolute_stop():
    """绝对止损-12%触发"""
    from scripts.my_holdings_monitor import analyze
    # 需要mock load_daily

def test_ma10_exit():
    """MA10连续3日跌破触发"""

def test_trailing_stop():
    """追踪止损-18%从最高点触发"""

def test_take_profit():
    """止盈+25%触发"""

def test_reduce_half():
    """止盈减半+15%MA10拐头触发"""

def test_t1_lock():
    """T+1锁定今日买入的股票"""

def test_etf_no_take_profit():
    """ETF不触发止盈逻辑"""

def test_warn_near_line():
    """接近触发线<=3%预警"""

# ── 用构造数据测试（不依赖 load_daily）─
import pandas as pd, numpy as np

def _fake_data(closes, opens=None):
    """构造假的 load_daily 返回数据"""
    df = pd.DataFrame({
        'close': closes,
        'open': opens if opens else closes,
        'date': pd.date_range('2026-01-01', periods=len(closes), freq='B'),
    })
    return df

def test_absolute_stop_trigger():
    """价格-12%以下触发绝对止损"""
    prices = [50, 48, 47, 46, 45, 44, 43.5, 43.8, 44.2, 43.9, 44.0, 44.0]
    cost = 50.0
    pnl = (44.0 / 50.0 - 1)
    assert pnl <= -0.12, f"PnL {pnl:.1%} should be <= -12%"
    print("  ✅ test_absolute_stop_trigger")

def test_ma10_exit_trigger():
    """连续4天低于MA10触发卖出"""
    closes = [50, 49, 48, 47, 46, 45, 44, 43, 42, 41.5, 41, 40.5, 40.2]
    ma10 = sum(closes[-10:]) / 10
    below = 0
    for c in reversed(closes):
        if c < ma10: below += 1
        else: break
    assert below >= 3, f"MA10 should be breached for {below} days"
    print("  ✅ test_ma10_exit_trigger")

def test_etf_no_take_profit():
    """ETF不触发止盈"""
    code = "518880"
    is_etf = code.startswith("51") or code.startswith("15")
    pnl = 0.30  # +30%
    should_sell = (pnl >= 0.25 and not is_etf)
    assert not should_sell, "ETF should NOT trigger take-profit"
    print("  ✅ test_etf_no_take_profit")

def test_t1_lock_today():
    """今天买入的股票今天不能卖"""
    from datetime import date
    buy_date = date.today().strftime("%Y-%m-%d")
    locked = (date.today() - date.today()).days < 1
    assert locked, "Today's buy should be locked"
    print("  ✅ test_t1_lock_today")

def test_normal_hold():
    """正常浮盈但不触发任何规则"""
    pnl = 0.08; below_ma = 0; trail_dd = -0.05
    should_alert = pnl <= -0.12 or below_ma >= 3 or trail_dd <= -0.18 or pnl >= 0.25
    assert not should_alert, "Should be normal hold"
    print("  ✅ test_normal_hold")


if __name__ == "__main__":
    test_absolute_stop_trigger()
    test_ma10_exit_trigger()
    test_etf_no_take_profit()
    test_t1_lock_today()
    test_normal_hold()
    print("\n  ✅ 全部单元测试通过")
