"""Parquet 存储工具，所有数据读写通过此模块进行。"""
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger
from config.settings import DATA_STORE


# ---------- 路径规则 ----------
def _daily_path(code: str, year: int) -> Path:
    p = DATA_STORE / "daily" / str(year)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{code}.parquet"

def _financial_path(code: str) -> Path:
    p = DATA_STORE / "financial"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{code}.parquet"

def _meta_path(name: str) -> Path:
    p = DATA_STORE / "meta"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{name}.parquet"

def _intraday_path(code: str, freq: str, year: int) -> Path:
    p = DATA_STORE / "intraday" / freq / str(year)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{code}.parquet"


# ---------- 写入 ----------
def save_daily(code: str, df: pd.DataFrame) -> None:
    """按年分片写入日线数据。"""
    if df.empty:
        return
    # 清洗：'-' / '暂无数据' 等非数值字段转 NaN，无效日期行丢弃
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if df.empty:
        return
    # code 列统一为6位零填充字符串
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.zfill(6)
    # 丢弃已知问题列（akshare格式不稳定）
    for bad in ["量比","成交笔数（笔）","换手率","振幅（%）","涨跌幅","前收盘（元）","均价（元）","涨跌（元）","股票名称","股票代码"]:
        if bad in df.columns: df=df.drop(columns=[bad])
    for col in df.select_dtypes(include="object").columns:
        if col not in ("date", "code"):
            try:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            except Exception:
                df.drop(columns=[col], inplace=True)  # 转换失败的辅助列直接丢弃
    for year, grp in df.groupby(df["date"].dt.year):
        path = _daily_path(code, year)
        if path.exists():
            existing = pd.read_parquet(path)
            grp = pd.concat([existing, grp]).drop_duplicates("date", keep="last").sort_values("date")
        grp.to_parquet(path, index=False)


def save_financial(code: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    _financial_path(code).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_financial_path(code), index=False)


def save_meta(name: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    df.to_parquet(_meta_path(name), index=False)


def save_intraday(code: str, freq: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    df["datetime"] = pd.to_datetime(df["datetime"])
    for year, grp in df.groupby(df["datetime"].dt.year):
        path = _intraday_path(code, freq, year)
        if path.exists():
            existing = pd.read_parquet(path)
            grp = pd.concat([existing, grp]).drop_duplicates("datetime").sort_values("datetime")
        grp.to_parquet(path, index=False)


# ---------- 读取 ----------
def load_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """读取本地 Parquet 日线数据，跨年自动合并。"""
    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    frames = []
    for year in range(start_dt.year, end_dt.year + 1):
        path = _daily_path(code, year)
        if path.exists():
            df = pd.read_parquet(path)
            df["date"] = pd.to_datetime(df["date"])
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames).sort_values("date")
    return result[(result["date"] >= start_dt) & (result["date"] <= end_dt)].reset_index(drop=True)


def load_daily_cross_section(date: str) -> pd.DataFrame:
    """
    读取全市场某日截面数据（用于因子计算）。
    扫描所有 code 对应的 Parquet，筛选指定日期行。
    注意：数据量大时建议缓存。
    """
    date_ts = pd.Timestamp(date)
    year = date_ts.year
    daily_dir = DATA_STORE / "daily" / str(year)
    if not daily_dir.exists():
        logger.warning(f"本地无 {year} 年日线数据，请先运行 daily_data_update.py")
        return pd.DataFrame()

    frames = []
    for f in daily_dir.glob("*.parquet"):
        df = pd.read_parquet(f, filters=[("date", "=", date_ts)])
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).reset_index(drop=True)


def load_financial(code: str) -> pd.DataFrame:
    path = _financial_path(code)
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def load_meta(name: str) -> pd.DataFrame:
    path = _meta_path(name)
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def load_intraday(code: str, freq: str, start: str, end: str) -> pd.DataFrame:
    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    frames = []
    for year in range(start_dt.year, end_dt.year + 1):
        path = _intraday_path(code, freq, year)
        if path.exists():
            df = pd.read_parquet(path)
            df["datetime"] = pd.to_datetime(df["datetime"])
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames).sort_values("datetime")
    return result[(result["datetime"] >= start_dt) & (result["datetime"] <= end_dt)].reset_index(drop=True)
