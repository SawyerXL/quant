"""
数据质量校验脚本。init_history.py 完成后自动运行。
检查项：
  1. 下载完整性（有多少只股票成功下载）
  2. 交易日覆盖率（与标准交易日历对比）
  3. 价格异常值（单日涨跌幅超过50%）
  4. 沪深300对标验证（000001平安银行关键价格点）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_daily, load_meta

logger.add("logs/validate_data.log", rotation="1 day", retention="30 days")


def check_coverage(data_dir: Path, trade_calendar: list) -> dict:
    """检查下载完整性。"""
    all_files = list(data_dir.glob("**/*.parquet"))
    codes = list({f.stem for f in all_files})

    n_trade_days = len(trade_calendar)
    coverage_list = []
    for code in codes[:200]:   # 抽样200只
        df = load_daily(code, trade_calendar[0], trade_calendar[-1])
        if df.empty:
            continue
        rate = len(df) / n_trade_days
        coverage_list.append(rate)

    return {
        "total_stocks":    len(codes),
        "total_files":     len(all_files),
        "sample_coverage": f"{np.mean(coverage_list):.1%}" if coverage_list else "N/A",
        "min_coverage":    f"{np.min(coverage_list):.1%}"  if coverage_list else "N/A",
    }


def check_price_anomaly(codes: list, start: str, end: str) -> dict:
    """检查价格异常（单日涨跌幅>50%，非停牌复牌情况）。"""
    anomalies = []
    sample = codes[:500]   # 抽样500只
    for code in sample:
        df = load_daily(code, start, end)
        if df.empty or "pct_chg" not in df.columns:
            continue
        df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
        bad = df[df["pct_chg"].abs() > 50]
        if not bad.empty:
            anomalies.append({"code": code, "count": len(bad)})
    return {"checked": len(sample), "with_anomaly": len(anomalies), "samples": anomalies[:5]}


def check_benchmark(start="2024-01-01", end="2024-12-31") -> dict:
    """
    用平安银行(000001)2024年数据做基准验证：
    2024-01-02 开盘 ≈ 11.x，2024-12-31 收盘 ≈ 10.x（前复权）
    """
    df = load_daily("000001", start, end)
    if df.empty:
        return {"status": "FAIL", "reason": "000001 数据缺失"}
    df = df.sort_values("date")
    first_close = float(df.iloc[0]["close"]) if "close" in df.columns else None
    last_close  = float(df.iloc[-1]["close"]) if "close" in df.columns else None

    # 合理范围：前复权价格应在 5-30 元之间（异常值说明复权方式有问题）
    ok = (first_close and 5 < first_close < 30) and (last_close and 5 < last_close < 30)
    return {
        "status":      "PASS" if ok else "WARN",
        "first_close": first_close,
        "last_close":  last_close,
        "rows":        len(df),
    }


def main():
    logger.info("=" * 60)
    logger.info("数据质量校验开始")
    logger.info("=" * 60)

    data_dir = Path("data_store/daily")
    if not data_dir.exists():
        logger.error("data_store/daily 目录不存在，init_history 未运行？")
        return

    # 交易日历
    cal_df = load_meta("trade_calendar")
    trade_calendar = cal_df["trade_date"].tolist() if not cal_df.empty else []

    # 1. 覆盖率
    logger.info("── 1. 下载完整性 ──")
    cov = check_coverage(data_dir, trade_calendar)
    logger.info(f"  成功下载股票数 : {cov['total_stocks']}")
    logger.info(f"  Parquet 文件数  : {cov['total_files']}")
    logger.info(f"  平均交易日覆盖率: {cov['sample_coverage']}（抽样200只）")
    logger.info(f"  最低覆盖率      : {cov['min_coverage']}")

    # 2. 异常价格
    logger.info("── 2. 价格异常检测 ──")
    codes = [f.stem for f in data_dir.glob("**/*.parquet")]
    codes = list(set(codes))
    if trade_calendar:
        anom = check_price_anomaly(codes, trade_calendar[0], trade_calendar[-1])
        logger.info(f"  抽样检查       : {anom['checked']} 只")
        logger.info(f"  发现异常股票数  : {anom['with_anomaly']} 只")
        if anom["samples"]:
            logger.info(f"  异常样例       : {anom['samples']}")
    else:
        logger.warning("  交易日历缺失，跳过价格异常检测")

    # 3. 基准验证
    logger.info("── 3. 基准价格验证（000001 平安银行 2024年）──")
    bench = check_benchmark()
    logger.info(f"  状态        : {bench['status']}")
    logger.info(f"  年初收盘价  : {bench.get('first_close')} 元")
    logger.info(f"  年末收盘价  : {bench.get('last_close')} 元")
    logger.info(f"  数据行数    : {bench.get('rows')}")

    # 总结
    logger.info("=" * 60)
    status = "PASS" if int(cov['total_stocks']) > 4000 else "WARN"
    summary = (
        f"校验完成 [{status}] | "
        f"股票数:{cov['total_stocks']} | "
        f"文件数:{cov['total_files']} | "
        f"覆盖率:{cov['sample_coverage']} | "
        f"基准:{bench['status']}"
    )
    logger.info(summary)
    logger.info("详细日志 → logs/validate_data.log")


if __name__ == "__main__":
    main()
