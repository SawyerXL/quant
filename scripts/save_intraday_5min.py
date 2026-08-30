"""
每日5分钟线落库 —— 只对持仓票+指数(数据量小, 新浪接口限流敏感, 批量调用需间隔)。

数据源: ak.stock_zh_a_minute(sina, 一次返回最近约40个交易日)。
首跑自动补齐全部可拉历史, 之后每日增量append去重。
落库: data_store/intraday/5min/{code}.parquet (day/open/high/low/close/volume)

cron: 2 16 * * 1-5 (错开15:45做T结算, 避免同一接口连打触发限流)
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import csv
import pandas as pd
import akshare as ak
from loguru import logger

logger.add("logs/intraday_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days")

OUT_DIR = Path("data_store/intraday/5min")
CB_PREFIX = ("110", "111", "113", "118", "123", "127", "128")
SKIP = {"718605", "400286"}   # 发债/老三板无5分钟线


def sina_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def targets():
    codes = ["000001"]   # 上证指数
    try:
        with open("config/my_holdings.csv") as f:
            for r in csv.DictReader(f):
                c = str(r.get("code", "")).strip().zfill(6)
                if c in SKIP or c[:3] in CB_PREFIX:
                    continue
                codes.append(c)
    except Exception:
        pass
    return sorted(set(codes))


def fetch(code: str) -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_minute(symbol=sina_symbol(code), period="5", adjust="")
            df["day"] = pd.to_datetime(df["day"])
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.dropna(subset=["high", "low"]).sort_values("day")
        except Exception as e:
            if attempt == 2:
                logger.warning(f"  {code} 拉取失败: {type(e).__name__} {str(e)[:60]}")
            time.sleep(3 * (attempt + 1))
    return pd.DataFrame()


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = targets()
    logger.info(f"5分钟线落库: {len(codes)}个标的")
    ok = 0
    for i, code in enumerate(codes):
        df = fetch(code)
        if df.empty:
            continue
        path = OUT_DIR / f"{code}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            old["day"] = pd.to_datetime(old["day"])
            df = pd.concat([old, df]).drop_duplicates("day", keep="last").sort_values("day")
        df.to_parquet(path, index=False)
        ok += 1
        logger.info(f"  {code}: 共{len(df)}根, 最新{str(df['day'].max())[:16]}")
        time.sleep(1.5)   # 新浪接口限流敏感
    logger.info(f"完成: {ok}/{len(codes)}")


if __name__ == "__main__":
    main()
