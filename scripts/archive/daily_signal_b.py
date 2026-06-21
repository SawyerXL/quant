"""
Track B 每周信号生成脚本（三位一体强势股）。

运行时机（cron）：
    25 14 * * 5  → 每周五 14:25 运行（只在周五触发调仓）
    非周五交易日仅打日志，不生成信号

逻辑：
    1. 非交易日 / 非周五 → 退出
    2. 周五调仓日：
       a. 大势层 → 仓位系数（读 manual_scores_b.json 融合人工判断）
       b. 板块层 → top-3 申万一级行业
       c. 个股层 → 每行业 top-2（共约6只）
       d. 与上期对比 → 买入/卖出清单
       e. 保存信号 JSON + 推送企业微信

手工调整：每周一早填写 data_store/meta/manual_scores_b.json：
    {
      "week_start": "YYYY-MM-DD",
      "market_manual_score": 70,     // null 则纯量化
      "sector_overrides": {"电子": 85},
      "notes": "本周判断说明"
    }
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import date

import pandas as pd
from loguru import logger

from run_backtest_a import load_panels
from data.storage import load_meta
from strategies.trinity.universe import build_universe
from strategies.trinity.screener import compute_strength_score
from strategies.trinity.market   import market_score, score_to_position
from strategies.trinity.sector   import sector_scores, select_sectors
from monitoring.alerts import send_alert

logger.add("logs/signal_b_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

TRACK_B_CAPITAL   = 300_000
N_SECTORS         = 3
STOCKS_PER_SECTOR = 2
SIGNAL_FILE       = Path("data_store/meta/signal_b_latest.json")


# ── 工具函数 ──────────────────────────────────────────────────────────

def _get_trade_calendar() -> list[str]:
    cal = load_meta("trade_calendar")
    return sorted(cal["trade_date"].tolist()) if not cal.empty else []


def is_trade_day(today: str, calendar: list[str]) -> bool:
    return today in calendar


def is_weekly_rebalance_day(today: str) -> bool:
    """周五（weekday==4）触发调仓；若遇节假日提前到周四。"""
    return date.fromisoformat(today).weekday() == 4


def _load_prev_holdings() -> list[str]:
    if not SIGNAL_FILE.exists():
        return []
    try:
        return json.loads(SIGNAL_FILE.read_text(encoding="utf-8")).get("holdings", [])
    except Exception:
        return []


def _calc_shares(codes: list[str], prices: pd.Series, n_total: int,
                 pos_ratio: float = 1.0) -> dict[str, int]:
    """等权计算各股计划买入手数（向下取整，至少1手）。"""
    if not codes or n_total == 0:
        return {}
    capital_per = TRACK_B_CAPITAL * pos_ratio / n_total   # 乘以仓位系数
    return {
        code: max(1, int(capital_per / prices.get(code, 1) / 100)) * 100
        for code in codes
        if prices.get(code, 0) > 0
    }


# ── 主流程 ────────────────────────────────────────────────────────────

def run():
    today    = date.today().strftime("%Y-%m-%d")
    calendar = _get_trade_calendar()

    if not is_trade_day(today, calendar):
        logger.info(f"{today} 非交易日，跳过")
        return

    if not is_weekly_rebalance_day(today):
        logger.info(f"{today} 非周五调仓日，无需生成 Track B 信号")
        return

    logger.info("=" * 55)
    logger.info(f"[Track B] 周度调仓日: {today}")
    logger.info("=" * 55)

    # 加载元数据
    stock_info = load_meta("stock_info_full")
    if stock_info.empty:
        send_alert("[Track B] stock_info_full 缺失，信号中止", level="error")
        return

    # 加载 CSI 800+1000 价格+成交额（约1分钟）
    csi800  = load_meta("csi800")
    csi1000 = load_meta("csi1000")
    valid_codes = set(stock_info["code"].tolist())
    codes = sorted(
        (set(csi800["code"]) | set(csi1000["code"])) & valid_codes
    )
    logger.info(f"加载价格矩阵（{len(codes)} 只）...")

    from datetime import timedelta
    start = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    panel, amount_panel = load_panels(codes, start, today)

    if panel.empty:
        send_alert("[Track B] 价格数据加载失败，信号中止", level="error")
        return

    today_ts = pd.Timestamp(today)

    # ── 大势层 ────────────────────────────────────────
    idx_df = load_meta("csi800_index")
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    index_close = idx_df.set_index("date")["close"].sort_index()

    csi800_panel = panel[[c for c in csi800["code"] if c in panel.columns]]
    m_score   = market_score(index_close, csi800_panel, today_ts)
    pos_ratio = score_to_position(m_score, week_start=today)
    logger.info(f"大势得分: {m_score:.0f}  →  仓位系数: {pos_ratio:.0%}")

    # 极度熊市：全空仓
    if pos_ratio <= 0.10:
        logger.warning("大势极弱，本期全空仓")
        prev = _load_prev_holdings()
        _save_and_alert({
            "signal_date":    today,
            "regime":         "bear",
            "cash_action":    "money_market",  # 30万转入货币基金
            "market_score":   round(m_score, 1),
            "position_ratio": pos_ratio,
            "sectors":  [],
            "holdings": [],
            "buy":      [],
            "sell":     prev,
            "weights":  {},
            "shares":   {},
        })
        return

    # ── 板块层 ────────────────────────────────────────
    s_scores = sector_scores(panel, amount_panel, stock_info, today_ts)
    selected_sectors = select_sectors(s_scores, week_start=today, top_n=N_SECTORS)

    if not selected_sectors:
        send_alert(f"[Track B] {today} 无符合条件行业，信号跳过", level="warning")
        return

    logger.info(f"选中行业: {selected_sectors}")

    # ── 个股层 ────────────────────────────────────────
    universe = build_universe(today, stock_info, panel)
    strength = compute_strength_score(panel, amount_panel, today_ts, universe)

    ind_map = stock_info.set_index("code")["industry_l1"]
    new_holdings = []
    sector_picks = {}

    for sector in selected_sectors:
        sector_codes = [c for c in ind_map[ind_map == sector].index if c in strength.index]
        candidates   = strength.reindex(sector_codes).dropna()
        top          = candidates.nlargest(STOCKS_PER_SECTOR).index.tolist()
        sector_picks[sector] = top
        new_holdings.extend(top)

    if not new_holdings:
        send_alert(f"[Track B] {today} 个股层无信号，跳过", level="warning")
        return

    # 买卖差量
    prev_holdings = _load_prev_holdings()
    buy_list  = [c for c in new_holdings if c not in set(prev_holdings)]
    sell_list = [c for c in prev_holdings if c not in set(new_holdings)]

    # 计划股数
    latest_prices = panel.iloc[-1]
    shares = _calc_shares(new_holdings, latest_prices, len(new_holdings), pos_ratio)

    signal = {
        "signal_date":    today,
        "regime":         "bull",
        "cash_action":    "equity",       # 资金用于股票持仓
        "market_score":   round(m_score, 1),
        "position_ratio": pos_ratio,
        "sectors":        selected_sectors,
        "sector_picks":   sector_picks,
        "holdings":       new_holdings,
        "buy":            buy_list,
        "sell":           sell_list,
        "weights":       {c: round(1 / len(new_holdings), 6) for c in new_holdings},
        "shares":        shares,
        "prices":        {c: round(float(latest_prices.get(c, 0)), 2) for c in new_holdings},
        "capital_per_stock": round(TRACK_B_CAPITAL * pos_ratio / max(len(new_holdings), 1)),
        "note":          "T+0：本日14:57前提交竞价委托",
    }
    _save_and_alert(signal)


def _save_and_alert(signal: dict):
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(
        json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"信号已保存 → {SIGNAL_FILE}")

    today    = signal["signal_date"]
    regime   = signal["regime"]
    holdings = signal["holdings"]
    sectors  = signal.get("sectors", [])
    buy      = signal["buy"]
    sell     = signal["sell"]
    m_score  = signal.get("market_score", 0)
    pos_pct  = f"{signal.get('position_ratio', 0):.0%}"

    if regime == "bear":
        msg = (
            f"【Track B 信号】{today}\n"
            f"⚠️ 大势过弱（{m_score:.0f}分），全部清仓\n"
            f"卖出: {len(sell)} 只\n"
            f"💰 空仓期操作：30万转入货币基金"
        )
    else:
        sector_str = " / ".join(sectors)
        picks_str  = "\n".join(
            f"  {s}: {', '.join(v)}"
            for s, v in signal.get("sector_picks", {}).items()
        )
        msg = (
            f"【Track B 信号】{today}\n"
            f"大势: {m_score:.0f}分 → 仓位 {pos_pct}\n"
            f"行业: {sector_str}\n"
            f"{picks_str}\n"
            f"买入({len(buy)}): {', '.join(buy)}\n"
            f"卖出({len(sell)}): {', '.join(sell)}\n"
            f"⏰ 请于 14:57 前提交竞价委托"
        )

    logger.info(msg)
    send_alert(msg)


if __name__ == "__main__":
    run()
