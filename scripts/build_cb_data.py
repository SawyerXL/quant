"""
可转债数据管道 — Stage 1: 基准指数 + 全量日线 + 强赎退市记录

阶段:
  1. 中证转债指数(000832) — 基准收益曲线
  2. 可转债全量列表 — 代码/上市日/退市日/评级/规模
  3. 逐只日线数据 — 价格/转股溢价率/成交额
  4. 强赎/退市记录 — 幸存者偏差处理
  5. point-in-time快照 — 每月调仓日可用标的

用法: python scripts/build_cb_data.py [--stage 1|2|3|4|5]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import datetime, timedelta
from loguru import logger
from data.storage import save_meta, load_meta
import time
import akshare as ak

CB_DIR = Path("data_store/convertible_bonds")
CB_DIR.mkdir(exist_ok=True)
DAILY_DIR = CB_DIR / "daily"
DAILY_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════
# Stage 1: 中证转债指数
# ══════════════════════════════════════════════════════════════════
def build_index():
    """获取中证转债指数日线数据(000832)。"""
    logger.info("Stage 1: 中证转债指数...")

    dfs = []
    # akshare bond_zh_hs_daily 对 sh000832 只到2018年
    # 用 bond_cb_index_jsl 获取近期数据(含 idx_price)
    try:
        idx_jsl = ak.bond_cb_index_jsl()
        if not idx_jsl.empty:
            idx_jsl['date'] = pd.to_datetime(idx_jsl['price_dt'])
            idx_jsl = idx_jsl.rename(columns={'idx_price': 'close'})
            dfs.append(idx_jsl[['date', 'close']])
            logger.info(f"  bond_cb_index_jsl: {len(idx_jsl)}行, {idx_jsl.date.min().date()}→{idx_jsl.date.max().date()}")
    except Exception as e:
        logger.warning(f"  bond_cb_index_jsl失败: {e}")

    # Also try stock_zh_index_daily for 000832 (中证转债指数)
    try:
        idx_em = ak.stock_zh_index_daily(symbol="sh000832")
        if not idx_em.empty:
            idx_em = idx_em.rename(columns={'date': 'date', 'close': 'close'})
            if 'date' in idx_em.columns:
                idx_em['date'] = pd.to_datetime(idx_em['date'])
                dfs.append(idx_em[['date', 'close']])
                logger.info(f"  stock_zh_index_daily: {len(idx_em)}行, {idx_em.date.min().date()}→{idx_em.date.max().date()}")
    except Exception as e:
        logger.warning(f"  stock_zh_index_daily失败: {e}")

    if dfs:
        idx_all = pd.concat(dfs).drop_duplicates('date').sort_values('date')
        idx_all['close'] = pd.to_numeric(idx_all['close'], errors='coerce')
        idx_all = idx_all.dropna()
        save_meta("cb_index", idx_all)
        logger.info(f"  ✓ 转债指数: {len(idx_all)}行, {idx_all.date.min().date()}→{idx_all.date.max().date()}")
    else:
        logger.error("  无法获取转债指数数据")

# ══════════════════════════════════════════════════════════════════
# Stage 2: 可转债全量列表
# ══════════════════════════════════════════════════════════════════
def build_universe():
    """获取所有可转债的基本信息: 代码/上市日/退市日/评级/规模/转股价"""
    logger.info("Stage 2: 可转债全量列表...")

    # 当前存续
    try:
        cov = ak.bond_zh_cov()
        logger.info(f"  当前列表: {len(cov)}只")
    except Exception as e:
        logger.error(f"  bond_zh_cov失败: {e}")
        return

    # 补充历史已退市 — 尝试 bond_cb_jsl 获取更全的列表
    records = []
    for _, r in cov.iterrows():
        records.append({
            'code': str(r.get('债券代码', '')),
            'name': str(r.get('债券简称', '')),
            'stock_code': str(r.get('正股代码', '')),
            'stock_name': str(r.get('正股简称', '')),
            'list_date': str(r.get('上市时间', '')),
            'issue_size': float(r.get('发行规模', 0) or 0),
            'rating': str(r.get('信用评级', '')),
            'convert_price': float(r.get('转股价', 0) or 0),
        })

    df = pd.DataFrame(records)
    save_meta("cb_universe", df)
    logger.info(f"  ✓ 转债列表: {len(df)}只")

# ══════════════════════════════════════════════════════════════════
# Stage 3: 逐只日线数据
# ══════════════════════════════════════════════════════════════════
def build_daily(batch_size=50, sleep_sec=2):
    """下载每只可转债的日线数据(含价格/溢价率)。"""
    logger.info("Stage 3: 逐只日线数据...")

    universe = load_meta("cb_universe")
    if universe.empty:
        logger.error("  请先运行 Stage 2")
        return

    codes = universe['code'].tolist()
    total = len(codes)
    done = 0
    skip = 0

    for i, code in enumerate(codes):
        fpath = DAILY_DIR / f"{code}.parquet"
        if fpath.exists():
            skip += 1
            continue

        try:
            # 东方财富可转债日线
            df = ak.bond_zh_hs_cov_daily(symbol=f"sh{code}" if not code.startswith('sh') else code)
            if not df.empty:
                df.to_parquet(fpath)
                done += 1
            else:
                # Try sz prefix
                try:
                    df = ak.bond_zh_hs_cov_daily(symbol=f"sz{code}" if not code.startswith('sz') else code)
                    if not df.empty:
                        df.to_parquet(fpath)
                        done += 1
                except Exception:
                    pass
        except Exception as e:
            pass  # bond may be delisted, skip

        if (i + 1) % batch_size == 0:
            logger.info(f"  {i+1}/{total}: 新增{done}, 跳过{skip}")
            time.sleep(sleep_sec)

    logger.info(f"  ✓ 日线: 新增{done}只, 已有{skip}只, 共{total}只")

# ══════════════════════════════════════════════════════════════════
# Stage 4: 强赎/退市记录
# ══════════════════════════════════════════════════════════════════
def build_redemption():
    """获取强赎公告记录 — 强赎是转债退市的主因,也是幸存者偏差的主要来源。"""
    logger.info("Stage 4: 强赎/退市记录...")

    redemptions = []

    # 尝试 bond_cb_redeem_jsl — 强赎记录
    try:
        redeem = ak.bond_cb_redeem_jsl()
        if not redeem.empty:
            logger.info(f"  bond_cb_redeem_jsl: {len(redeem)}条")
            for _, r in redeem.iterrows():
                redemptions.append({
                    'code': str(r.get('债券代码', '')),
                    'name': str(r.get('债券简称', '')),
                    'redeem_date': str(r.get('强赎日期', '')),
                    'redeem_price': float(r.get('强赎价格', 0) or 0),
                    'type': '强赎',
                })
    except Exception as e:
        logger.warning(f"  bond_cb_redeem_jsl失败: {e}")

    # 检查日线数据中最后交易日期,推断退市日
    universe = load_meta("cb_universe")
    if not universe.empty:
        for _, r in universe.iterrows():
            code = r['code']
            fpath = DAILY_DIR / f"{code}.parquet"
            if fpath.exists():
                df = pd.read_parquet(fpath)
                if not df.empty:
                    last_date = str(df['date'].max())[:10]
                    # 如果最后日期 < 2025-12-31, 可能已退市
                    if last_date < '2025-12-31':
                        # Check if already in redemptions
                        if not any(rd['code'] == code for rd in redemptions):
                            redemptions.append({
                                'code': code,
                                'name': r.get('name', ''),
                                'redeem_date': last_date,
                                'redeem_price': 0,
                                'type': '已退市(日线终止)',
                            })

    if redemptions:
        df_r = pd.DataFrame(redemptions)
        save_meta("cb_redemptions", df_r)
        logger.info(f"  ✓ 退市记录: {len(df_r)}条 (强赎{len(df_r[df_r['type']=='强赎'])}只 + 推断{len(df_r[df_r['type']!='强赎'])}只)")
    else:
        logger.info("  ⚠ 无退市记录")

# ══════════════════════════════════════════════════════════════════
# Stage 5: point-in-time 快照
# ══════════════════════════════════════════════════════════════════
def build_snapshots(start="2019-01-01", end="2025-12-31", freq="MS", append=False):
    """
    生成每月初的存续可转债快照(point-in-time):
    - 已上市
    - 未退市(未强赎)
    - 含当日价格/转股溢价率/双低值
    append=True: 增量模式, 与旧快照合并去重而非覆盖(2026-08-31 补洞用)
    """
    logger.info(f"Stage 5: point-in-time快照 {start}→{end}...")

    universe = load_meta("cb_universe")
    redemptions = load_meta("cb_redemptions") if (CB_DIR.parent/"meta"/"cb_redemptions.parquet").exists() else pd.DataFrame()

    # 构建退市日期映射
    delist_map = {}
    if not redemptions.empty:
        for _, r in redemptions.iterrows():
            code = str(r['code'])
            d = str(r['redeem_date'])[:10]
            if d:
                delist_map[code] = d

    cal = load_meta("trade_calendar")
    all_dates = sorted(cal['trade_date'].tolist()) if not cal.empty else []
    snap_dates = pd.date_range(start, end, freq=freq)

    snapshots = []
    for snap_date in snap_dates:
        snap_str = snap_date.strftime("%Y-%m-%d")
        # 找最近的交易日
        valid_dates = [d for d in all_dates if d <= snap_str]
        if not valid_dates: continue
        trade_date = valid_dates[-1]

        bonds_at_date = []
        for _, r in universe.iterrows():
            code = str(r['code'])
            list_date = str(r.get('list_date', ''))[:10]
            if not list_date or list_date > snap_str:
                continue  # 未上市
            delist_d = delist_map.get(code, '2099-12-31')
            if delist_d < snap_str:
                continue  # 已退市

            # 读取当日价格
            fpath = DAILY_DIR / f"{code}.parquet"
            price = None; premium = None
            if fpath.exists():
                df = pd.read_parquet(fpath)
                df['date'] = pd.to_datetime(df['date'])
                row = df[df['date'] == trade_date]
                if not row.empty:
                    price = float(row.iloc[0]['close'])
                    premium = float(row.iloc[0].get('premium', row.iloc[0].get('转股溢价率', None)) or 0)

            if price and price > 0:
                dblow = price + 100 * (premium / 100) if premium else price
                bonds_at_date.append({
                    'code': code,
                    'name': r.get('name', ''),
                    'price': price,
                    'premium': premium,
                    'dblow': round(dblow, 2),
                    'rating': r.get('rating', ''),
                    'size': r.get('issue_size', 0),
                })

        if bonds_at_date:
            snap_df = pd.DataFrame(bonds_at_date)
            snap_df['snap_date'] = snap_str
            snapshots.append(snap_df)

        if len(snapshots) % 12 == 0:
            logger.info(f"  {snap_str}: {len(bonds_at_date)}只存续")

    if snapshots:
        all_snaps = pd.concat(snapshots, ignore_index=True)
        if append:
            old = load_meta("cb_snapshots")
            if not old.empty:
                old["snap_date"] = pd.to_datetime(old["snap_date"])
                all_snaps["snap_date"] = pd.to_datetime(all_snaps["snap_date"])
                all_snaps = (pd.concat([old, all_snaps], ignore_index=True)
                             .drop_duplicates(subset=["code", "snap_date"], keep="last"))
                logger.info(f"  ✓ 快照增量合并: 旧{len(old)}条 + 新{len(snapshots)}个月 → 共{len(all_snaps)}条")
        save_meta("cb_snapshots", all_snaps)
        logger.info(f"  ✓ 快照: {len(snapshots)}个月, 共{len(all_snaps)}条记录")

# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--stage", type=int, default=0, help="1-5, 0=all")
    args = p.parse_args()

    stages = {
        1: build_index,
        2: build_universe,
        3: build_daily,
        4: build_redemption,
        5: build_snapshots,
    }

    if args.stage == 0:
        for s in [1, 2, 3, 4, 5]:
            logger.info(f"\n{'='*60}\n  Stage {s}\n{'='*60}")
            try:
                stages[s]()
            except Exception as e:
                logger.error(f"Stage {s} 失败: {e}")
    else:
        stages[args.stage]()
