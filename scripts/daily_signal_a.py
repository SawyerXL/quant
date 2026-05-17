"""
Track A 每日信号生成脚本（策略A-3版）。

策略A-3 在 A-2 基础上新增「新仓保护期」：
  ⑦ 新仓保护期（GRACE_PERIODS=2, GRACE_THRESHOLD=15%）
     持仓 < 2个调仓周期（约4周）的股票受保护，
     替换者需得分高出15%才能换出，防止「旋转门」效应。

  数据支撑（持仓天数分析）：
    <2周 胜率23%，单笔期望-4.3%（负收益）
    2-4周 胜率33%，单笔期望-3.2%（负收益）
    >4周 胜率51%，单笔期望+6.0%（正收益）
  → 减少早期换仓，让持仓进入正期望区间

继承 A-2 全部改进：
  ① 多周期动量叠加：Z行业(1M)×30% + Z行业(6M)×40% + Z行业(12M)×30%
  ② 波动率调控：高波动股票得分×0.7，低波动×1.3
  ③ 行业均衡选股：按行业强度分配名额，单行业上限8只
  ④ 得分加权：top股权重约2倍bottom股（线性递减）
  ⑤ 主线板块1.3倍：最强板块额外放大
  ⑥ 阶梯式仓位（5档）：CSI800/MA200 → 30%~100%

运行时机（cron）：
    25 14 * * 1-5  → 每个交易日 14:25 运行
    14:25 生成信号 → 人工确认 → 14:57 前提交竞价收盘委托
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import date, timedelta
import numpy as np
import pandas as pd
from loguru import logger

# 策略A-3：复用 A-2 的打分/权重，替换选股为 A-3 的保护期选股
from run_backtest_a2 import (
    compute_score_a2,
    compute_weights,
    get_position_ratio,
    MAX_IND_SLOT,
    SECTOR_BOOST,
)
from run_backtest_a3 import select_with_grace   # ★ A-3 新仓保护期选股

GRACE_PERIODS   = 2     # 保护期：持仓不足2期的股票受保护
GRACE_THRESHOLD = 0.15  # 替换门槛：替换者需高出15%得分
from run_backtest_a import MA_PERIOD, load_panels
from data.storage import load_meta
from monitoring.alerts import send_alert

logger.add("logs/signal_a_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

TRACK_A_CAPITAL = 600_000       # Track A 总资金 60万
N_HOLDINGS      = 30            # 持仓股票数
SIGNAL_FILE     = Path("data_store/meta/signal_a_latest.json")


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
    """
    基于 CSI800/MA200 返回阶梯仓位比例（30%~100%）。
    缺少数据时保守返回 70%。
    """
    idx_df = load_meta("csi800_index")
    if idx_df.empty:
        logger.warning("缺少 csi800_index，仓位默认70%")
        return 0.70
    idx_df["date"] = pd.to_datetime(idx_df["date"])
    close = idx_df.set_index("date")["close"].sort_index()
    close = pd.to_numeric(close, errors="coerce").dropna()
    return get_position_ratio(close, pd.Timestamp(today))


def _load_prev_signal() -> dict:
    """读取上期信号文件（含 hold_counts 持仓期数）。"""
    if not SIGNAL_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_hold_counts() -> dict[str, int]:
    """从上期信号文件读取各股已持仓期数，用于保护期判断。"""
    prev = _load_prev_signal()
    return {str(k): int(v) for k, v in prev.get("hold_counts", {}).items()}


def _calc_weighted_shares(
    holdings: list[str],
    weights: dict[str, float],
    prices: pd.Series,
    total_capital: float,
) -> dict[str, int]:
    """
    按得分权重分配资金，计算各股买入股数。
    科创板（688开头）最小申报单位200股，其余100股。
    """
    shares = {}
    for code in holdings:
        w     = weights.get(code, 1 / max(len(holdings), 1))
        price = prices.get(code)
        if price and not np.isnan(float(price)) and float(price) > 0:
            capital   = total_capital * w
            min_lot   = 200 if str(code).startswith("688") else 100
            lots      = int(capital / float(price) / min_lot)
            shares[code] = lots * min_lot
        else:
            shares[code] = 0
    return shares


# ── 主流程 ────────────────────────────────────────────────────────────────

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

    logger.info("=" * 60)
    logger.info(f"[Track A-3] 两周调仓日: {today}")
    logger.info("=" * 60)

    # ③ 阶梯式仓位（策略A-3：不再二值化，5档30%~100%）
    pos_ratio = _get_position_ratio(today)
    logger.info(f"[大势研判] CSI800/MA200 → 仓位比例: {pos_ratio:.0%}")

    prev_signal   = _load_prev_signal()
    prev_holdings = prev_signal.get("holdings", [])

    if pos_ratio <= 0.30:
        # 极度熊市：保留最低30%仓位（实盘：清股票仓，资金转货币基金）
        logger.warning("[大势研判] 极度熊市（CSI800 < MA200×0.95），最小仓位30%，清仓股票")
        signal = {
            "signal_date":   today,
            "regime":        "bear",
            "position_ratio": pos_ratio,
            "cash_action":   "money_market",
            "holdings":      [],
            "buy":           [],
            "sell":          prev_holdings,
            "weights":       {},
            "shares":        {},
            "prices":        {},
        }
        _save_and_alert(signal, pos_ratio)
        return

    # ④ 加载价格+成交额（过去~14个月，保证270+条K线供多周期计算）
    logger.info("加载价格+成交额矩阵...")
    start  = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
    csi800 = load_meta("csi800")
    if csi800.empty:
        send_alert("[Track A] CSI800成分股数据缺失，信号中止", level="error")
        return
    codes = sorted(csi800["code"].tolist())
    panel, amount_panel = load_panels(codes, start, today)
    if panel.empty:
        send_alert("[Track A] 价格数据加载失败，信号中止", level="error")
        return
    logger.info(f"价格矩阵：{panel.shape[0]}天 × {panel.shape[1]}只")

    # ⑤ 策略A-3 选股打分（多周期行业中性 + 波动率调控）
    logger.info("策略A-3 选股中（多周期行业中性动量 + 新仓保护期）...")
    today_ts   = pd.Timestamp(today)
    stock_info = load_meta("stock_info_full")
    stock_info = None if stock_info.empty else stock_info

    score = compute_score_a2(panel, today_ts, amount_panel, stock_info)
    if len(score) < N_HOLDINGS:
        msg = f"[Track A] 有效股票数不足（{len(score)} < {N_HOLDINGS}），信号跳过"
        logger.error(msg)
        send_alert(msg, level="error")
        return

    # ⑥ A-3 保护期选股（持仓<2期的股票需15%优势才能被替换）
    hold_counts = _load_hold_counts()
    selected = select_with_grace(
        score, prev_holdings, hold_counts,
        N_HOLDINGS, GRACE_PERIODS, GRACE_THRESHOLD
    )
    logger.info(f"A-3 保护期选股完成: {len(selected)} 只 "
                f"（保护中: {sum(1 for c in selected if hold_counts.get(c,0)<GRACE_PERIODS)}只）")

    # ⑦ 得分加权 + 主线板块1.3倍权重
    raw_weights = compute_weights(selected, score, stock_info, SECTOR_BOOST)

    # 按仓位比例调整实际配置权重
    actual_weights = {c: w * pos_ratio for c, w in raw_weights.items()}
    # 剩余为现金/货基比例
    cash_ratio = 1.0 - pos_ratio
    logger.info(f"仓位分配: 股票={pos_ratio:.0%}  现金/货基={cash_ratio:.0%}")

    # ⑧ 与上期持仓对比，生成买卖清单
    new_set  = set(selected)
    prev_set = set(prev_holdings)
    buy_list  = [c for c in selected if c not in prev_set]
    sell_list = [c for c in prev_holdings if c not in new_set]

    # ⑨ 按权重计算委托股数
    latest_prices  = panel.ffill().iloc[-1]
    effective_cap  = TRACK_A_CAPITAL * pos_ratio   # 实际投入股票的资金
    shares         = _calc_weighted_shares(selected, raw_weights, latest_prices, effective_cap)
    zero_shares    = [c for c, s in shares.items() if s == 0]
    if zero_shares:
        logger.warning(f"以下 {len(zero_shares)} 只资金不足一手，需人工处理: {zero_shares}")

    # ⑩ 输出信号摘要（含权重信息）
    top5 = sorted(selected, key=lambda c: raw_weights.get(c, 0), reverse=True)[:5]
    logger.info("持仓 Top5（权重）：" + "  ".join(
        f"{c}({raw_weights.get(c,0):.1%})" for c in top5
    ))

    # 更新持仓期数（新入仓从1开始，续仓+1）
    new_set = set(selected)
    old_set = set(prev_holdings)
    new_hold_counts = {
        c: hold_counts.get(c, 0) + 1 if c in old_set else 1
        for c in selected
    }
    logger.info(f"持仓期数分布: "
                f"≥2期={sum(1 for v in new_hold_counts.values() if v>=2)}只  "
                f"1期={sum(1 for v in new_hold_counts.values() if v==1)}只（受保护）")

    signal = {
        "signal_date":    today,
        "strategy":       "A-3",
        "regime":         "bull",
        "position_ratio": pos_ratio,
        "cash_ratio":     cash_ratio,
        "cash_action":    "equity" if pos_ratio >= 0.70 else "equity_reduced",
        "holdings":       selected,
        "buy":            buy_list,
        "sell":           sell_list,
        "weights":        {c: round(w, 6) for c, w in raw_weights.items()},
        "actual_weights": {c: round(w, 6) for c, w in actual_weights.items()},
        "shares":         shares,
        "prices":         {c: round(float(latest_prices.get(c, 0)), 2) for c in selected},
        "effective_capital": round(effective_cap),
        "hold_counts":    new_hold_counts,          # 持仓期数，下次调仓读取
        "note":           f"T+0：14:57前提交竞价收盘委托 | 仓位{pos_ratio:.0%} | 策略A-3",
    }
    _save_and_alert(signal, pos_ratio)


def _save_and_alert(signal: dict, pos_ratio: float = 1.0):
    """保存信号文件并推送企业微信告警。"""
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
            f"【Track A-3 信号】{today}\n"
            f"⚠️ 极度熊市（CSI800/MA200 < 0.95）\n"
            f"股票仓位：{pos_ratio:.0%} → 清仓转货币基金\n"
            f"卖出: {len(sell)} 只\n"
            f"💰 资金操作：转入货币基金/短债，约年化2%"
        )
    else:
        # 计算各行业分布
        stock_info = load_meta("stock_info_full")
        if not stock_info.empty and "industry_l1" in stock_info.columns:
            ind_map = stock_info.set_index("code")["industry_l1"].to_dict()
            ind_cnt: dict[str, int] = {}
            for c in holdings:
                ind = ind_map.get(c, "其他")
                ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
            top_ind = sorted(ind_cnt.items(), key=lambda x: x[1], reverse=True)[:3]
            ind_str = "  ".join(f"{k}({v}只)" for k, v in top_ind)
        else:
            ind_str = "—"

        top5 = sorted(holdings, key=lambda c: weights.get(c, 0), reverse=True)[:5]
        msg = (
            f"【Track A-3 信号】{today}\n"
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
