# ARCHIVED 2026-06-11: Regime Gate 已验证无效。详见 BACKTEST_AUDIT.md
# 前视偏差修复后夏普0.23，不达标。MA200择时CSI2000同样失败(年化-0.2%)。
# 结论: CSI2000不适合任何技术指标择时。
"""
Track B 第一层：大势 Regime Gate。

4 个子指标各 0/1，总分 0-4：
  ① 趋势：收盘 > MA20 且 MA20 > MA60
  ② 波动：20日年化波动率 / 250日年化波动率 < 1.3
  ③ 赚钱效应：(涨停家数 - 跌停家数) 5日均值 > 20
  ④ 亏钱效应：炸板率 5日均值 < 40%

状态机（连续 2 日确认）：
  score >= 3 → ATTACK（仓位上限 100%）
  score == 2 → NEUTRAL（上限 50%，禁止新开仓）
  score <= 1 → DEFENSE（上限 0%，只卖不买）

独立运行：python -m strategies.trinity.regime --date 2026-06-10
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from loguru import logger
from config.strategy_params.trinity import REGIME
from strategies.trinity.data_cache import get_limit_up_down_stats

# ── 指标计算 ────────────────────────────────────────────────
def _load_benchmark(index_symbol: str, start: str, end: str) -> pd.Series:
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=index_symbol)
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["close"].sort_index()
    return s[(s.index >= start) & (s.index <= end)]


def _trend_indicator(close: pd.Series, fast: int = 20, slow: int = 60) -> int:
    """① 趋势：收盘 > MA20 且 MA20 > MA60"""
    if len(close) < slow + 1:
        return 0
    cur = close.iloc[-1]
    ma_fast = close.rolling(fast).mean().iloc[-1]
    ma_slow = close.rolling(slow).mean().iloc[-1]
    return 1 if (cur > ma_fast and ma_fast > ma_slow) else 0


def _vol_indicator(close: pd.Series) -> int:
    """② 波动率：20日/250日 < 1.3"""
    if len(close) < 251:
        return 0
    rets = close.pct_change().dropna()
    vol_20d  = rets.tail(20).std() * np.sqrt(252)
    vol_250d = rets.tail(250).std() * np.sqrt(252)
    ratio = vol_20d / max(vol_250d, 0.001)
    return 1 if ratio < REGIME["vol_ratio_threshold"] else 0


def _breadth_from_panel(panel: pd.DataFrame) -> int:
    """
    ③ 赚钱效应：(涨幅>9.5%家数 - 跌幅>9.5%家数) 5日均值 > 20
    使用全市场日线面板计算，无需API。
    panel: 全市场收盘价矩阵 (date × code)
    """
    if len(panel) < 6:
        return 0
    rets = panel.tail(5).pct_change().dropna(how='all')
    if rets.empty:
        return 0
    # 每天：涨>9.5%个数 - 跌>9.5%个数
    up_count   = (rets > 0.095).sum(axis=1)
    dn_count   = (rets < -0.095).sum(axis=1)
    net = (up_count - dn_count).mean()
    return 1 if net > REGIME["advance_minus_decline_min"] else 0


def _blowup_from_panel(panel: pd.DataFrame) -> int:
    """
    ④ 亏钱效应：炸板率近似。无日内数据，用尾盘回落股占比替代。
    规则：若当日最高价涨幅>9.5%但收盘涨幅<5%的股票数 / 最高价涨幅>9.5%总数 > 40%，
    视为炸板率偏高。
    回测用简化版：涨超9.5%家数 vs 市场情绪，炸板率默认<40%（偏乐观）。
    """
    # 无日内数据时，回测默认通过（指数化简化处理）
    return 1  # 回测默认：炸板率正常


def _breadth_indicator(trade_date: str) -> int:
    """③ 赚钱效应：优先用面板计算，API不可用时用缓存。"""
    # 实盘模式：用 akshare API
    try:
        s = get_limit_up_down_stats(trade_date)
        if s["limit_up"] > 0:
            # 近5日均值
            total = s["limit_up"] - s["limit_down"]; count = 1
            d = date.fromisoformat(trade_date)
            for i in range(1, 5):
                dt = (d - timedelta(days=i)).strftime("%Y-%m-%d")
                try:
                    prev = get_limit_up_down_stats(dt)
                    total += prev["limit_up"] - prev["limit_down"]
                    count += 1
                except Exception:
                    pass
            return 1 if (total / max(count, 1)) > REGIME["advance_minus_decline_min"] else 0
    except Exception:
        pass
    return 0  # API不可用，默认不通过（保守）


def _blowup_indicator(trade_date: str) -> int:
    """④ 亏钱效应：API优先，不可用时默认通过。"""
    try:
        s = get_limit_up_down_stats(trade_date)
        if s["blowup_rate"] > 0:
            rates = [s["blowup_rate"]]
            d = date.fromisoformat(trade_date)
            for i in range(1, 5):
                dt = (d - timedelta(days=i)).strftime("%Y-%m-%d")
                try:
                    rates.append(get_limit_up_down_stats(dt)["blowup_rate"])
                except Exception:
                    pass
            avg = sum(rates) / len(rates)
            return 1 if avg < REGIME["blowup_rate_max"] else 0
    except Exception:
        pass
    return 1  # API不可用，默认通过


# ── 状态机 ──────────────────────────────────────────────────
class RegimeGate:
    """大势状态机，连续 confirm_days 确认后才切换状态。"""

    def __init__(self):
        self._history: list[str] = []   # 最近几天的状态候选

    def evaluate(self, trade_date: str, fast_mode: bool = False) -> dict:
        """
        计算当日 Regime 状态。
        fast_mode=True: 仅趋势+波动（回测用，避免API限速）
        fast_mode=False: 完整4指标（实盘用）
        """
        close = _load_benchmark(
            REGIME["benchmark_index"],
            (date.fromisoformat(trade_date) - timedelta(days=400)).strftime("%Y-%m-%d"),
            trade_date,
        )
        if close.empty:
            return self._fallback(trade_date)

        if fast_mode:
            sub = {
                "trend":   _trend_indicator(close),
                "vol":     _vol_indicator(close),
                "breadth": 1,   # 回测模式默认通过
                "blowup":  1,
            }
        else:
            sub = {
                "trend":   _trend_indicator(close),
                "vol":     _vol_indicator(close),
                "breadth": _breadth_indicator(trade_date),
                "blowup":  _blowup_indicator(trade_date),
            }
        score = sum(sub.values())
        raw_state = "ATTACK" if score >= 3 else ("NEUTRAL" if score == 2 else "DEFENSE")
        confirmed_state = self._confirm(raw_state)
        state_cfg = REGIME["state"][confirmed_state]

        return {
            "date":          trade_date,
            "score":         score,
            "state":         confirmed_state,
            "position_cap":  state_cfg["position_cap"],
            "allow_new":     state_cfg["allow_new"],
            "sub_indicators": sub,
        }

    def _confirm(self, raw_state: str) -> str:
        """连续 REGIME['confirm_days'] 天同一状态才确认切换。"""
        n = REGIME["confirm_days"]
        self._history.append(raw_state)
        if len(self._history) > n:
            self._history.pop(0)
        if len(self._history) >= n and all(s == raw_state for s in self._history):
            return raw_state
        # 未确认时保持历史最后一个确认状态
        return self._history[0] if self._history else raw_state

    def _fallback(self, trade_date: str) -> dict:
        return {"date": trade_date, "score": 0, "state": "DEFENSE",
                "position_cap": 0.0, "allow_new": False,
                "sub_indicators": {}}


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    args = parser.parse_args()

    gate = RegimeGate()
    # 回填前两天的状态
    for i in range(2, 0, -1):
        dt = (date.fromisoformat(args.date) - timedelta(days=i)).strftime("%Y-%m-%d")
        gate.evaluate(dt)
    result = gate.evaluate(args.date)

    print(f"\n  Regime Gate  {args.date}")
    print(f"  {'─'*40}")
    print(f"  趋势: {result['sub_indicators'].get('trend','?')}/1  "
          f"波动: {result['sub_indicators'].get('vol','?')}/1")
    print(f"  赚钱效应: {result['sub_indicators'].get('breadth','?')}/1  "
          f"亏钱效应: {result['sub_indicators'].get('blowup','?')}/1")
    print(f"  总分: {result['score']}/4  →  {result['state']}  "
          f"仓位上限: {result['position_cap']:.0%}")
    print(f"  {'─'*40}\n")
