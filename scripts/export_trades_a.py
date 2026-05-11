"""
从 Track A 回测复现每次调仓的详细交易记录。
输出 logs/trades_a_detail.csv，包含每次买卖的股票、价格、手数、金额。

运行：
    python scripts/export_trades_a.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_daily, load_meta

logger.add("logs/export_trades_a.log", rotation="1 day")

# 与回测保持一致的参数
FORMULA         = os.getenv("FORMULA", "I")
BACKTEST_START  = "2019-01-01"
BACKTEST_END    = os.getenv("BACKTEST_END", "")
N_HOLDINGS      = 30
COMMISSION      = 0.00125
MIN_BARS        = 250
LIQUIDITY_THRESH = 1000
REBAL_FREQ      = "biweekly"


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


def compute_score(panel, date, amount_panel=None):
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
        vr = (ha.iloc[-20:].mean() / ha.iloc[-250:].mean().replace(0, float("nan"))).clip(0.5, 3.0)
    else:
        vr = pd.Series(1.0, index=p.index)
    boost = ((price_nh - 0.9) * 2).clip(0, 1) * ((vr - 1) * 0.5).clip(0, 0.5)
    base  = mom * (1 + boost)
    ar    = (ha.iloc[-20:].mean() if amount_panel is not None
             else pd.Series(1.0, index=p.index))
    tm    = (0.80 + 0.20 * ar.rank(pct=True).reindex(p.index)).fillna(0.90)
    return (base * tm).dropna()


def get_rebal_dates(calendar):
    dates = pd.DatetimeIndex(sorted(calendar))
    dates = dates[(dates >= BACKTEST_START) & (dates <= (BACKTEST_END or str(dates[-1].date())))]
    if REBAL_FREQ == "biweekly":
        result = []
        for yr in range(dates[0].year, dates[-1].year + 1):
            for mo in range(1, 13):
                md = dates[(dates.year == yr) & (dates.month == mo)]
                if len(md) == 0:
                    continue
                mid = md[len(md) // 2]
                result.append(mid)
                result.append(md[-1])
        return sorted(set(result))
    else:
        result = []
        for yr in range(dates[0].year, dates[-1].year + 1):
            for mo in range(1, 13):
                md = dates[(dates.year == yr) & (dates.month == mo)]
                if len(md) > 0:
                    result.append(md[-1])
        return result


def main():
    logger.info("生成 Track A 详细交易记录...")

    cal_df = load_meta("trade_calendar")
    if cal_df.empty:
        logger.error("交易日历缺失")
        return
    calendar = [d for d in cal_df["trade_date"].tolist()
                if BACKTEST_START <= d]

    stock_info = load_meta("stock_info_full")
    codes = stock_info["code"].tolist() if not stock_info.empty else []
    logger.info(f"股票池 {len(codes)} 只，加载价格矩阵中...")

    end = BACKTEST_END or calendar[-1]
    panel, amount_panel = load_panels(codes, BACKTEST_START, end)
    if panel.empty:
        logger.error("价格数据加载失败")
        return

    rebal_dates = get_rebal_dates(calendar)
    logger.info(f"共 {len(rebal_dates)} 个调仓日")

    # 复现持仓变化
    capital = 600_000.0
    nav     = capital
    prev_holdings = []
    rows = []

    for rebal_dt in rebal_dates:
        score = compute_score(panel, rebal_dt, amount_panel)
        if len(score) < N_HOLDINGS:
            continue
        new_holdings = score.nlargest(N_HOLDINGS).index.tolist()

        # 当日价格
        day_prices = panel[panel.index <= rebal_dt].iloc[-1]

        sell_list = [c for c in prev_holdings if c not in new_holdings]
        buy_list  = [c for c in new_holdings if c not in prev_holdings]
        hold_list = [c for c in new_holdings if c in prev_holdings]

        # 等权计算每只目标金额
        price_map = day_prices.to_dict()
        target_val = nav / N_HOLDINGS

        for code in sell_list:
            p = price_map.get(code, 0)
            if not p or pd.isna(p) or p <= 0:
                continue
            shares = int(target_val / p / 100) * 100
            amount = round(shares * p, 2)
            fee    = round(amount * COMMISSION, 2)
            rows.append({
                "调仓日":    str(rebal_dt.date()),
                "方向":      "卖出",
                "代码":      code,
                "名称":      stock_info.set_index("code")["name"].get(code, "") if not stock_info.empty else "",
                "参考价(元)": round(p, 2),
                "手数(手)":  shares // 100,
                "股数(股)":  shares,
                "金额(元)":  amount,
                "手续费(元)": fee,
                "调仓原因":   "移出持仓池",
            })

        for code in buy_list:
            p = price_map.get(code, 0)
            if not p or pd.isna(p) or p <= 0:
                continue
            shares = int(target_val / p / 100) * 100
            amount = round(shares * p, 2)
            fee    = round(amount * COMMISSION, 2)
            rows.append({
                "调仓日":    str(rebal_dt.date()),
                "方向":      "买入",
                "代码":      code,
                "名称":      stock_info.set_index("code")["name"].get(code, "") if not stock_info.empty else "",
                "参考价(元)": round(p, 2),
                "手数(手)":  shares // 100,
                "股数(股)":  shares,
                "金额(元)":  amount,
                "手续费(元)": fee,
                "调仓原因":   "纳入持仓池",
            })

        for code in hold_list:
            p = price_map.get(code, 0)
            rows.append({
                "调仓日":    str(rebal_dt.date()),
                "方向":      "持有",
                "代码":      code,
                "名称":      stock_info.set_index("code")["name"].get(code, "") if not stock_info.empty else "",
                "参考价(元)": round(p, 2) if p > 0 else "--",
                "手数(手)":  "--",
                "股数(股)":  "--",
                "金额(元)":  "--",
                "手续费(元)": "--",
                "调仓原因":   "继续持有",
            })

        prev_holdings = new_holdings

    df = pd.DataFrame(rows)
    out = Path("logs/trades_a_detail.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")

    trades = df[df["方向"].isin(["买入", "卖出"])]
    logger.info(f"交易记录生成完成")
    logger.info(f"  总调仓次数: {df['调仓日'].nunique()} 次")
    logger.info(f"  总交易笔数: {len(trades)} 笔（买入{len(df[df['方向']=='买入'])} 卖出{len(df[df['方向']=='卖出'])}）")
    logger.info(f"  结果文件  : {out}")
    print(f"\n最近3次调仓样本：")
    print(df[df["方向"] != "持有"].tail(15).to_string(index=False))


if __name__ == "__main__":
    main()
