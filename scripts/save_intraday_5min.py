"""
每日5分钟线落库 —— 持仓票+策略信号持仓+指数(数据量小, 新浪接口限流敏感, 批量调用需间隔)。

数据源: ak.stock_zh_a_minute(sina, 一次返回最近约40个交易日)。
首跑自动补齐全部可拉历史, 之后每日增量append去重。
落库: data_store/intraday/5min/{code}.parquet (day/open/high/low/close/volume)

2026-10-31 修复/扩展:
- 旧版 `codes=["000001"]` 注释为“上证指数”，但 sina_symbol 映射成 sz000001
  → 落盘的是平安银行(11元)，不是上证指数(3600点)。现指数用 `sh000001`，
  `000001.parquet` 保留为个股(平安银行)，两者不再混。
- 新增策略信号持仓：signal_a_g0/g1 + signal_cb_latest；
  为 C0005（14:30 vs 收盘竞价执行）积累股票/CB 5分钟数据。
- 新浪支持可转债5分钟线（实测 sh110092 1970根到 2026-10-30）。

cron: 2 16 * * 1-5 (错开15:45做T结算, 避免同一接口连打触发限流)
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import csv
import json
import pandas as pd
import akshare as ak
from loguru import logger

logger.add("logs/intraday_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days")

OUT_DIR = Path("data_store/intraday/5min")
CB_PREFIX = ("110", "111", "113", "118", "123", "127", "128")
SKIP = {"718605", "400286"}   # 发债/老三板无5分钟线
SIGNAL_FILES = (
    "data_store/meta/signal_a_g0.json",
    "data_store/meta/signal_a_g1.json",
    "data_store/meta/signal_cb_latest.json",
)


def sina_symbol(code: str) -> str:
    """6位码→新浪符号；已带 sh/sz 前缀的原样返回（指数用 sh000001）。"""
    code = str(code).strip()
    if code.startswith(("sh", "sz")):
        return code
    if code.startswith(("110", "111", "113", "118", "5", "6", "9")):
        return "sh" + code
    return "sz" + code


def _json_holdings(path) -> list[str]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return [str(x).zfill(6) for x in d.get("holdings", []) if x]
    except Exception:
        return []


def targets(holdings_csv="config/my_holdings.csv", signal_files=SIGNAL_FILES) -> list[str]:
    codes = {"sh000001"}   # 上证指数；000001 是个股(平安银行)，不能混
    try:
        with open(holdings_csv) as f:
            for r in csv.DictReader(f):
                c = str(r.get("code", "")).strip().zfill(6)
                if c and c not in SKIP:
                    codes.add(c)
    except Exception:
        pass
    for p in signal_files:
        codes.update(_json_holdings(p))
    return sorted(c for c in codes if c not in SKIP)


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
