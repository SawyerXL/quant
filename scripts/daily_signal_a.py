"""
Track A 每日信号生成脚本。

运行时机（cron）：
    25 14 * * 1-5  → 每个交易日 14:25 运行
    14:25 生成信号 → 人工确认 → 14:57 前提交竞价收盘委托

逻辑：
    1. 非交易日 / 非调仓日（月中+月末）→ 仅打日志，退出
    2. 两周调仓日（月中/月末）：
       a. 大势过滤（CSI 800 MA200 ±2%）→ 熊市则清仓
       b. 牛市：运行公式H计算截面得分，取前30只
       c. 与上期持仓对比，输出买入/卖出清单
       d. 保存信号 JSON + 发企业微信告警
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import date, timedelta

import pandas as pd
from loguru import logger

# 复用回测脚本里已验证的核心函数
from run_backtest_a import (
    LIQUIDITY_THRESH,
    MA_PERIOD,
    N_HOLDINGS,
    REGIME_BEAR_THR,
    REGIME_BULL_THR,
    build_regime_series,
    compute_score,
    load_panels,
)
from data.storage import load_meta
from monitoring.alerts import send_alert

logger.add("logs/signal_a_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

TRACK_A_CAPITAL = 600_000          # Track A 总资金 60万
SIGNAL_FILE     = Path("data_store/meta/signal_a_latest.json")


# ── 工具函数 ─────────────────────────────────────────────────────────────

def _get_trade_calendar() -> list[str]:
    cal = load_meta("trade_calendar")
    return sorted(cal["trade_date"].tolist()) if not cal.empty else []


def is_trade_day(today: str, calendar: list[str]) -> bool:
    return today in calendar


def is_rebalance_day(today: str, calendar: list[str]) -> bool:
    """
    两周调仓日判断（与 run_backtest_a.py 的 REBAL_FREQ=biweekly 一致）：
    - 月末最后一个交易日
    - 月中（当月第 floor(N/2)-1 个交易日，约第10-12个）
    """
    import pandas as pd
    year_month = today[:7]
    month_dates = sorted([d for d in calendar if d.startswith(year_month)])
    if not month_dates:
        return False

    # 月末
    is_month_end = (today == month_dates[-1])

    # 月中：第 floor(N/2)-1 个交易日（与回测完全一致）
    n = len(month_dates)
    mid_idx = max(0, n // 2 - 1)
    is_month_mid = (n >= 2 and today == month_dates[mid_idx])

    return is_month_end or is_month_mid


def _is_bull_market(today: str) -> bool:
    """
    基于 CSI 800 MA200 ±2% 缓冲区判断当前市场状态。
    若缺少指数数据，保守返回 True（不空仓）并告警。
    """
    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        logger.warning("缺少 csi800_index，大势过滤跳过，默认牛市")
        return True

    idx_df["date"] = pd.to_datetime(idx_df["date"])
    close = idx_df.set_index("date")["close"].sort_index()
    close = pd.to_numeric(close, errors="coerce").dropna()

    ma = close.rolling(MA_PERIOD).mean()
    ratio = close / ma

    # 回放状态机到 today
    state = True
    for dt, r in ratio.items():
        if pd.isna(r):
            continue
        if state and r < REGIME_BEAR_THR:
            state = False
        elif not state and r > REGIME_BULL_THR:
            state = True
        if str(dt.date()) >= today:
            break
    return state


def _load_prev_holdings() -> list[str]:
    """读取上期信号文件中的持仓列表。"""
    if not SIGNAL_FILE.exists():
        return []
    try:
        data = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
        return data.get("holdings", [])
    except Exception:
        return []


def _calc_shares(codes: list[str], prices: pd.Series) -> dict[str, int]:
    """
    按等权计算每只股票应买入的股数（向下取整到100股=1手）。
    """
    if not codes:
        return {}
    capital_per = TRACK_A_CAPITAL / len(codes)
    shares = {}
    for code in codes:
        price = prices.get(code)
        if price and price > 0:
            lots = int(capital_per / price / 100)   # 向下取整到整手
            shares[code] = lots * 100
        else:
            shares[code] = 0
    return shares


# ── 主流程 ───────────────────────────────────────────────────────────────

def run():
    today    = date.today().strftime("%Y-%m-%d")
    calendar = _get_trade_calendar()

    # ① 非交易日
    if not is_trade_day(today, calendar):
        logger.info(f"{today} 非交易日，跳过")
        return

    # ② 非调仓日
    if not is_rebalance_day(today, calendar):
        logger.info(f"{today} 非调仓日（非月中/月末），跳过")
        return

    logger.info(f"{'='*55}")
    logger.info(f"[Track A] 两周调仓日: {today}")
    logger.info(f"{'='*55}")

    # ③ 大势过滤
    in_bull = _is_bull_market(today)
    if not in_bull:
        logger.warning(f"[大势过滤] CSI 800 处于熊市状态，本期清仓")
        prev = _load_prev_holdings()
        signal = {
            "signal_date": today,
            "regime":      "bear",
            "cash_action": "money_market",  # 空仓：资金转入货币基金
            "holdings":    [],
            "buy":         [],
            "sell":        prev,
            "weights":     {},
            "shares":      {},
        }
        _save_and_alert(signal)
        return

    # ④ 加载价格+成交额（过去约14个月，保证270条K线）
    logger.info("加载价格矩阵...")
    start = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    csi800 = load_meta("csi800")
    if csi800.empty:
        send_alert("[Track A] CSI 800 成分股数据缺失，信号中止", level="error")
        return
    codes = sorted(csi800["code"].tolist())
    panel, amount_panel = load_panels(codes, start, today)

    if panel.empty:
        send_alert("[Track A] 价格数据加载失败，信号中止", level="error")
        return

    # ⑤ 公式H计算得分，取前30
    logger.info("运行公式H选股...")
    today_ts = pd.Timestamp(today)
    score = compute_score(panel, today_ts, amount_panel)

    if len(score) < N_HOLDINGS:
        msg = f"[Track A] 有效股票数不足（{len(score)} < {N_HOLDINGS}），信号跳过"
        logger.error(msg)
        send_alert(msg, level="error")
        return

    new_holdings = score.nlargest(N_HOLDINGS).index.tolist()

    # ⑥ 与上期持仓对比，生成买卖清单
    prev_holdings = _load_prev_holdings()
    buy_list  = [c for c in new_holdings if c not in set(prev_holdings)]
    sell_list = [c for c in prev_holdings if c not in set(new_holdings)]

    # ⑦ 计算委托股数（基于今日收盘价）
    latest_prices = panel.iloc[-1]
    shares = _calc_shares(new_holdings, latest_prices)

    signal = {
        "signal_date": today,
        "regime":      "bull",
        "cash_action": "equity",          # 资金用于股票持仓
        "holdings":    new_holdings,
        "buy":         buy_list,
        "sell":        sell_list,
        "weights":     {c: round(1 / N_HOLDINGS, 6) for c in new_holdings},
        "shares":      shares,
        "prices":      {c: round(float(latest_prices.get(c, 0)), 2) for c in new_holdings},
        "capital_per_stock": round(TRACK_A_CAPITAL / N_HOLDINGS),
        "note":        "T+0：在本日14:57前提交竞价收盘委托",
    }
    _save_and_alert(signal)


def _save_and_alert(signal: dict):
    """保存信号文件并推送企业微信告警。"""
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(
        json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"信号已保存 → {SIGNAL_FILE}")

    today   = signal["signal_date"]
    regime  = signal["regime"]
    holdings = signal["holdings"]
    buy      = signal["buy"]
    sell     = signal["sell"]

    if regime == "bear":
        msg = (
            f"【Track A 信号】{today}\n"
            f"⚠️ 大势过滤：熊市，全部清仓\n"
            f"卖出: {len(sell)} 只\n"
            f"💰 空仓期操作：60万转入货币基金（天弘余额宝等），约年化2%"
        )
    else:
        cash_note = ""
        if signal.get("cash_action") == "equity":
            prev_was_cash = len(signal.get("sell", [])) == 0 and len(signal.get("buy", [])) == len(holdings)
            if prev_was_cash:
                cash_note = "\n💰 从货币基金赎回资金，准备建仓"
        msg = (
            f"【Track A 信号】{today}\n"
            f"✅ 牛市 | 持仓 {len(holdings)} 只\n"
            f"买入({len(buy)}): {', '.join(buy[:5])}{'...' if len(buy)>5 else ''}\n"
            f"卖出({len(sell)}): {', '.join(sell[:5])}{'...' if len(sell)>5 else ''}\n"
            f"每股仓位: {signal.get('capital_per_stock',0):,} 元\n"
            f"⏰ 请于 14:57 前提交竞价收盘委托{cash_note}"
        )

    logger.info(msg)
    send_alert(msg)


if __name__ == "__main__":
    run()
