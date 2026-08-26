"""
重建被污染的指数parquet文件。
问题: daily_data_update.py 遍历全市场代码时，000001/000688/000905/000906
      既是股票代码也是指数代码，akshare前缀逻辑拉到了个股价格而非指数点位。

用法: python scripts/rebuild_index_data.py [--dry-run]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd
from datetime import date
from loguru import logger

logger.add("logs/rebuild_index.log", rotation="7 days")

DATA_STORE = Path("data_store/daily")

# 需要重建的指数: (代码, 名称, akshare指数symbol, 正确取值范围)
INDICES = [
    # 上证2005年前长期低于2000点(998-2245), 下限500才不误杀早期数据
    ("000001", "上证指数", "sh000001", (500, 6000)),
    ("000688", "科创50", "sh000688", (500, 3000)),
    ("000905", "中证500", "sh000905", (4000, 12000)),
    ("000906", "中证800", "sh000906", (3000, 7000)),
]

# akshare stock_zh_index_daily 返回列名映射
COL_MAP = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}

EXPECTED_COLS = ["date", "code", "open", "high", "low", "close", "volume"]


def rebuild_one(code: str, name: str, ak_symbol: str, valid_range: tuple) -> dict:
    """拉取指数日线，按年份写回parquet。返回统计。"""
    result = {"code": code, "name": name, "years": 0, "rows": 0, "errors": []}

    try:
        logger.info(f"拉取 {code} {name} ({ak_symbol})...")
        raw = ak.stock_zh_index_daily(symbol=ak_symbol)
        if raw.empty:
            result["errors"].append("API返回空")
            return result

        # 列名可能略有差异，统一处理
        df = raw.rename(columns={
            "date": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume",
        })

        df["date"] = pd.to_datetime(df["date"])
        df["code"] = code
        df = df.sort_values("date")

        # 验证：收盘价应该在合理范围
        close_min, close_max = valid_range
        valid_mask = (df["close"] >= close_min) & (df["close"] <= close_max)
        if not valid_mask.all():
            bad_dates = df[~valid_mask]["date"].dt.strftime("%Y-%m-%d").tolist()
            logger.warning(f"  {code}: {len(bad_dates)}行收盘价超出范围 {valid_range}, 已过滤")
            df = df[valid_mask]

        # 确保有amount列(指数可能无此列，填0)
        if "amount" not in df.columns:
            df["amount"] = 0.0
        if "pct_chg" not in df.columns:
            df["pct_chg"] = 0.0

        # 按年份分组写回
        for yr, grp in df.groupby(df["date"].dt.year):
            yr_dir = DATA_STORE / str(yr)
            yr_dir.mkdir(exist_ok=True)
            out_path = yr_dir / f"{code}.parquet"

            out = grp[EXPECTED_COLS + ["amount", "pct_chg"]].copy() if "amount" in df.columns else grp[EXPECTED_COLS].copy()
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            out.to_parquet(out_path, index=False)

            result["years"] += 1
            result["rows"] += len(out)
            logger.info(f"  写入 {out_path}: {len(out)}行")

        # 验证写入
        for yr in df["date"].dt.year.unique():
            check = pd.read_parquet(DATA_STORE / str(yr) / f"{code}.parquet")
            if check["close"].iloc[-1] < 100:
                logger.error(f"  ❌ {code} {yr} 写入后仍然 <100! close={check['close'].iloc[-1]:.2f}")
                result["errors"].append(f"year {yr} 验证失败")

    except Exception as e:
        logger.error(f"  ❌ {code} 重建失败: {e}")
        result["errors"].append(str(e))

    return result


def run(dry_run: bool = False):
    today = date.today().strftime("%Y-%m-%d")
    logger.info(f"开始重建指数数据 {today} {'(DRY RUN)' if dry_run else ''}")

    for code, name, ak_symbol, valid_range in INDICES:
        if dry_run:
            logger.info(f"[DRY RUN] 将重建 {code} {name} ({ak_symbol})")
            continue

        # 删除旧文件
        for yr_dir in DATA_STORE.glob("20*"):
            old = yr_dir / f"{code}.parquet"
            if old.exists():
                old.unlink()
                logger.info(f"  删除 {old}")

        result = rebuild_one(code, name, ak_symbol, valid_range)

        status = "✅" if not result["errors"] else "❌"
        logger.info(f"  {status} {code}: {result['years']}年 {result['rows']}行 错误={len(result['errors'])}")

    # 最终验证
    logger.info("── 最终验证 ──")
    for code, name, _, (lo, hi) in INDICES:
        for yr_dir in sorted(DATA_STORE.glob("20*")):
            f = yr_dir / f"{code}.parquet"
            if f.exists():
                df = pd.read_parquet(f)
                cl = df["close"]
                ok = cl.between(lo, hi).all()
                status = "✅" if ok else f"❌ range=[{cl.min():.1f}, {cl.max():.1f}]"
                logger.info(f"  {code} {name} {yr_dir.name}: {status} latest={cl.iloc[-1]:.2f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
