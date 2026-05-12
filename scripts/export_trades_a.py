"""
Track A 回测详细交易记录导出（修复版）。

修复内容：
  1. 卖出股数 = 实际持仓，不再按卖出价重新计算（原bug：卖出价低时反算出更多股数）
  2. 高价股若等权资金不足一手，记录为"资金不足，跳过"而非 0 股
  3. 新增字段：成本价、当前价/卖出价、每笔盈亏（元和百分比）

输出：logs/trades_a_detail.csv
运行：python scripts/export_trades_a.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta

logger.add("logs/export_trades_a.log", rotation="1 day")

FORMULA          = os.getenv("FORMULA", "I")
BACKTEST_START   = "2019-01-01"
BACKTEST_END     = os.getenv("BACKTEST_END", "")
N_HOLDINGS       = 30
COMMISSION       = 0.00125
MIN_BARS         = 250
LIQUIDITY_THRESH = 1000
REBAL_FREQ       = "biweekly"


def load_panels(codes, start, end):
    prices, amounts = {}, {}
    for code in codes:
        df = load_daily(code, start, end)
        if df.empty or "close" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        c = pd.to_numeric(df["close"],  errors="coerce")
        a = pd.to_numeric(df.get("amount", pd.Series(dtype=float)), errors="coerce")
        if len(c) > 200:
            prices[code] = c
            amounts[code] = a
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(amounts).sort_index()


def _zscore(s):
    mu, sigma = s.mean(), s.std()
    return pd.Series(0.0, index=s.index) if sigma < 1e-8 else ((s - mu) / sigma).clip(-3, 3)


def compute_score(panel, date, amount_panel=None, stock_info=None):
    hist = panel[panel.index <= date]
    if len(hist) < MIN_BARS:
        return pd.Series(dtype=float)
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        ra = ha.iloc[-20:].mean()
        liq = ra[ra > LIQUIDITY_THRESH].index
        hist = hist[hist.columns.intersection(liq)]
    if hist.empty:
        return pd.Series(dtype=float)

    p, p_126 = hist.iloc[-1], hist.iloc[-126]
    high_250 = hist.iloc[-250:].max()
    mom = p / p_126 - 1
    price_nh = (p / high_250).clip(0.5, 1.2)
    if amount_panel is not None:
        ha = amount_panel[amount_panel.index <= date]
        vol_r = ha.iloc[-20:].mean()
        vol_b = ha.iloc[-250:].mean().replace(0, float("nan"))
        vr = (vol_r / vol_b).clip(0.5, 3.0)
    else:
        vol_r = pd.Series(1.0, index=p.index)
        vr = pd.Series(1.0, index=p.index)
    boost = ((price_nh - 0.9) * 2).clip(0, 1) * ((vr - 1) * 0.5).clip(0, 0.5)
    base = mom * (1 + boost)
    cross_rank = vol_r.rank(pct=True).reindex(p.index)
    if stock_info is not None and "industry_l1" in stock_info.columns:
        ind_map = stock_info.set_index("code")["industry_l1"]
        sector_rank = pd.Series(0.5, index=p.index)
        for ind in ind_map.unique():
            ic = [c for c in ind_map[ind_map == ind].index if c in p.index and c in vol_r.index]
            if len(ic) >= 3:
                sector_rank[ic] = vol_r[ic].rank(pct=True)
        combined = 0.70 * cross_rank + 0.30 * sector_rank.reindex(p.index)
    else:
        combined = cross_rank
    tm = (0.80 + 0.20 * combined).fillna(0.90)
    return (base * tm).dropna()


def get_rebal_dates(calendar):
    dates = pd.DatetimeIndex(sorted(calendar))
    end = BACKTEST_END or str(dates[-1].date())
    dates = dates[(dates >= BACKTEST_START) & (dates <= end)]
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in range(1, 13):
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if not len(md):
                continue
            if REBAL_FREQ == "biweekly":
                result.append(md[len(md) // 2])
                result.append(md[-1])
            else:
                result.append(md[-1])
    return sorted(set(result))


def _min_lot(code: str) -> int:
    """科创板（688开头）最小手数200股，其他100股。"""
    return 200 if str(code).startswith("688") else 100


def main():
    logger.info("生成 Track A 详细交易记录（修复版）...")

    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失")
        return
    calendar = [d for d in cal_df["trade_date"].tolist() if BACKTEST_START <= d]

    stock_info = load_meta("stock_info_full")
    codes = stock_info["code"].tolist() if not stock_info.empty else []
    name_map = stock_info.set_index("code")["name"].to_dict() if not stock_info.empty else {}
    ind_map  = stock_info.set_index("code")["industry_l1"].to_dict() if not stock_info.empty else {}

    end = BACKTEST_END or calendar[-1]
    logger.info(f"加载价格矩阵 {len(codes)} 只...")
    panel, amount_panel = load_panels(codes, BACKTEST_START, end)
    if panel.empty:
        logger.error("价格数据加载失败")
        return

    rebal_dates = get_rebal_dates(calendar)
    logger.info(f"共 {len(rebal_dates)} 个调仓日")

    capital     = 600_000.0
    nav         = capital
    positions   = {}    # {code: {'shares': int, 'cost': float}}  实际持仓
    prev_hold   = []
    rows        = []

    for rebal_dt in rebal_dates:
        score = compute_score(panel, rebal_dt, amount_panel, stock_info)
        if len(score) < N_HOLDINGS:
            continue
        new_hold = score.nlargest(N_HOLDINGS).index.tolist()

        price_map = panel[panel.index <= rebal_dt].iloc[-1].to_dict()
        date_str  = str(rebal_dt.date())
        capital_per = nav / N_HOLDINGS

        sell_list = [c for c in prev_hold if c not in set(new_hold)]
        buy_list  = [c for c in new_hold  if c not in set(prev_hold)]
        hold_list = [c for c in new_hold  if c in set(prev_hold)]

        # ── 卖出（使用实际持仓股数）──────────────────────
        for code in sell_list:
            p = price_map.get(code, 0)
            if not p or pd.isna(p) or p <= 0:
                continue
            pos = positions.pop(code, None)
            if pos is None:
                continue
            actual_shares = pos["shares"]
            cost_price    = pos["cost"]
            sell_amount   = round(actual_shares * p, 2)
            fee           = round(sell_amount * COMMISSION, 2)
            pnl           = round((p - cost_price) * actual_shares - fee, 2)
            pnl_pct       = round((p / cost_price - 1) * 100, 2) if cost_price else 0

            rows.append({
                "日期":        date_str,
                "方向":        "卖出",
                "代码":        code,
                "名称":        name_map.get(code, ""),
                "行业":        ind_map.get(code, ""),
                "手数(手)":    actual_shares // 100,
                "股数(股)":    actual_shares,
                "成本价(元)":  round(cost_price, 2),
                "成交价(元)":  round(p, 2),
                "成交金额(元)": sell_amount,
                "手续费(元)":  fee,
                "盈亏(元)":    pnl,
                "盈亏(%)":     pnl_pct,
                "备注":        "移出持仓池",
            })
            nav += pnl  # 更新净值

        # ── 买入（计算整手数，记录成本）──────────────────
        for code in buy_list:
            p = price_map.get(code, 0)
            if not p or pd.isna(p) or p <= 0:
                continue
            min_lot = _min_lot(code)
            lots    = int(capital_per / p / min_lot)
            shares  = lots * min_lot
            if shares == 0:
                rows.append({
                    "日期":        date_str,
                    "方向":        "买入",
                    "代码":        code,
                    "名称":        name_map.get(code, ""),
                    "行业":        ind_map.get(code, ""),
                    "手数(手)":    0,
                    "股数(股)":    0,
                    "成本价(元)":  round(p, 2),
                    "成交价(元)":  round(p, 2),
                    "成交金额(元)": 0,
                    "手续费(元)":  0,
                    "盈亏(元)":    0,
                    "盈亏(%)":     0,
                    "备注":        f"资金不足一手（需{p*min_lot:,.0f}元，分配{capital_per:,.0f}元）",
                })
                continue
            amount = round(shares * p, 2)
            fee    = round(amount * COMMISSION, 2)
            positions[code] = {"shares": shares, "cost": p}
            rows.append({
                "日期":        date_str,
                "方向":        "买入",
                "代码":        code,
                "名称":        name_map.get(code, ""),
                "行业":        ind_map.get(code, ""),
                "手数(手)":    lots,
                "股数(股)":    shares,
                "成本价(元)":  round(p, 2),
                "成交价(元)":  round(p, 2),
                "成交金额(元)": amount,
                "手续费(元)":  fee,
                "盈亏(元)":    0,
                "盈亏(%)":     0,
                "备注":        "纳入持仓池",
            })

        # ── 持有（显示浮动盈亏）──────────────────────────
        for code in hold_list:
            p   = price_map.get(code, 0)
            pos = positions.get(code, {})
            cost = pos.get("cost", 0)
            shares = pos.get("shares", 0)
            if p and cost:
                unreal_pnl = round((p - cost) * shares, 2)
                unreal_pct = round((p / cost - 1) * 100, 2)
            else:
                unreal_pnl = unreal_pct = 0
            rows.append({
                "日期":        date_str,
                "方向":        "持有",
                "代码":        code,
                "名称":        name_map.get(code, ""),
                "行业":        ind_map.get(code, ""),
                "手数(手)":    shares // 100 if shares else "--",
                "股数(股)":    shares or "--",
                "成本价(元)":  round(cost, 2) if cost else "--",
                "成交价(元)":  round(p, 2) if p else "--",
                "成交金额(元)": "--",
                "手续费(元)":  "--",
                "盈亏(元)":    unreal_pnl,
                "盈亏(%)":     unreal_pct,
                "备注":        "继续持有",
            })

        prev_hold = new_hold

    df = pd.DataFrame(rows)
    out = Path("logs/trades_a_detail.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    trades = df[df["方向"].isin(["买入", "卖出"])]
    sell_pnl = pd.to_numeric(df[df["方向"] == "卖出"]["盈亏(元)"], errors="coerce")

    logger.info(f"交易记录生成完成")
    logger.info(f"  调仓次数: {df['日期'].nunique()} 次")
    logger.info(f"  买入笔数: {len(df[df['方向']=='买入'])}")
    logger.info(f"  卖出笔数: {len(df[df['方向']=='卖出'])}")
    logger.info(f"  实现盈亏: {sell_pnl.sum():+,.0f} 元")
    logger.info(f"  盈利笔数: {(sell_pnl > 0).sum()} / 亏损笔数: {(sell_pnl < 0).sum()}")
    logger.info(f"  结果文件: {out}")

    # 抽查300502新易盛
    sample = df[df["代码"] == "300502"]
    if not sample.empty:
        print("\n=== 抽查 300502 新易盛 ===")
        print(sample[["日期","方向","股数(股)","成本价(元)","成交价(元)","盈亏(元)","备注"]].to_string(index=False))


if __name__ == "__main__":
    main()
