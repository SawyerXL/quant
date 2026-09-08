"""Parquet 存储工具，所有数据读写通过此模块进行。"""
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger
from config.settings import DATA_STORE

# 库内被指数数据占位的代码: daily/{code}.parquet 存的是指数日线(上证/科创50/
# 中证500/中证800), 与深市同名个股(平安银行等)冲突——个股单读这些代码会拿到
# 指数点位。2026-09-06 从 daily_data_update 上移到此单一来源。
INDEX_CODES = {"000001", "000688", "000905", "000906"}
# A股千元股白名单(价格身份断言用): 收盘价>1000 且不在此列的代码=污染数据
PRICE_WHITELIST = {"600519"}  # 贵州茅台(真>1000元)


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
def save_daily(code: str, df: pd.DataFrame) -> int:
    """按年分片写入日线数据。返回被拒绝的行数(价格身份断言+相对跳变检查)。

    2026-09-08: 改为返回拒绝行数, 让 update_today 不再单独读全历史做脏检查
    (此前每票 3 次整文件读, 5500 票 = 一天 6 万+ 次文件操作)。
    """
    if df.empty:
        return 0
    # 清洗：'-' / '暂无数据' 等非数值字段转 NaN，无效日期行丢弃
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if df.empty:
        return 0
    rejected = 0
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
    # ── 价格身份断言(2026-09-06, 000001读回3942点教训): A股无四位数股价,
    # 超千元且非指数/白名单=身份污染(指数点位混入个股库), 直接拒绝该行入库
    if "close" in df.columns:
        over = pd.to_numeric(df["close"], errors="coerce") > 1000
        allowed = code in INDEX_CODES or code in PRICE_WHITELIST
        if over.any() and not allowed:
            n = int(over.sum())
            logger.warning(f"{code}: 拒绝{n}行收盘价>1000(疑似指数点位污染) — 白名单外不入库")
            df = df[~over]
            rejected += n
    # ── 相对跳变检查(2026-09-06 加): 收盘价 vs 前一根健康收盘跳变>50% 且日期
    # ≥1997(涨跌停制度后)拒绝。白名单式绝对断言会随时间失效(新高价股/拆股),
    # 相对检查零维护; 50%阈值与 update_today 脏过滤一致(30%会误拒合法的
    # 44%新股首日涨幅)。qfq库无除权跳变, 主要盲点=长期停牌复牌的真实大涨。
    # 逐行游标而非向量shift: 污染行被拒后, 后续行的prev必须停留在最后一根
    # 健康收盘(向量shift会连坐后一行); 首行prev=库内最后一根收盘
    if "close" in df.columns and "date" in df.columns and not df.empty:
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        store_last = None
        valid_dates = df["date"].dropna()
        if len(valid_dates):
            p = _daily_path(code, int(valid_dates.iloc[0].year))
            if p.exists():
                try:
                    # 只读close列: 日更路径每票省一次整文件读(2026-09-08)
                    ex = pd.read_parquet(p, columns=["close"])
                    if not ex.empty:
                        store_last = float(pd.to_numeric(ex["close"], errors="coerce").iloc[-1])
                except Exception:
                    pass
        drop_idx, prev_close = [], (store_last if store_last and store_last > 0 else None)
        for idx, r in df.iterrows():
            c = r["close"]
            c = float(c) if pd.notna(c) else None
            if (prev_close and c and pd.notna(r["date"])
                    and r["date"] >= pd.Timestamp("1997-01-01")
                    and abs(c / prev_close - 1) > 0.5):
                drop_idx.append(idx)
                continue  # 不更新prev: 后续行仍与最后健康收盘比
            if c and c > 0:
                prev_close = c
        if drop_idx:
            logger.warning(f"{code}: 拒绝{len(drop_idx)}行相对前收跳变>50%(疑似数据错配)")
            df = df.drop(index=drop_idx)
            rejected += len(drop_idx)
    for year, grp in df.groupby(df["date"].dt.year):
        path = _daily_path(code, year)
        if path.exists():
            existing = pd.read_parquet(path)
            # 存量库被全库重算脚本写成str日期(f95f2c4), 与入参Timestamp混合后
            # sort_values报"<' not supported..."(9/2起增量更新全挂的根因); 读回时统一归化
            existing["date"] = pd.to_datetime(existing["date"])
            grp = pd.concat([existing, grp]).drop_duplicates("date", keep="last").sort_values("date")
        grp.to_parquet(path, index=False)
    return rejected


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
            existing["datetime"] = pd.to_datetime(existing["datetime"])  # 同save_daily防混型
            grp = pd.concat([existing, grp]).drop_duplicates("datetime").sort_values("datetime")
        grp.to_parquet(path, index=False)


# ---------- 读取 ----------
def load_daily(code: str, start: str, end: str, columns: list | None = None) -> pd.DataFrame:
    """读取本地 Parquet 日线数据，跨年自动合并。

    columns 只读指定列(date 自动补入): 补洞检查等只需要日期, 传
    columns=["date"] 省掉整文件解压(2026-09-08, 5500票全扫的IO放大修复)。
    """
    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    if columns is not None:
        cols = list(columns)
        if "date" not in cols:
            cols.append("date")
    else:
        cols = None
    frames = []
    for year in range(start_dt.year, end_dt.year + 1):
        path = _daily_path(code, year)
        if path.exists():
            df = pd.read_parquet(path, columns=cols)
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
