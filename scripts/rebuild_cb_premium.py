#!/usr/bin/env python3
"""
补全可转债日线数据 — 添加转股溢价率(从bond_zh_cov_value_analysis)
同时补充缺失/不完整的日线数据

用法:
  python scripts/rebuild_cb_premium.py              # 全量补(约4-8分钟)
  python scripts/rebuild_cb_premium.py --check       # 仅检查覆盖率,不下载
  python scripts/rebuild_cb_premium.py --batch 30    # 自定义批次大小
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime
from loguru import logger
import time

DAILY_DIR = Path("data_store/convertible_bonds/daily")
DAILY_DIR.mkdir(exist_ok=True)
BATCH_SLEEP = 2.0  # 批次间休息秒数


def get_existing_stats():
    """统计现有数据覆盖情况."""
    files = list(DAILY_DIR.glob('*.parquet'))
    stats = []
    for f in files:
        df = pd.read_parquet(f)
        code = f.stem
        has_premium = 'premium' in df.columns and df['premium'].notna().any()
        n_rows = len(df)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            last_date = df['date'].max()
        else:
            last_date = None
        stats.append({
            'code': code,
            'rows': n_rows,
            'has_premium': has_premium,
            'last_date': last_date,
        })
    return pd.DataFrame(stats)


def fetch_and_merge(code, fpath):
    """
    拉取单只转债的value_analysis数据(含溢价率),
    合并到现有日线文件中.
    返回: (新增行数, 是否补了溢价率)
    """
    try:
        new_df = ak.bond_zh_cov_value_analysis(symbol=code)
    except Exception:
        return 0, False

    if new_df.empty:
        return 0, False

    new_df = new_df.rename(columns={
        '日期': 'date',
        '收盘价': 'close',
        '转股溢价率': 'premium',
        '纯债价值': 'bond_value',
        '转股价值': 'convert_value',
        '纯债溢价率': 'bond_premium',
    })
    new_df['date'] = pd.to_datetime(new_df['date'])

    # 保留有用的列
    keep_cols = ['date', 'close', 'premium']
    for c in ['bond_value', 'convert_value', 'bond_premium']:
        if c in new_df.columns:
            keep_cols.append(c)
    new_df = new_df[keep_cols].copy()

    # 加载现有数据
    if fpath.exists():
        old_df = pd.read_parquet(fpath)
        old_df['date'] = pd.to_datetime(old_df['date'])

        # 检查是否已有premium
        had_premium = 'premium' in old_df.columns and old_df['premium'].notna().any()

        # 合并: 新数据的premium写到旧数据中
        if 'premium' not in old_df.columns:
            old_df['premium'] = np.nan

        # Merge on date, keeping old OHLCV but adding new premium
        merged = old_df.merge(
            new_df[['date', 'premium'] + [c for c in keep_cols if c not in ('date', 'close', 'premium')]],
            on='date', how='left', suffixes=('', '_new')
        )
        # Fill missing premium from new data
        for col in ['premium'] + [c for c in keep_cols if c not in ('date', 'close', 'premium')]:
            new_col = f'{col}_new'
            if new_col in merged.columns:
                merged[col] = merged[col].fillna(merged[new_col])
                merged.drop(columns=[new_col], inplace=True)

        # Add any new dates from value_analysis not in old
        old_dates = set(old_df['date'])
        new_dates = new_df[~new_df['date'].isin(old_dates)]
        if not new_dates.empty:
            merged = pd.concat([merged, new_dates], ignore_index=True)
            merged = merged.sort_values('date').reset_index(drop=True)

        new_rows = len(new_dates)
        added_premium = not had_premium and 'premium' in merged.columns
    else:
        merged = new_df
        new_rows = len(new_df)
        added_premium = True

    merged.to_parquet(fpath, index=False)
    return new_rows, added_premium


def main(check_only=False, batch_size=50):
    # 获取全量转债列表
    logger.info("获取转债全量列表...")
    try:
        universe = ak.bond_zh_cov()
    except Exception as e:
        logger.error(f"获取列表失败: {e}")
        return

    # 过滤已上市
    universe['上市时间'] = pd.to_datetime(universe['上市时间'], errors='coerce')
    today = datetime.now()
    listed = universe[universe['上市时间'].notna() & (universe['上市时间'] <= today)]
    codes = listed['债券代码'].astype(str).str.zfill(6).tolist()
    logger.info(f"已上市: {len(codes)}只")

    if check_only:
        stats = get_existing_stats()
        existing_codes = set(stats['code'].tolist()) if not stats.empty else set()
        missing = [c for c in codes if c not in existing_codes]
        no_premium = stats[~stats['has_premium']]['code'].tolist() if not stats.empty else []

        print(f"\n  数据覆盖检查:")
        print(f"  已上市: {len(codes)}")
        print(f"  有日线文件: {len(existing_codes)} ({len(existing_codes)/len(codes)*100:.0f}%)")
        print(f"  无日线文件: {len(missing)}")
        print(f"  缺溢价率: {len(no_premium)}")
        if not stats.empty:
            stale = stats[stats['last_date'] < pd.Timestamp('2026-07-01')]
            print(f"  数据停在7月前: {len(stale)}")
        return

    # 逐只补数据
    total = len(codes)
    new_total = 0
    premium_total = 0
    errors = 0

    for i, code in enumerate(codes):
        fpath = DAILY_DIR / f"{code}.parquet"

        try:
            new_rows, added_premium = fetch_and_merge(code, fpath)
            new_total += new_rows
            if added_premium:
                premium_total += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning(f"  {code} 失败: {e}")

        # 批次进度
        if (i + 1) % batch_size == 0:
            logger.info(f"  [{i+1}/{total}] 新增{new_total}行, 补溢价{premium_total}只, 错{errors}")
            time.sleep(BATCH_SLEEP)

    logger.info(f"完成: {total}只, 新增{new_total}行, 补溢价率{premium_total}只, 失败{errors}")


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='补全CB日线溢价率数据')
    p.add_argument('--check', action='store_true', help='仅检查覆盖率')
    p.add_argument('--batch', type=int, default=50, help='批次大小(默认50)')
    args = p.parse_args()

    main(check_only=args.check, batch_size=args.batch)
