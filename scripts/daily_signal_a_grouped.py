"""
Track A 摊平分组信号生成（2026-09-02，摊平部署的核心改造）。

背景: 时点运气检验(极差16.64pp) → 摊平定案2组, 调仓日偏移{g0:+2, g1:+7}
交易日(结构间距=双周10交易日最大错开5天)。每组独立选股/退出, 各50万。

设计:
  - 每组一个信号文件 signal_a_g0.json / signal_a_g1.json
  - 每组调仓日 = 15日+月末锚点 + 组偏移 (与回测make_rebal_dates同口径)
  - 首次部署: 现有持仓交替分配给两组(g0奇数位/g1偶数位)
  - QMT校正: 每组持仓与快照交集(止损卖掉的票从所属组移除)
  - 执行端(fetch_and_execute)合并两组为账户级目标(100万)后diff下单

Cron 切换: 9/14 前把 25 14 * * 1-5 从 daily_signal_a_v2.py 切到本脚本。
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

from daily_signal_a_v2 import (
    _check_ma10_exits, _fill_missing_shares, _select_top_turnover,
    _calc_shares, _load_actual_qmt_holdings, _get_trade_calendar,
    is_trade_day, _get_position_ratio, MAX_VOL20,
)
from run_backtest_a import load_panels
from data.storage import load_meta
from monitoring.alerts import send_alert

logger.add("logs/signal_a_grouped_{time:YYYY-MM-DD}.log",
           rotation="1 day", retention="60 days")

GROUPS = {"g0": 2, "g1": 7}          # 组偏移(交易日), 摊平定案{2,7}
GROUP_CAPITAL = 500_000              # 每组50万(方案A: 股票100万=2组)
N_HOLDINGS = 30                      # pool30定案
_DRY_RUN = os.environ.get("GROUP_SIGNAL_DRY_RUN", "0") == "1"
_BASE = Path("/tmp/signal_group_test") if _DRY_RUN else Path("data_store/meta")
if _DRY_RUN:
    _BASE.mkdir(parents=True, exist_ok=True)
GROUP_FILE = {g: _BASE / f"signal_a_{g}.json" for g in GROUPS}
LEGACY_FILE = Path("data_store/meta/signal_a_latest.json")


def _is_base_rebalance_day(today: str, calendar: list[str]) -> bool:
    """15日+月末锚点(与回测make_rebal_dates同口径; 旧v2用倒数第二+月中是
    回测-实盘口径错位的遗留, 摊平日历必须与回测锚点一致)。"""
    t = pd.Timestamp(today)
    if t.day == 15:
        return True
    month_dates = sorted([d for d in calendar if d.startswith(today[:7])])
    return bool(month_dates) and today == month_dates[-1]


def _is_group_rebalance_day(today, calendar, offset):
    """组调仓日 = 锚点日向后平移offset个交易日。"""
    idx = {d: i for i, d in enumerate(calendar)}
    if today not in idx:
        return False
    # 检查 today-offset 是否是锚点
    src_i = idx[today] - offset
    if src_i < 0:
        return False
    src = calendar[src_i]
    return _is_base_rebalance_day(src, calendar)


def _load_group_signal(g: str) -> dict:
    if not GROUP_FILE[g].exists():
        return {}
    try:
        return json.loads(GROUP_FILE[g].read_text(encoding="utf-8"))
    except Exception:
        return {}


def _initial_split(today: str, pos_ratio: float, calendar):
    """首次部署: 持仓交替分配给两组, 组内等权50万。

    2026-09-02 修正: 基准优先用最新QMT快照(30只)而非旧信号文件(24只)——
    用旧文件做基准会让执行器把"今早新买但不在分组名单"的持仓误卖。
    价格: 快照 market_value/volume; 旧信号prices补缺。
    """
    if any(GROUP_FILE[g].exists() for g in GROUPS):
        return
    actual = _load_actual_qmt_holdings(calendar)
    legacy = {}
    if LEGACY_FILE.exists():
        try:
            legacy = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
        except Exception:
            legacy = {}
    base = sorted(actual) if actual is not None else legacy.get("holdings", [])
    if not base:
        logger.warning("[分组初始化] 无持仓基准(快照和旧信号都空), 两组从空开始")
        return
    prices = dict(legacy.get("prices", {}))
    snap = Path("logs/qmt_positions_latest.json")
    if snap.exists():
        try:
            d = json.loads(snap.read_text(encoding="utf-8"))
            for k, v in d.get("positions", {}).items():
                code = str(k).split(".")[0]
                if code in set(base) and code not in prices:
                    mv = v.get("market_value", 0)
                    vol = v.get("volume", 0)
                    if mv and vol:
                        prices[code] = round(mv / vol, 2)
        except Exception:
            pass
    g0 = base[0::2]
    g1 = base[1::2]
    capital = GROUP_CAPITAL * pos_ratio
    for g, hs in (("g0", g0), ("g1", g1)):
        sig = {
            "signal_date": today, "strategy": "A-v2-grouped",
            "group": g, "regime": "bull" if pos_ratio >= 0.70 else "neutral",
            "position_ratio": pos_ratio,
            "holdings": hs, "buy": [], "sell": [],
            "shares": _fill_missing_shares(hs, {}, prices, capital),
            "prices": {c: round(float(prices[c]), 2) for c in hs
                       if c in prices and prices[c]},
            "effective_capital": round(capital),
            "days_below_ma10": {},
        }
        GROUP_FILE[g].write_text(json.dumps(sig, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    send_alert(f"[摊平分组初始化] {today}: 基准{len(base)}只(快照{'✓' if actual else '旧信号'})"
               f" → g0={len(g0)}只 g1={len(g1)}只")


def _correct_group(g: str, calendar, holdings, days_below):
    """组持仓校正到QMT快照交集(止损卖掉的票从该组移除)。"""
    actual = _load_actual_qmt_holdings(calendar)
    if actual is None:
        return holdings, days_below
    keep = [c for c in holdings if c in set(actual)]
    gone = sorted(set(holdings) - set(keep))
    if gone:
        logger.warning(f"[{g}持仓漂移] 实盘已无: {gone}")
    days_below = {k: v for k, v in days_below.items() if k in set(keep)}
    return keep, days_below


def run_group(g: str, offset: int, today: str, calendar, panel, amt,
              latest_prices, pos_ratio):
    prev = _load_group_signal(g)
    holdings = [str(c) for c in prev.get("holdings", [])]
    days_below = {str(k): int(v) for k, v
                  in prev.get("days_below_ma10", {}).items()}
    holdings, days_below = _correct_group(g, calendar, holdings, days_below)

    # 熊市: 清仓信号
    if pos_ratio <= 0.30:
        sig = {"signal_date": today, "strategy": "A-v2-grouped", "group": g,
               "regime": "bear", "position_ratio": pos_ratio,
               "holdings": [], "buy": [], "sell": holdings,
               "weights": {}, "shares": {}, "prices": {},
               "effective_capital": 0, "days_below_ma10": {}}
        GROUP_FILE[g].write_text(json.dumps(sig, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        return

    # 每日: MA10出清检查(含补买) —— 组内
    ma10_exits, days_below, latest_closes = _check_ma10_exits(
        holdings, today, days_below)
    is_rebal = _is_group_rebalance_day(today, calendar, offset)
    capital_now = GROUP_CAPITAL * pos_ratio

    if ma10_exits:
        holdings = [c for c in holdings if c not in set(ma10_exits)]
        days_below = {k: v for k, v in days_below.items()
                      if k not in set(ma10_exits)}
        # 组内补买: 成交额候补(跳过已持有)
        replacements = []
        if not amt.empty:
            rprices = latest_prices
            budget = GROUP_CAPITAL * pos_ratio / N_HOLDINGS
            candidates = _select_top_turnover(
                amt, rprices, budget * N_HOLDINGS, int(N_HOLDINGS * 1.5))
            for c in candidates:
                if c not in holdings and c not in replacements:
                    replacements.append(c)
                if len(replacements) >= len(ma10_exits):
                    break
            holdings += replacements
            logger.info(f"[{g} MA10补买] {today}: 出清{ma10_exits} 补入{replacements}")

    if is_rebal:
        # 组调仓日: 独立TOP30选股
        budget = capital_now
        selected = _select_top_turnover(amt, latest_prices, budget, N_HOLDINGS)
        sell_list = [c for c in holdings if c not in set(selected)]
        buy_list = [c for c in selected if c not in set(holdings)]
        holdings = [c for c in holdings if c in set(selected)] + buy_list
        shares = _calc_shares(selected, latest_prices, capital_now)
        prices = {c: round(float(latest_prices.get(c, 0)), 2)
                  for c in selected if latest_prices.get(c)}
        logger.info(f"[{g} 调仓] {today}: 选{len(selected)}只 卖{len(sell_list)} 买{len(buy_list)}")
    else:
        # 非调仓日: 维持持仓, 补齐shares/prices(9/1事故修复的组内版)
        sell_list = []
        buy_list = []
        prices = dict(prev.get("prices", {}))
        prices.update({c: round(v, 2) for c, v in latest_closes.items()
                       if c in set(holdings)})
        held = set(holdings)
        prev_shares = {k: v for k, v in prev.get("shares", {}).items()
                       if str(k) in held}
        shares = _fill_missing_shares(holdings, prev_shares, prices,
                                      capital_now)

    sig = {
        "signal_date": today, "strategy": "A-v2-grouped", "group": g,
        "regime": "bull" if pos_ratio >= 0.70 else "neutral",
        "position_ratio": pos_ratio,
        "holdings": holdings, "buy": buy_list, "sell": sell_list,
        "weights": {c: 1.0 / N_HOLDINGS for c in holdings},
        "shares": shares, "prices": prices,
        "effective_capital": round(capital_now),
        "days_below_ma10": {c: days_below.get(c, 0) for c in holdings},
    }
    GROUP_FILE[g].write_text(json.dumps(sig, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def run():
    today = date.today().strftime("%Y-%m-%d")
    calendar = _get_trade_calendar()
    if not is_trade_day(today, calendar):
        logger.info(f"{today} 非交易日，跳过")
        return

    pos_ratio = _get_position_ratio(today)
    logger.info(f"[摊平分组] {today} pos_ratio={pos_ratio:.0%}")

    # 首次部署: 交替分配现有持仓
    _initial_split(today, pos_ratio, calendar)

    # 共用一次面板加载(两组同源数据)
    start = (date.today() - timedelta(days=350)).strftime("%Y-%m-%d")
    csi800 = load_meta("csi800")
    codes = sorted(csi800["code"].tolist())
    panel, amt = load_panels(codes, start, today)
    latest_prices = panel.ffill().iloc[-1] if not panel.empty else pd.Series()

    for g, offset in GROUPS.items():
        try:
            run_group(g, offset, today, calendar, panel, amt,
                      latest_prices, pos_ratio)
        except Exception as e:
            logger.error(f"[{g}] 信号生成失败: {e}")
            send_alert(f"[{g}] 摊平信号生成失败: {e}", level="error")
    logger.info(f"[摊平分组] {today} 完成: g0={len(_load_group_signal('g0').get('holdings', []))}只 "
                f"g1={len(_load_group_signal('g1').get('holdings', []))}只")


if __name__ == "__main__":
    run()
