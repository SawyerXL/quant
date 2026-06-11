"""
Track B 数据缓存层：涨停/跌停统计、板块成分股、流通市值。
首次运行自动从 akshare 拉取并缓存到 data_store/meta/。
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta
from loguru import logger

META_DIR = Path("data_store/meta")

# ── 涨停/跌停统计 ──────────────────────────────────────────
def get_limit_up_down_stats(trade_date: str) -> dict:
    """
    返回当日涨停/跌停/炸板统计。
    trade_date: 'YYYY-MM-DD'
    返回: {limit_up_count, limit_down_count, blowup_count, touch_count}
    """
    import akshare as ak
    cache_file = META_DIR / f"limit_stats_{trade_date.replace('-','')}.json"
    if cache_file.exists():
        import json
        return json.loads(cache_file.read_text(encoding="utf-8"))

    try:
        # 涨停池
        df_up = ak.stock_zt_pool_em(date=trade_date.replace("-", ""))
        up_count = len(df_up[~df_up["名称"].str.contains("ST", na=False)]) if "名称" in df_up.columns else len(df_up)
    except Exception:
        up_count, df_up = 0, pd.DataFrame()

    try:
        # 跌停池
        df_dn = ak.stock_zt_pool_dtgc_em(date=trade_date.replace("-", ""))
        dn_count = len(df_dn[~df_dn["名称"].str.contains("ST", na=False)]) if "名称" in df_dn.columns else len(df_dn)
    except Exception:
        dn_count = 0

    # 炸板率 = 开板数/触板数（用涨停池的封板统计近似）
    blowup = 0; touch = up_count
    if not df_up.empty and "涨停统计" in df_up.columns:
        # 涨停统计列含"炸板"等标记
        blowup = df_up["涨停统计"].astype(str).str.contains("炸板").sum()

    result = {
        "date": trade_date, "limit_up": up_count,
        "limit_down": dn_count, "blowup": blowup, "touch": touch,
        "blowup_rate": round(blowup / max(touch, 1), 4),
    }
    import json
    META_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False))
    return result


def get_limit_stats_series(start: str, end: str) -> pd.DataFrame:
    """获取区间内每日涨停统计，返回 DataFrame(date, limit_up, limit_down, blowup_rate)"""
    start_dt = date.fromisoformat(start)
    end_dt   = date.fromisoformat(end)
    rows = []
    d = start_dt
    while d <= end_dt:
        try:
            s = get_limit_up_down_stats(d.strftime("%Y-%m-%d"))
            rows.append(s)
        except Exception:
            pass
        d += timedelta(days=1)
    return pd.DataFrame(rows)


# ── 板块成分股 ─────────────────────────────────────────────
def get_sector_members(industry_col: str = "industry_l1") -> dict[str, list[str]]:
    """从 stock_info_full 构建行业→股票列表映射。缓存到 parquet。"""
    cache_file = META_DIR / f"sector_members_{industry_col}.parquet"
    if cache_file.exists():
        return pd.read_parquet(cache_file).set_index("industry")["codes"].apply(lambda x: x.split(",")).to_dict()

    from data.storage import load_meta
    info = load_meta("stock_info_full")
    if info.empty:
        return {}
    info["code"] = info["code"].astype(str).str.zfill(6)
    if industry_col not in info.columns:
        logger.warning(f"{industry_col} 不存在，回退到 industry_l1")
        industry_col = "industry_l1"
    mapping = info.groupby(industry_col)["code"].apply(lambda x: ",".join(x)).reset_index()
    mapping.columns = ["industry", "codes"]
    mapping.to_parquet(cache_file)
    return mapping.set_index("industry")["codes"].apply(lambda x: x.split(",")).to_dict()


# ── 流通市值 ───────────────────────────────────────────────
def get_mktcap(date_str: str) -> pd.Series:
    """获取当日A股流通市值（亿元）。缓存到 parquet。"""
    cache_file = META_DIR / f"mktcap_{date_str.replace('-','')}.parquet"
    if cache_file.exists():
        s = pd.read_parquet(cache_file)
        return s.set_index("code")["mktcap"] if "code" in s.columns else pd.Series(dtype=float)

    import akshare as ak
    try:
        df = ak.stock_zh_a_spot_em()
        df["code"] = df["代码"].astype(str).str.zfill(6)
        mktcap = df[["code", "流通市值"]].copy()
        mktcap.columns = ["code", "mktcap"]
        mktcap["mktcap"] = pd.to_numeric(mktcap["mktcap"], errors="coerce") / 1e8  # 元→亿
        mktcap = mktcap.dropna()
        mktcap.to_parquet(cache_file)
        return mktcap.set_index("code")["mktcap"]
    except Exception as e:
        logger.warning(f"流通市值获取失败: {e}")
        return pd.Series(dtype=float)
