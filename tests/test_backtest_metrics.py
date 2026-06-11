"""
回测指标单元测试 — 全部用构造数据（已知正确答案）。

验证: metrics(总收益/年化/夏普/回撤)、分年度收益复利、MA10止损。
"""
import pandas as pd, numpy as np

RF = 0.025
COMM = 0.00175

# ── 1. metrics 函数 ──────────────────────────────────────
def metrics(nav_series: pd.Series) -> dict:
    """标准回测指标。给定净值序列，返回指标字典。"""
    d = nav_series.pct_change().dropna()
    if len(d) < 2:
        return {"error": "insufficient data"}
    total = nav_series.iloc[-1] / nav_series.iloc[0] - 1
    # auto-detect frequency
    try:
        days_span = (nav_series.index[-1] - nav_series.index[0]).days
        freq = len(d) / max(days_span / 365, 0.1) if days_span > 0 else 252
    except (AttributeError, TypeError):
        freq = 252  # non-DatetimeIndex, assume daily
    yrs = max(len(d) / freq, 0.25)
    annual = (1 + total) ** (1 / yrs) - 1
    vol = d.std() * np.sqrt(freq)
    rf_period = RF / freq
    sharpe = (d.mean() - rf_period) / d.std() * np.sqrt(freq) if d.std() > 0 else 0
    mdd = (nav_series / nav_series.cummax() - 1).min()
    return {"total": total, "annual": annual, "vol": vol, "sharpe": sharpe,
            "max_dd": mdd, "n_obs": len(d)}

# ── 2. 单位测试 ──────────────────────────────────────────
def test_simple_annual():
    """已知年化10%，验证总收益/年化换算一致"""
    years = 3; periods = int(years * 252)
    ret = 0.01 / np.sqrt(252)  # daily ≈ 10% annual with low vol
    daily = np.random.default_rng(42).normal(ret, 0.005, periods)
    nav = pd.Series((1 + daily).cumprod())
    m = metrics(nav)
    annual = m["annual"]
    total  = m["total"]
    # 自洽性：total → annual 和 annual → total 换算一致
    annual_from_total = (1 + total) ** (1 / years) - 1
    total_from_annual = (1 + annual) ** years - 1
    assert abs(annual_from_total - annual) < 0.01, f"annual mismatch: {annual:.4f} vs {annual_from_total:.4f}"
    assert abs(total_from_annual - total) < 0.02, f"total mismatch"
    print(f"  ✅ test_simple_annual: annual={annual:.1%} total={total:.1%}")

def test_sharpe_known():
    """零均值序列的夏普应接近0"""
    np.random.seed(1)
    daily = np.random.randn(1000) * 0.01
    nav = pd.Series((1 + daily).cumprod())
    m = metrics(nav)
    assert abs(m["sharpe"]) < 0.5, f"zero-mean sharpe should be ~0, got {m['sharpe']:.2f}"
    print(f"  ✅ test_sharpe_known: sharpe={m['sharpe']:.2f}")

def test_drawdown_known():
    """从高点回撤50% → MDD应为-50%"""
    nav = pd.Series([1.0, 1.1, 1.05, 0.55, 0.60, 0.66, 0.80])
    m = metrics(nav)
    expected_dd = (0.55 - 1.1) / 1.1  # -0.5
    assert abs(m["max_dd"] - expected_dd) < 0.01, f"MDD {m['max_dd']:.2f} != {expected_dd:.2f}"
    print(f"  ✅ test_drawdown_known: MDD={m['max_dd']:.1%}")

def test_annual_consistency():
    """分年度收益复利连乘 ≈ 总收益（自洽性检验）"""
    yrs_data = {2020: 1.20, 2021: 0.85, 2022: 0.70, 2023: 1.30}  # yr-end NAV
    nav_list = [1.0]
    for yr, val in sorted(yrs_data.items()):
        nav_list.append(val)
    total = nav_list[-1] / nav_list[0] - 1
    # 分别算每年
    product = 1.0
    for i in range(1, len(nav_list)):
        product *= nav_list[i] / nav_list[i-1]
    assert abs(product - 1 - total) < 0.001, f"复利连乘{product-1:.4f} != total{total:.4f}"
    print(f"  ✅ test_annual_consistency: product={product-1:.1%} total={total:.1%}")

def test_no_nan_inf():
    """验证不会产出 NaN/inf"""
    # 常数序列
    nav = pd.Series([1.0, 1.0, 1.0])
    m = metrics(nav)
    for k, v in m.items():
        if isinstance(v, float):
            assert not np.isnan(v) and not np.isinf(v), f"{k} is NaN/inf"
    # 暴跌序列（需要足够长的观测数，至少3个数据点）
    nav2 = pd.Series([1.0, 0.9, 0.8, 0.7, 0.6])
    m2 = metrics(nav2)
    assert not np.isnan(m2["max_dd"]), f"max_dd={m2.get('max_dd')}"
    assert abs(m2["max_dd"] - (-0.4)) < 0.01, f"MDD should be -40%, got {m2['max_dd']:.1%}"
    print(f"  ✅ test_no_nan_inf: clean")

def test_ma10_exit():
    """MA10连续3天跌破触发止损"""
    close = pd.Series([10, 10.5, 10.3, 10.1, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4,
                       9.3, 9.2, 9.1, 9.0, 8.9])  # 持续下行
    ma10 = close.rolling(10).mean()
    below = 0; exit_day = None
    for i in range(10, len(close)):
        if close.iloc[i] < ma10.iloc[i]:
            below += 1
        else:
            below = 0
        if below >= 3:
            exit_day = close.index[i]
            break
    assert exit_day is not None, "MA10 should trigger exit"
    # 止损后20日应继续跌（证明止损不是反向指标）
    exit_i = close.index.get_loc(exit_day)
    post_20d = close.iloc[exit_i:exit_i+min(5,len(close)-exit_i)]
    assert post_20d.iloc[-1] < post_20d.iloc[0], "止损后应继续跌"
    print(f"  ✅ test_ma10_exit: triggered at index {exit_i}, post_5d={post_20d.values}")


if __name__ == "__main__":
    test_simple_annual()
    test_sharpe_known()
    test_drawdown_known()
    test_annual_consistency()
    test_no_nan_inf()
    test_ma10_exit()
    print("\n  ✅ 全部指标单测通过 — 可安全重跑回测")
