"""
下载中证800历史季报财务数据（2018至今）。
存储字段：report_date / roe_annualized / eps_ttm / bvps
运行一次即可，后续用 daily_data_update.py 增量更新。
"""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd
from loguru import logger

from data.storage import save_financial, load_meta

logger.add("logs/init_financial.log", rotation="1 day", retention="30 days")

_KEY_COLS = {
    "日期":              "report_date",
    "净资产报酬率(%)":   "roe_ytd",
    "摊薄每股收益(元)":  "eps_ytd",
    "每股净资产_调整前(元)": "bvps",
}

_ANNUALIZE = {3: 4, 6: 2, 9: 4 / 3, 12: 1}


def _annualize_row(row: pd.Series) -> pd.Series:
    """将YTD累计指标年化为TTM/年化值。"""
    month = pd.Timestamp(row["report_date"]).month
    f = _ANNUALIZE.get(month, 1)
    return pd.Series({
        "roe_annualized": row["roe_ytd"] * f if pd.notna(row["roe_ytd"]) else None,
        "eps_ttm":        row["eps_ytd"] * f if pd.notna(row["eps_ytd"]) else None,
        "bvps":           row["bvps"],
    })


def _download_one(code: str) -> tuple[str, pd.DataFrame | None]:
    try:
        raw = ak.stock_financial_analysis_indicator(symbol=code, start_year="2018")
        # 只取需要的列（部分接口列名可能缺失，做防御）
        available = {k: v for k, v in _KEY_COLS.items() if k in raw.columns}
        if "日期" not in available:
            return code, None
        df = raw[list(available.keys())].rename(columns=available)
        df["report_date"] = pd.to_datetime(df["report_date"])
        annualized = df.apply(_annualize_row, axis=1)
        result = pd.concat([df[["report_date"]], annualized], axis=1)
        result = result.dropna(subset=["roe_annualized", "eps_ttm"]).reset_index(drop=True)
        return code, result if not result.empty else None
    except Exception as e:
        logger.debug(f"{code} 下载失败: {e}")
        return code, None


def main():
    # 优先用历史宇宙（全量覆盖），回退到 CSI800
    uh = load_meta("universe_history")
    if not uh.empty:
        all_codes = set()
        for _, row in uh.iterrows():
            if row.get("codes"):
                all_codes.update(str(c) for c in row["codes"].split(","))
        codes = sorted(all_codes)
        logger.info(f"从历史宇宙取 {len(codes)} 只")
    else:
        csi800 = load_meta("csi800")
        if csi800.empty:
            logger.error("csi800 数据不存在，请先保存中证800成分股")
            return
        codes = csi800["code"].tolist()

    # 去重：跳过已有数据的股票（增量模式）
    import os as _os
    existing = set(f.stem.zfill(6) for f in Path("data_store/financial").glob("*.parquet"))
    new_codes = [c for c in codes if c not in existing]
    logger.info(f"总{len(codes)}只，已有{len(existing)}只，需下载{len(new_codes)}只（3线程）")
    codes = new_codes
    if not codes:
        logger.info("所有股票已有财务数据，无需下载")
        return

    success, failed = 0, []

    # 3 并发：避免触发东方财富频率限制
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_download_one, c): c for c in codes}
        done = 0
        for fut in as_completed(futures):
            code, df = fut.result()
            done += 1
            if df is not None:
                save_financial(code, df)
                success += 1
            else:
                failed.append(code)
            if done % 50 == 0:
                logger.info(f"进度 {done}/{len(codes)}  成功:{success}  失败:{len(failed)}")

    logger.info("=" * 50)
    logger.info(f"完成: 成功 {success}/{len(codes)}，失败 {len(failed)}")
    if failed:
        logger.warning(f"失败列表: {failed}")
        Path("logs/failed_financial.txt").write_text("\n".join(failed))


if __name__ == "__main__":
    main()
