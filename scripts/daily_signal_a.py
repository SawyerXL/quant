"""
Track A 每日信号生成脚本（策略A-4版）。

策略A-4 = A-3 + 两项改进：

【改进1】动态保护期（浮盈自由换，浮亏保护）
  原版：持仓 <2期 → 统一15%门槛
  新版：浮盈股 → 门槛=0（随时可换）
       浮亏股 → 门槛=15%（保护，防旋转门）

【改进2】MA10 连续3天跌破出清（技术弱化信号）
  每个交易日检查所有持仓，不仅限于调仓日。
  连续3天收盘价低于10日均线 → 主动出清，防深套。

三层止损（从早到晚）：
  MA10 连续3天跌破 → 技术弱化主动退出
  追踪止损 -18%    → 实盘由 trader.py 监控
  期内止损 -15%    → 实盘由 trader.py 监控

运行时机（cron）：
    25 14 * * 1-5  → 每个交易日 14:25 运行
    14:25 生成信号 → 人工确认 → 14:57 前提交竞价收盘委托
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import date, timedelta

import numpy as np
import pandas as pd
from loguru import logger

from run_backtest_a2 import (
    compute_score_a2, compute_weights,
    get_position_ratio, MAX_IND_SLOT, SECTOR_BOOST,
)
from run_backtest_a4 import select_dynamic_grace   # ★ A-4 动态保护期选股
from run_backtest_a import MA_PERIOD, load_panels
from data.storage import load_meta
from monitoring.alerts import send_alert

logger.add("logs/signal_a_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

TRACK_A_CAPITAL  = 600_000
N_HOLDINGS       = 30
MA10_EXIT_DAYS   = 3       # 连续跌破10日均线几天出清
SIGNAL_FILE      = Path("data_store/meta/signal_a_latest.json")


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _get_trade_calendar() -> list[str]:
    cal = load_meta("trade_calendar")
    return sorted(cal["trade_date"].tolist()) if not cal.empty else []


def is_trade_day(today: str, calendar: list[str]) -> bool:
    return today in calendar


def is_rebalance_day(today: str, calendar: list[str]) -> bool:
    """月末最后交易日 + 月中（与回测biweekly一致）。"""
    year_month  = today[:7]
    month_dates = sorted([d for d in calendar if d.startswith(year_month)])
    if not month_dates:
        return False
    is_month_end = (today == month_dates[-1])
    n = len(month_dates)
    mid_idx = max(0, n // 2 - 1)
    is_month_mid = (n >= 2 and today == month_dates[mid_idx])
    return is_month_end or is_month_mid


def _get_position_ratio(today: str) -> float:
    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        return 0.70
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    close = idx_df.set_index("date")["close"].sort_index()
    close = pd.to_numeric(close, errors="coerce").dropna()
    return get_position_ratio(close, pd.Timestamp(today))


def _load_prev_signal() -> dict:
    if not SIGNAL_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _check_ma10_exits(
    holdings: list[str],
    today: str,
    prev_days_below: dict[str, int],
) -> tuple[list[str], dict[str, int]]:
    """
    检查持仓股是否连续 MA10_EXIT_DAYS 天跌破10日均线。
    返回 (需要出清的列表, 更新后的days_below_ma10)
    """
    if not holdings:
        return [], {}

    start = (date.fromisoformat(today) - timedelta(days=25)).strftime("%Y-%m-%d")
    panel, _ = load_panels(holdings, start, today)
    if panel.empty:
        return [], prev_days_below

    panel = panel.ffill()
    exits, new_days = [], {}

    for code in holdings:
        if code not in panel.columns:
            new_days[code] = prev_days_below.get(code, 0)
            continue
        col = panel[code].dropna()
        if len(col) < 10:
            new_days[code] = 0
            continue
        ma10  = col.iloc[-10:].mean()
        cur_p = col.iloc[-1]
        if cur_p < ma10:
            new_days[code] = prev_days_below.get(code, 0) + 1
        else:
            new_days[code] = 0
        if new_days[code] >= MA10_EXIT_DAYS:
            exits.append(code)

    return exits, new_days


def _calc_weighted_shares(
    holdings: list[str],
    weights: dict[str, float],
    prices: pd.Series,
    total_capital: float,
) -> dict[str, int]:
    shares = {}
    for code in holdings:
        w     = weights.get(code, 1 / max(len(holdings), 1))
        price = prices.get(code)
        if price and not np.isnan(float(price)) and float(price) > 0:
            capital  = total_capital * w
            min_lot  = 200 if str(code).startswith("688") else 100
            lots     = int(capital / float(price) / min_lot)
            shares[code] = lots * min_lot
        else:
            shares[code] = 0
    return shares


# ── 主流程 ────────────────────────────────────────────────────────────────

def run():
    today    = date.today().strftime("%Y-%m-%d")
    calendar = _get_trade_calendar()

    if not is_trade_day(today, calendar):
        logger.info(f"{today} 非交易日，跳过")
        return

    prev_signal      = _load_prev_signal()
    current_holdings = prev_signal.get("holdings", [])
    hold_counts      = {str(k): int(v) for k, v in prev_signal.get("hold_counts", {}).items()}
    entry_prices     = {str(k): float(v) for k, v in prev_signal.get("entry_prices", {}).items()}
    days_below_ma10  = {str(k): int(v) for k, v in prev_signal.get("days_below_ma10", {}).items()}

    # ── 每日检查：MA10 连续跌破出清 ──────────────────────────────────
    ma10_exits, days_below_ma10 = _check_ma10_exits(
        current_holdings, today, days_below_ma10
    )

    if ma10_exits:
        logger.warning(f"[MA10出清] {today} 触发 {len(ma10_exits)} 只: {ma10_exits}")
        current_holdings = [c for c in current_holdings if c not in set(ma10_exits)]
        hold_counts      = {k: v for k, v in hold_counts.items()      if k not in set(ma10_exits)}
        entry_prices     = {k: v for k, v in entry_prices.items()     if k not in set(ma10_exits)}
        days_below_ma10  = {k: v for k, v in days_below_ma10.items()  if k not in set(ma10_exits)}

        # 保存中间状态（含MA10出清），等待调仓日补仓
        partial_signal = dict(prev_signal)
        partial_signal.update({
            "signal_date":    today,
            "ma10_exits":     ma10_exits,
            "holdings":       current_holdings,
            "sell":           ma10_exits,
            "buy":            [],
            "hold_counts":    hold_counts,
            "entry_prices":   entry_prices,
            "days_below_ma10": days_below_ma10,
            "note":           f"MA10出清：{ma10_exits}，等待下次调仓日补仓",
        })
        SIGNAL_FILE.write_text(
            json.dumps(partial_signal, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        send_alert(
            f"【Track A MA10出清】{today}\n"
            f"⚠️ 技术走弱出清 {len(ma10_exits)} 只：{', '.join(ma10_exits)}\n"
            f"当前持仓剩余: {len(current_holdings)} 只\n"
            f"等待下次调仓日（月中/月末）自动补仓"
        )

    # ── 非调仓日且无出清：记录日志退出 ───────────────────────────
    # FORCE_REBAL=1 可强制在非调仓日生成完整选股信号（用于手动建仓）
    force = os.getenv("FORCE_REBAL", "0") == "1"
    if not is_rebalance_day(today, calendar) and not force:
        if not ma10_exits:
            logger.info(f"{today} 非调仓日，MA10正常，持仓 {len(current_holdings)} 只")
        return
    if force and not is_rebalance_day(today, calendar):
        logger.info(f"[FORCE_REBAL] 强制在非调仓日生成完整选股信号")

    # ── 调仓日：完整 A-4 选股 ─────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"[Track A-4] 两周调仓日: {today}")
    logger.info("=" * 60)

    pos_ratio = _get_position_ratio(today)
    logger.info(f"[大势研判] CSI800/MA200 → 仓位比例: {pos_ratio:.0%}")

    if pos_ratio <= 0.30:
        signal = {
            "signal_date":    today, "strategy": "A-4",
            "regime":         "bear", "position_ratio": pos_ratio,
            "cash_action":    "money_market",
            "holdings":       [], "buy": [], "sell": current_holdings,
            "weights":        {}, "shares": {}, "prices":  {},
            "hold_counts":    {}, "entry_prices": {},
            "days_below_ma10": {},
        }
        _save_and_alert(signal, pos_ratio)
        return

    # 加载价格矩阵（含MA10出清后的状态）
    logger.info("加载价格+成交额矩阵...")
    start      = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
    csi800     = load_meta("csi800")
    codes      = sorted(csi800["code"].tolist())
    panel, amt = load_panels(codes, start, today)
    if panel.empty:
        send_alert("[Track A] 价格数据加载失败，信号中止", level="error")
        return
    logger.info(f"价格矩阵：{panel.shape[0]}天 × {panel.shape[1]}只")

    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info
    today_ts   = pd.Timestamp(today)

    score = compute_score_a2(panel, today_ts, amt, stock_info)
    if len(score) < N_HOLDINGS:
        send_alert(f"[Track A] 有效股票数不足（{len(score)}），信号跳过", level="error")
        return

    # ★ A-4 动态保护期选股（浮盈自由换，浮亏15%门槛）
    cur_prices_series = panel.ffill().iloc[-1]
    selected = select_dynamic_grace(
        score, current_holdings, N_HOLDINGS,
        entry_prices, cur_prices_series
    )

    raw_weights    = compute_weights(selected, score, stock_info, SECTOR_BOOST)
    actual_weights = {c: w * pos_ratio for c, w in raw_weights.items()}

    new_set  = set(selected)
    prev_set = set(current_holdings)
    buy_list  = [c for c in selected  if c not in prev_set]
    sell_list = [c for c in current_holdings if c not in new_set]

    latest_prices = panel.ffill().iloc[-1]
    effective_cap = TRACK_A_CAPITAL * pos_ratio
    shares        = _calc_weighted_shares(selected, raw_weights, latest_prices, effective_cap)
    zero_shares   = [c for c, s in shares.items() if s == 0]
    if zero_shares:
        logger.warning(f"以下 {len(zero_shares)} 只资金不足一手，需人工处理: {zero_shares}")

    # 更新持仓期数
    new_hold_counts = {
        c: hold_counts.get(c, 0) + 1 if c in prev_set else 1
        for c in selected
    }

    # 更新入场价（新买入记录今日收盘，续仓保留原价）
    new_entry_prices = dict(entry_prices)
    for c in buy_list:
        p = latest_prices.get(c)
        if p and not pd.isna(p):
            new_entry_prices[c] = float(p)
    for c in sell_list:
        new_entry_prices.pop(c, None)
    # 清理已不在持仓的
    new_entry_prices = {c: v for c, v in new_entry_prices.items() if c in new_set}

    # 清理已出清股票的MA10计数
    new_days_below = {c: days_below_ma10.get(c, 0) for c in selected}

    # 统计浮盈/浮亏保护情况
    profit_free = sum(1 for c in selected
                      if new_entry_prices.get(c) and float(latest_prices.get(c, 0)) >= new_entry_prices.get(c, 0))
    loss_prot   = len(selected) - profit_free

    logger.info(f"动态保护: 浮盈可换={profit_free}只  浮亏保护={loss_prot}只")
    logger.info(f"持仓期数: ≥2期={sum(1 for v in new_hold_counts.values() if v>=2)}只  "
                f"新仓={sum(1 for v in new_hold_counts.values() if v==1)}只")

    top5 = sorted(selected, key=lambda c: raw_weights.get(c, 0), reverse=True)[:5]

    signal = {
        "signal_date":     today,
        "strategy":        "A-4",
        "regime":          "bull",
        "position_ratio":  pos_ratio,
        "cash_ratio":      round(1.0 - pos_ratio, 2),
        "cash_action":     "equity" if pos_ratio >= 0.70 else "equity_reduced",
        "holdings":        selected,
        "buy":             buy_list,
        "sell":            sell_list,
        "weights":         {c: round(w, 6) for c, w in raw_weights.items()},
        "actual_weights":  {c: round(w, 6) for c, w in actual_weights.items()},
        "shares":          shares,
        "prices":          {c: round(float(latest_prices.get(c, 0)), 2) for c in selected},
        "effective_capital": round(effective_cap),
        "hold_counts":     new_hold_counts,
        "entry_prices":    {c: round(v, 2) for c, v in new_entry_prices.items()},
        "days_below_ma10": new_days_below,
        "note":            f"T+0：14:57前提交竞价收盘委托 | 仓位{pos_ratio:.0%} | 策略A-4",
    }
    _save_and_alert(signal, pos_ratio)


def _save_and_alert(signal: dict, pos_ratio: float = 1.0):
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(
        json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"信号已保存 → {SIGNAL_FILE}")

    today    = signal["signal_date"]
    regime   = signal["regime"]
    holdings = signal.get("holdings", [])
    buy      = signal.get("buy", [])
    sell     = signal.get("sell", [])
    weights  = signal.get("weights", {})

    if regime == "bear":
        msg = (
            f"【Track A-4 信号】{today}\n"
            f"⚠️ 极度熊市 → 清仓转货币基金\n"
            f"卖出: {len(sell)} 只"
        )
    else:
        stock_info = load_meta("stock_info_full")
        ind_str = "—"
        if not stock_info.empty and "industry_l1" in stock_info.columns:
            ind_map = stock_info.set_index("code")["industry_l1"].to_dict()
            ind_cnt: dict[str, int] = {}
            for c in holdings:
                ind = ind_map.get(c, "其他")
                ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
            top_ind = sorted(ind_cnt.items(), key=lambda x: x[1], reverse=True)[:3]
            ind_str = "  ".join(f"{k}({v}只)" for k, v in top_ind)

        top5 = sorted(holdings, key=lambda c: weights.get(c, 0), reverse=True)[:5]
        msg = (
            f"【Track A-4 信号】{today}\n"
            f"📊 仓位: {pos_ratio:.0%}（股票）+ {1-pos_ratio:.0%}（货基）\n"
            f"📈 持仓: {len(holdings)} 只 | 主要行业: {ind_str}\n"
            f"🔴 卖出({len(sell)}): {', '.join(sell[:4])}{'...' if len(sell)>4 else ''}\n"
            f"🟢 买入({len(buy)}): {', '.join(buy[:4])}{'...' if len(buy)>4 else ''}\n"
            f"⭐ 权重Top5: {', '.join(f'{c}({weights.get(c,0):.1%})' for c in top5)}\n"
            f"💰 实际投入: {signal.get('effective_capital',0):,} 元\n"
            f"⏰ 请于 14:57 前提交竞价收盘委托"
        )

    logger.info(msg)
    send_alert(msg)


if __name__ == "__main__":
    run()
