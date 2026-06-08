"""
策略A-4：在A-3基础上的两项改进

改进1：保护期改为动态（浮盈自由换，浮亏保护）
  原版：持仓 <2期 → 统一15%门槛
  新版：浮盈股 → 门槛=0（随时可换，已验证方向正确）
       浮亏股 → 门槛=15%（需要时间恢复，继续保护）

改进2：MA10 连续3天跌破出清（技术弱化信号）
  规则：持仓股收盘价连续3个交易日低于10日均线 → 主动清仓
  意义：
    - 强势股的标志是"在10日线上方运行"
    - 跌破10日线3天不能收复 → 走势已弱，主动离场
    - 防止深套：在 -15%/-18% 止损触发前更早退出
    - 减少跌停时的滑点损失（更早的信号，在跌停前出场）

  三层止损（从早到晚）：
    MA10 连续3天跌破（最早）→ 技术弱化主动退出
    追踪止损 -18%（中间）    → 从最高点回撤
    期内止损 -15%（最晚）    → 两周内总跌幅兜底

运行：
    python scripts/run_backtest_a4.py
    python scripts/run_backtest_a4.py --compare    # 与A-3对比
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta

from run_backtest_a2 import (
    compute_score_a2, select_industry_balanced, compute_weights,
    get_position_ratio, _make_rebal_dates,
    MAX_IND_SLOT, SECTOR_BOOST, MAX_TURNOVER,
)
from run_backtest_a import (
    load_panels, calc_metrics,
    BACKTEST_START, COMMISSION, MIN_BARS, CASH_YIELD,
    PERIOD_STOP, TRAILING_STOP, MA_PERIOD,
)
# 复用A-3的参数
from run_backtest_a3 import (
    GRACE_THRESHOLD,
)

logger.add("logs/backtest_a4.log", rotation="1 day", retention="30 days")

BACKTEST_END     = os.getenv("BACKTEST_END", "")
N_HOLDINGS       = int(os.getenv("N_HOLDINGS", "30"))
USE_REGIME       = os.getenv("USE_REGIME", "1") == "1"
REBAL_FREQ       = os.getenv("REBAL_FREQ", "biweekly")

MA10_EXIT_DAYS   = int(os.getenv("MA10_EXIT_DAYS", "3"))    # 连续跌破MA几天出清
MA_EXIT_WINDOW    = int(os.getenv("MA_EXIT_WINDOW", "10"))   # MA窗口（天）


NEW_STOCK_PROTECT = int(os.getenv("NEW_STOCK_PROTECT", "2"))  # 新股保护周期数


def select_dynamic_grace(
    score: pd.Series,
    current_holdings: list[str],
    n: int,
    entry_prices: dict[str, float],
    current_prices: pd.Series,
    tenure: dict[str, int] | None = None,
) -> list[str]:
    """
    动态保护期选股（含新股保护）：
    - 新股（持有 < NEW_STOCK_PROTECT 个周期）：统一15%门槛，不管浮盈浮亏
      → 防止追高买入后马上被换掉（5/28新买21只81%亏的根因修复）
    - 老股浮盈 → 无门槛，随时可换
    - 老股浮亏 → 替换方需得分高出 15% 才能换出
    """
    if not current_holdings:
        return select_industry_balanced(score, None, n, MAX_IND_SLOT)

    wider_n    = min(int(n * 1.5), len(score))
    normal_top = set(score.nlargest(n).index.tolist())
    cur_set    = set(current_holdings)
    tenure     = tenure or {}

    remove_cands = sorted(
        [c for c in current_holdings if c not in normal_top],
        key=lambda c: score.get(c, -np.inf)
    )[:MAX_TURNOVER]   # 单次换手上限：最多换出15只
    add_cands = sorted(
        [c for c in score.nlargest(wider_n).index if c not in cur_set],
        key=lambda c: score.get(c, -np.inf), reverse=True
    )[:MAX_TURNOVER]

    result = list(cur_set)

    for rm, add in zip(remove_cands, add_cands):
        if rm not in result:
            continue
        rm_score  = score.get(rm,  -np.inf)
        add_score = score.get(add, -np.inf)

        # ── 动态门槛 ──
        entry = entry_prices.get(rm)
        cur_p = current_prices.get(rm) if hasattr(current_prices, 'get') else None
        if cur_p is None:
            cur_p = current_prices.iloc[-1].get(rm) if hasattr(current_prices, 'iloc') else None

        is_new   = tenure.get(rm, 999) < NEW_STOCK_PROTECT
        is_profit = (entry is not None and cur_p is not None and float(cur_p) >= float(entry))

        if is_new:
            threshold = GRACE_THRESHOLD          # 新股强制保护
        elif is_profit:
            threshold = 0.0                       # 老股浮盈，自由换
        else:
            threshold = GRACE_THRESHOLD           # 老股浮亏

        if threshold == 0.0:
            can_replace = (add_score > rm_score) and (add not in result)
        else:
            needed = rm_score * (1 + threshold) if rm_score > 0 else rm_score
            can_replace = (add_score >= needed) and (add not in result)

        if can_replace:
            result.remove(rm)
            result.append(add)

    # 补足至 N
    for c in score.sort_values(ascending=False).index:
        if len(result) >= n:
            break
        if c not in result:
            result.append(c)

    return result[:n]


def run_backtest_a4(
    panel: pd.DataFrame,
    rebalance_dates: list,
    amount_panel: pd.DataFrame | None,
    index_close: pd.Series | None,
    stock_info: pd.DataFrame | None,
    universe_map: dict | None = None,   # {date_str: set(codes)}，历史宇宙快照
) -> pd.Series:
    """
    universe_map: 若传入，每个调仓日动态限制股票池为对应快照（修复幸存者偏差）。
    """
    all_dates = panel.index
    port_rets = pd.Series(0.0, index=all_dates)

    cur_weights:    dict[str, float] = {}
    entry_prices:   dict[str, float] = {}   # code → 入场收盘价
    tenure:         dict[str, int]   = {}   # code → 已持有个周期数（用于新股保护）
    days_below_ma10: dict[str, int]  = {}   # code → 连续跌破MA10天数
    cumul_nav = 1.0
    entry_hwm = 1.0
    nav_since = 1.0
    pos_ratio = 1.0
    rebal_set = set(str(d.date()) if hasattr(d, "date") else d for d in rebalance_dates)

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # ── Step 1：MA10 连续跌破出清（早于止损，防深套）─────────────
        if cur_weights and i >= 10:
            ma10_exits = []
            for code in list(cur_weights.keys()):
                # 计算该股10日均线（包含今日）
                col = panel[code] if code in panel.columns else None
                if col is None:
                    continue
                w = MA_EXIT_WINDOW
                hist_ma = col.iloc[max(0, i - w + 1): i + 1].dropna()
                if len(hist_ma) < max(5, w // 2):
                    continue
                ma_val = hist_ma.mean()
                cur_p    = panel.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p is None or cur_p <= 0:
                    continue

                if cur_p < ma_val:
                    days_below_ma10[code] = days_below_ma10.get(code, 0) + 1
                else:
                    days_below_ma10[code] = 0   # 价格回到10日线上方，重置计数器

                if days_below_ma10.get(code, 0) >= MA10_EXIT_DAYS:
                    ma10_exits.append(code)

            if ma10_exits:
                for code in ma10_exits:
                    w = cur_weights.pop(code, 0)
                    port_rets.iloc[i] -= w * COMMISSION   # 单边卖出手续费
                    entry_prices.pop(code, None)
                    tenure.pop(code, None)
                    days_below_ma10.pop(code, None)
                logger.debug(f"{date_str} MA10出清({MA10_EXIT_DAYS}天): {ma10_exits}")

        # ── Step 2：调仓日选股 ─────────────────────────────────────
        if date_str in rebal_set and i >= MIN_BARS:
            # 动态宇宙：找最近的历史快照（≤ 当前日期）
            if universe_map:
                avail = sorted([d for d in universe_map if d <= date_str], reverse=True)
                current_universe = universe_map[avail[0]] if avail else None
            else:
                current_universe = None

            pos_ratio = get_position_ratio(index_close, date) if index_close is not None else 1.0

            if pos_ratio <= 0.30:
                cur_weights  = {}
                entry_prices = {}
                tenure       = {}
                days_below_ma10 = {}
                nav_since = 1.0
                entry_hwm = cumul_nav
            else:
                # 若有历史宇宙，将面板临时限制到当期快照
                if current_universe:
                    active_cols = [c for c in panel.columns if c in current_universe]
                    _panel_u = panel[active_cols]
                    _amt_u   = amount_panel[active_cols] if amount_panel is not None else None
                else:
                    _panel_u, _amt_u = panel, amount_panel

                score = compute_score_a2(_panel_u, date, _amt_u, stock_info)
                if len(score) >= N_HOLDINGS:
                    old_hold = list(cur_weights.keys())
                    cur_p_series = panel.ffill().iloc[i]   # 当期价格（ffill）

                    # ★ 动态保护期选股
                    new_hold = select_dynamic_grace(
                        score, old_hold, N_HOLDINGS,
                        entry_prices, cur_p_series, tenure
                    )

                    # 更新tenure：老股+1，新股=0
                    new_set = set(new_hold)
                    old_set = set(old_hold)
                    for c in old_set & new_set:
                        tenure[c] = tenure.get(c, 0) + 1
                    for c in new_set - old_set:
                        tenure[c] = 0
                        ep = cur_p_series.get(c)
                        if ep and not pd.isna(ep):
                            entry_prices[c] = float(ep)
                    # 清除已离场的
                    for c in old_set - new_set:
                        entry_prices.pop(c, None)
                        days_below_ma10.pop(c, None)
                        tenure.pop(c, None)

                    # 权重
                    raw_w = compute_weights(new_hold, score, stock_info, SECTOR_BOOST)
                    new_w = {c: w * pos_ratio for c, w in raw_w.items()}

                    # 换手成本
                    enter = sum(new_w.get(c, 0) for c in new_set - old_set)
                    exit_ = sum(cur_weights.get(c, 0) for c in old_set - new_set)
                    port_rets.iloc[i] -= (enter + exit_) / 2 * COMMISSION * 2

                    if not cur_weights:
                        entry_hwm = cumul_nav
                    cur_weights = new_w
                nav_since = 1.0

        # ── Step 3：组合级止损（-15% 期内 / -18% 追踪）─────────────
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights  = {}
                entry_prices = {}
                tenure       = {}
                days_below_ma10 = {}
                nav_since = 1.0
                entry_hwm = cumul_nav

        # ── Step 4：日收益 ────────────────────────────────────────
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i - 1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp / pp - 1)
            port_rets.iloc[i] += ret

        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD / 252

        nav_since = nav_since * (1 + port_rets.iloc[i])
        cumul_nav = cumul_nav * (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true", help="同时运行A-3做对比")
    args = parser.parse_args()

    global BACKTEST_END
    cal_df = load_meta("trade_calendar")
    if not BACKTEST_END:
        end_candidates = [d for d in sorted(cal_df["trade_date"].tolist()) if d <= "2026-12-31"]
        BACKTEST_END   = end_candidates[-1] if end_candidates else "2024-12-31"

    logger.info("=" * 68)
    logger.info(f"策略A-4  {BACKTEST_START} → {BACKTEST_END}")
    logger.info(f"动态保护（浮盈自由换/浮亏15%门槛）+ MA10连续{MA10_EXIT_DAYS}天出清")
    logger.info("=" * 68)

    calendar    = [d for d in cal_df["trade_date"].tolist() if BACKTEST_START <= d <= BACKTEST_END]
    rebal_dates = _make_rebal_dates(calendar, REBAL_FREQ)
    logger.info(f"调仓日期：{len(rebal_dates)} 个")

    # 股票池：若有历史宇宙则用全量股票（历史宇宙在回测中动态切换）
    use_hist_universe = os.getenv("USE_HIST_UNIVERSE", "0") == "1"
    hist_universe = load_meta("universe_history") if use_hist_universe else pd.DataFrame()

    if use_hist_universe and not hist_universe.empty:
        # 合并所有历史时点出现过的股票，作为面板加载范围
        all_hist_codes = set()
        for _, row in hist_universe.iterrows():
            if row.get("codes"):
                all_hist_codes.update(row["codes"].split(","))
        codes_to_load = sorted(all_hist_codes)
        logger.info(f"历史宇宙模式：加载 {len(codes_to_load)} 只（含历史退市）")
    else:
        csi800 = load_meta("csi800")
        codes_to_load = sorted(csi800["code"].tolist())
        if use_hist_universe:
            logger.warning("未找到 universe_history，回退到当前 CSI 800")

    panel, ap = load_panels(codes_to_load, BACKTEST_START, BACKTEST_END)
    logger.info(f"价格矩阵：{panel.shape[0]}天 × {panel.shape[1]}只")

    # 预建历史宇宙查询表 {日期字符串 -> set(codes)}
    if use_hist_universe and not hist_universe.empty:
        hist_universe_map = {}
        for _, row in hist_universe.iterrows():
            if row.get("codes"):
                hist_universe_map[row["date"]] = set(row["codes"].split(","))
        logger.info(f"历史宇宙快照: {len(hist_universe_map)} 个时点")

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        index_close = None
    else:
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        index_close = idx_df.set_index("date")["close"].sort_index()

    logger.info("运行A-4回测...")
    umap = hist_universe_map if (use_hist_universe and 'hist_universe_map' in dir()) else None
    nav_a4 = run_backtest_a4(panel, rebal_dates, ap, index_close, stock_info,
                             universe_map=umap)

    logger.info("── A-4 分年度 ──")
    year_end = int(BACKTEST_END[:4])
    for yr in range(int(BACKTEST_START[:4]), year_end + 1):
        yn = nav_a4[nav_a4.index.year == yr]
        if len(yn) < 2: continue
        ret = yn.iloc[-1] / yn.iloc[0] - 1
        mdd = ((yn - yn.cummax()) / yn.cummax()).min()
        logger.info(f"  {yr}  收益:{ret:+.1%}  最大回撤:{mdd:.1%}")

    m4 = calc_metrics(nav_a4)
    logger.info("── A-4 总体 ──")
    for k, v in m4.items():
        logger.info(f"  {k}: {v}")

    ar = float(m4["年化收益率"].strip("%")) / 100
    md = float(m4["最大回撤"].strip("%")) / 100
    sr = float(m4["夏普比率"])
    ok = ar >= 0.15 and md >= -0.25 and sr >= 1.0
    logger.info(f"\n结论: {'✅ 达标' if ok else '⚠️ 未达标'}  年化:{ar:.1%}  回撤:{md:.1%}  夏普:{sr:.2f}")

    if args.compare:
        logger.info("\n── A-3 vs A-4 对比 ──")
        from run_backtest_a3 import run_backtest_a3, GRACE_PERIODS
        logger.info("运行A-3对比...")
        nav_a3 = run_backtest_a3(panel, rebal_dates, ap, index_close, stock_info)
        m3 = calc_metrics(nav_a3)
        logger.info(f"A-3  年化:{m3['年化收益率']}  夏普:{m3['夏普比率']}  回撤:{m3['最大回撤']}")
        logger.info(f"A-4  年化:{m4['年化收益率']}  夏普:{m4['夏普比率']}  回撤:{m4['最大回撤']}")

    nav_a4.to_csv(f"logs/backtest_a4_nav.csv", header=["nav"])
    logger.info(f"净值已保存 → logs/backtest_a4_nav.csv")
    logger.info("=" * 68)


if __name__ == "__main__":
    main()
