"""诊断 09-21 快照重建被单调性闸门拦截的 284 行缺口 (2026-09-23)
复刻 build_snapshots 的构建循环(new-only, 不过闸门, 落影子表), 与主表
逐月 diff, 定位缺口来自哪几个月/哪些券。只读+写影子, 不碰主表。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger
from data.storage import load_meta, save_meta

CB_DIR = Path("data_store/convertible_bonds")
DAILY_DIR = CB_DIR / "daily"
SHADOW = "cb_snapshots_diag_20260923"


def main():
    universe = load_meta("cb_universe")
    redemptions = load_meta("cb_redemptions") if (CB_DIR.parent / "meta" / "cb_redemptions.parquet").exists() else pd.DataFrame()
    logger.info(f"universe {len(universe)}只 | redemptions {len(redemptions)}条")

    delist_map = {}
    if not redemptions.empty:
        for _, r in redemptions.iterrows():
            code = str(r['code'])
            d = str(r['redeem_date'])[:10]
            if d:
                delist_map[code] = d
    logger.info(f"delist_map {len(delist_map)} 条")

    cal = load_meta("trade_calendar")
    all_dates = sorted(cal['trade_date'].tolist()) if not cal.empty else []
    snap_dates = pd.date_range("2019-01-01", "2026-09-23", freq="MS")

    snapshots = []
    for snap_date in snap_dates:
        snap_str = snap_date.strftime("%Y-%m-%d")
        valid_dates = [d for d in all_dates if d <= snap_str]
        if not valid_dates:
            continue
        trade_date = valid_dates[-1]
        bonds = []
        for _, r in universe.iterrows():
            code = str(r['code'])
            list_date = str(r.get('list_date', ''))[:10]
            if not list_date or list_date > snap_str:
                continue
            if delist_map.get(code, '2099-12-31') < snap_str:
                continue
            fpath = DAILY_DIR / f"{code}.parquet"
            if not fpath.exists():
                continue
            df = pd.read_parquet(fpath)
            df['date'] = pd.to_datetime(df['date'])
            row = df[df['date'] == trade_date]
            if row.empty:
                continue
            price = float(row.iloc[0]['close'])
            if price and price > 0:
                bonds.append((code, price))
        if bonds:
            snapshots.append(pd.DataFrame({
                'code': [b[0] for b in bonds],
                'snap_date': snap_str,
            }))
        if len(snapshots) % 12 == 0:
            logger.info(f"  {snap_str}: {len(bonds)}只")

    new_df = pd.concat(snapshots, ignore_index=True)
    new_df["snap_date"] = pd.to_datetime(new_df["snap_date"])
    logger.info(f"新构建: {len(new_df)} 行, {new_df['snap_date'].nunique()} 个月")
    save_meta(SHADOW, new_df)
    logger.info(f"已落影子表 {SHADOW}")

    old = load_meta("cb_snapshots")
    old["snap_date"] = pd.to_datetime(old["snap_date"])
    logger.info(f"主表: {len(old)} 行, {old['snap_date'].nunique()} 个月")

    om = old["snap_date"].astype(str).str[:7].value_counts().sort_index()
    nm = new_df["snap_date"].astype(str).str[:7].value_counts().sort_index()
    print("\n逐月 diff (新−旧):")
    for m in om.index.union(nm.index):
        d = nm.get(m, 0) - om.get(m, 0)
        if d != 0:
            print(f"  {m}: 旧{om.get(m,0):>4} 新{nm.get(m,0):>4}  差{d:+d}")

    # 缺口最大的月份的券级对比
    worst = None
    worst_d = 0
    for m in om.index:
        d = nm.get(m, 0) - om.get(m, 0)
        if d < worst_d:
            worst_d = d
            worst = m
    if worst:
        print(f"\n缺口最大月份 {worst} (差{worst_d}):")
        o_set = set(old[old["snap_date"].astype(str).str[:7] == worst]["code"].astype(str))
        n_set = set(new_df[new_df["snap_date"].astype(str).str[:7] == worst]["code"].astype(str))
        print("  旧有新无:", sorted(o_set - n_set)[:20])
        print("  新有旧无:", sorted(n_set - o_set)[:20])


if __name__ == "__main__":
    main()
