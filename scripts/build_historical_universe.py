"""
构建历史股票池（解决幸存者偏差）。

方法：流动性代理法（批量面板加载，高效版）
  在每个半年度时点，按过去6个月日均成交额排名取前800只。
  包含所有历史股票，避免幸存者偏差。

输出：data_store/meta/universe_history.parquet
  字段：date, count, codes（逗号分隔）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from loguru import logger
from data.storage import load_meta, save_meta, load_daily

logger.add("logs/build_universe.log", rotation="1 day")

UNIVERSE_SIZE = 800
MIN_AMOUNT    = 500    # 万元/日
MIN_HIST_DAYS = 20     # 至少有20天数据才纳入排名
LOOKBACK_DAYS = 126    # 6个月


def get_rebalance_dates(start="2016-06-01", end="2025-12-31"):
    cal = load_meta("trade_calendar")
    dates = sorted([d for d in cal["trade_date"].tolist() if start <= d <= end])
    result = []
    for yr in range(2016, 2026):
        for mo in [6, 12]:
            mo_dates = [d for d in dates if d.startswith(f"{yr}-{mo:02d}")]
            if mo_dates:
                result.append(mo_dates[-1])
    return result


def load_amount_panel(all_codes, start, end):
    """批量加载成交额面板"""
    frames = {}
    for code in all_codes:
        df = load_daily(code, start, end)
        if df.empty or "amount" not in df.columns:
            continue
        df["date"] = pd.to_datetime(df["date"])
        amt = pd.to_numeric(df.set_index("date")["amount"], errors="coerce")
        # 单位自适应（部分老数据单位是元而非万元）
        if amt.median() > 1e7:
            amt = amt / 10000
        if len(amt.dropna()) >= MIN_HIST_DAYS:
            frames[code] = amt
    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames).sort_index()


def build_universe_history():
    logger.info("=" * 60)
    logger.info("构建历史股票池（流动性代理法）")
    logger.info("=" * 60)

    rebal_dates = get_rebalance_dates()
    logger.info(f"时点数: {len(rebal_dates)} ({rebal_dates[0]} ~ {rebal_dates[-1]})")

    info_df = load_meta("stock_info_full")
    if not info_df.empty:
        info_df["code"] = info_df["code"].astype(str).str.zfill(6)
    st_codes = set(info_df[info_df.get("is_st", pd.Series(False, index=info_df.index)) == True]["code"]) \
        if not info_df.empty and "is_st" in info_df.columns else set()
    list_dates = {} if info_df.empty or "list_date" not in info_df.columns else \
        info_df.set_index("code")["list_date"].dropna().to_dict()

    # 从本地日线目录获取所有历史股票代码
    daily_dir = Path("data_store/daily")
    all_codes = set()
    for year_dir in daily_dir.iterdir():
        if year_dir.is_dir():
            for f in year_dir.glob("*.parquet"):
                all_codes.add(f.stem)
    all_codes = sorted(all_codes)
    logger.info(f"本地股票数（含历史）: {len(all_codes)} 只")

    # 按年度分块加载，避免一次性加载全部
    records = []
    years_needed = sorted(set(d[:4] for d in rebal_dates))

    # 加载所有年份的成交额数据（按年分段）
    logger.info("加载成交额面板（按年分段）...")
    amount_cache = {}  # year -> DataFrame

    for yr in years_needed:
        start = f"{int(yr)-1}-07-01"   # 往前多取半年保证lookback
        end   = f"{yr}-12-31"
        logger.info(f"  加载 {start} ~ {end}...")
        panel = load_amount_panel(all_codes, start, end)
        if not panel.empty:
            amount_cache[yr] = panel
            logger.info(f"    → {panel.shape[1]}只股票，{panel.shape[0]}天")

    # 对每个时点生成股票池
    for date in rebal_dates:
        yr = date[:4]
        panel = amount_cache.get(yr)
        if panel is None or panel.empty:
            logger.warning(f"{date}: 无成交额数据，跳过")
            records.append({"date": date, "count": 0, "codes": ""})
            continue

        date_ts = pd.Timestamp(date)
        # 取回望期内的数据
        hist = panel[panel.index <= date_ts].tail(LOOKBACK_DAYS)
        if len(hist) < MIN_HIST_DAYS:
            logger.warning(f"{date}: 数据不足 {len(hist)} 天，跳过")
            records.append({"date": date, "count": 0, "codes": ""})
            continue

        avg_amt = hist.mean()

        # 过滤
        filtered = {}
        for code, val in avg_amt.items():
            if pd.isna(val) or val < MIN_AMOUNT:
                continue
            if code in st_codes:
                continue
            ld = list_dates.get(code)
            if ld:
                try:
                    if (date_ts - pd.Timestamp(ld)).days < 252:
                        continue
                except Exception:
                    pass
            filtered[code] = val

        # 排序取前800
        top = sorted(filtered, key=filtered.get, reverse=True)[:UNIVERSE_SIZE]
        records.append({"date": date, "count": len(top), "codes": ",".join(top)})
        logger.info(f"{date}: 选出 {len(top)} 只（候选 {len(filtered)} 只）")

    df_result = pd.DataFrame(records)
    save_meta("universe_history", df_result)
    logger.info(f"\n✅ 历史股票池已保存")
    print(df_result[["date","count"]].to_string(index=False))
    return df_result


if __name__ == "__main__":
    build_universe_history()
