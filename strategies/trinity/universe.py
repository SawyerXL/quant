"""
Track B 动态股票池构建。
每次调仓日调用，返回当日可交易的标的列表。
"""
import pandas as pd
from loguru import logger


def build_universe(
    date: str,
    stock_info: pd.DataFrame,
    price_panel: pd.DataFrame,
    min_listed_days: int = 120,
) -> list[str]:
    """
    从全市场过滤出当日可用的股票池。

    过滤规则：
    1. 剔除 ST/*ST/退市（stock_info.is_st == True）
    2. 剔除上市不足 min_listed_days 天的新股
    3. 剔除当日停牌（价格面板当日无收盘价）
    4. 必须有足够历史数据（至少 35 根 K 线，保证 MA30 计算）
    """
    date_ts = pd.Timestamp(date)
    all_codes = set(price_panel.columns)

    # ── 1. ST 过滤 ────────────────────────────────────
    if "is_st" in stock_info.columns:
        st_codes = set(stock_info.loc[stock_info["is_st"] == True, "code"].tolist())
    else:
        # 降级：从名称判断
        st_codes = set(stock_info.loc[
            stock_info["name"].str.contains(r"ST|\*ST|退市", na=False, regex=True),
            "code"
        ].tolist())
    all_codes -= st_codes

    # ── 2. 新股过滤 ───────────────────────────────────
    if "list_date" in stock_info.columns:
        recent_ipo = stock_info[
            pd.to_datetime(stock_info["list_date"], errors="coerce") >
            date_ts - pd.Timedelta(days=min_listed_days)
        ]["code"].tolist()
        all_codes -= set(recent_ipo)

    # ── 3. 停牌过滤 + 数据充足性过滤 ─────────────────
    hist = price_panel[price_panel.index <= date_ts]
    valid = []
    for code in all_codes:
        if code not in hist.columns:
            continue
        col = hist[code].dropna()
        if len(col) < 35:          # 至少35条，保证 MA30 计算
            continue
        if pd.isna(col.iloc[-1]):  # 当日停牌
            continue
        valid.append(code)

    logger.debug(f"{date} 股票池: 过滤后 {len(valid)} 只 "
                 f"（ST剔除:{len(st_codes)&len(set(price_panel.columns))} "
                 f"总原始:{len(price_panel.columns)}）")
    return valid
