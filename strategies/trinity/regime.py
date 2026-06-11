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


def _breadth_indicator(trade_date: str) -> int:
    """③ 赚钱效应：(涨停-跌停) 5日均值 > 20"""
    total = 0; count = 0
    d = date.fromisoformat(trade_date)
    for i in range(5):
        dt = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            s = get_limit_up_down_stats(dt)
            total += s["limit_up"] - s["limit_down"]
            count += 1
        except Exception:
            pass
    if count == 0:
        return 0
    return 1 if (total / count) > REGIME["advance_minus_decline_min"] else 0


def _blowup_indicator(trade_date: str) -> int:
    """④ 亏钱效应：炸板率 5日均值 < 40%"""
    rates = []
    d = date.fromisoformat(trade_date)
    for i in range(5):
        dt = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            s = get_limit_up_down_stats(dt)
            rates.append(s["blowup_rate"])
        except Exception:
            pass
    if not rates:
        return 0
    return 1 if (sum(rates) / len(rates)) < REGIME["blowup_rate_max"] else 0


# ── 状态机 ──────────────────────────────────────────────────
class RegimeGate:
    """大势状态机，连续 confirm_days 确认后才切换状态。"""

    def __init__(self):
        self._history: list[str] = []   # 最近几天的状态候选

    def evaluate(self, trade_date: str) -> dict:
        """
        计算当日 Regime 状态。
        返回: {date, score, state, position_cap, sub_indicators}
        """
        close = _load_benchmark(
            REGIME["benchmark_index"],
            (date.fromisoformat(trade_date) - timedelta(days=400)).strftime("%Y-%m-%d"),
            trade_date,
        )
        if close.empty:
            return self._fallback(trade_date)

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
