"""
策略A-4 修复版本：修正执行顺序的look-ahead bug

原bug：调仓日先选新股 → 再用新持仓算当日收益 = 先知交易
修复：当日收益（旧持仓）→ MA10出清 → 调仓选股（明日生效）

参考：Track B 同bug修复 ec0a68b
"""
import numpy as np
import pandas as pd
from loguru import logger

from run_backtest_a2 import (
    compute_score_a2, select_industry_balanced, compute_weights,
    get_position_ratio, MAX_IND_SLOT, SECTOR_BOOST, MAX_TURNOVER,
)
from run_backtest_a import COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP, MA_PERIOD
from run_backtest_a3 import GRACE_THRESHOLD
from run_backtest_a4 import (
    select_dynamic_grace, MA10_EXIT_DAYS, MA_EXIT_WINDOW, NEW_STOCK_PROTECT,
    N_HOLDINGS,
)


def run_backtest_a4_fixed(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None,
    index_close: pd.Series | None,
    stock_info: pd.DataFrame | None,
    universe_map: dict | None = None,
) -> pd.Series:
    """
    修复版：先算收益再调仓，消除先知交易偏差。
    universe_map: 若传入，每个调仓日动态限制股票池为对应快照。
    """
    all_dates = panel.index
    port_rets = pd.Series(0.0, index=all_dates)

    cur_weights:    dict[str, float] = {}
    entry_prices:   dict[str, float] = {}
    tenure:         dict[str, int]   = {}
    days_below_ma10: dict[str, int]  = {}
    cumul_nav = 1.0
    entry_hwm = 1.0
    nav_since = 1.0
    pos_ratio = 1.0
    rebal_set = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # ═══ Step 1：当日收益（基于昨日收盘持仓 = 今日实际持有） ═══
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i - 1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp / pp - 1)
            port_rets.iloc[i] += ret

        # ═══ Step 2：MA10 连续跌破出清（今日收盘判断，明日不再持有） ═══
        if cur_weights and i >= 10:
            ma10_exits = []
            for code in list(cur_weights.keys()):
                col = panel[code] if code in panel.columns else None
                if col is None:
                    continue
                w = MA_EXIT_WINDOW
                hist_ma = col.iloc[max(0, i - w + 1): i + 1].dropna()
                if len(hist_ma) < max(5, w // 2):
                    continue
                ma_val = hist_ma.mean()
                cur_p = panel.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p is None or cur_p <= 0:
                    continue

                if cur_p < ma_val:
                    days_below_ma10[code] = days_below_ma10.get(code, 0) + 1
                else:
                    days_below_ma10[code] = 0

                if days_below_ma10.get(code, 0) >= MA10_EXIT_DAYS:
                    ma10_exits.append(code)

            if ma10_exits:
                for code in ma10_exits:
                    w = cur_weights.pop(code, 0)
                    port_rets.iloc[i] -= w * COMMISSION
                    entry_prices.pop(code, None)
                    tenure.pop(code, None)
                    days_below_ma10.pop(code, None)
                logger.debug(f"{date_str} MA10出清({MA10_EXIT_DAYS}天): {ma10_exits}")

        # ═══ Step 3：调仓日选股（今日收盘信号，明日生效） ═══
        if date_str in rebal_set and i >= MIN_BARS:
            if universe_map:
                avail = sorted([d for d in universe_map if d <= date_str], reverse=True)
                current_universe = universe_map[avail[0]] if avail else None
            else:
                current_universe = None

            pos_ratio = get_position_ratio(index_close, date) if index_close is not None else 1.0

            if pos_ratio <= 0.30:
                cur_weights = {}
                entry_prices = {}
                tenure = {}
                days_below_ma10 = {}
                nav_since = 1.0
                entry_hwm = cumul_nav
            else:
                if current_universe:
                    active_cols = [c for c in panel.columns if c in current_universe]
                    _panel_u = panel[active_cols]
                    _amt_u = amount_panel[active_cols] if amount_panel is not None else None
                else:
                    _panel_u, _amt_u = panel, amount_panel

                score = compute_score_a2(_panel_u, date, _amt_u, stock_info)
                if len(score) >= N_HOLDINGS:
                    old_hold = list(cur_weights.keys())
                    cur_p_series = panel.ffill().iloc[i]

                    new_hold = select_dynamic_grace(
                        score, old_hold, N_HOLDINGS,
                        entry_prices, cur_p_series, tenure
                    )

                    new_set = set(new_hold)
                    old_set = set(old_hold)
                    for c in old_set & new_set:
                        tenure[c] = tenure.get(c, 0) + 1
                    for c in new_set - old_set:
                        tenure[c] = 0
                        ep = cur_p_series.get(c)
                        if ep and not pd.isna(ep):
                            entry_prices[c] = float(ep)
                    for c in old_set - new_set:
                        entry_prices.pop(c, None)
                        days_below_ma10.pop(c, None)
                        tenure.pop(c, None)

                    raw_w = compute_weights(new_hold, score, stock_info, SECTOR_BOOST)
                    new_w = {c: w * pos_ratio for c, w in raw_w.items()}

                    enter = sum(new_w.get(c, 0) for c in new_set - old_set)
                    exit_ = sum(cur_weights.get(c, 0) for c in old_set - new_set)
                    port_rets.iloc[i] -= (enter + exit_) / 2 * COMMISSION * 2

                    if not cur_weights:
                        entry_hwm = cumul_nav
                    cur_weights = new_w  # 新持仓明日生效
                nav_since = 1.0

        # ═══ Step 4：组合级止损 ═══
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights = {}
                entry_prices = {}
                tenure = {}
                days_below_ma10 = {}
                nav_since = 1.0
                entry_hwm = cumul_nav

        # ═══ Step 5：现金计息 ═══
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD / 252

        nav_since = nav_since * (1 + port_rets.iloc[i])
        cumul_nav = cumul_nav * (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()
