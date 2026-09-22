"""CB双低 持仓券流动性分布 (2026-09-22 用户追问: 405%/年换手, 3.8万单券买入
是否会推动价格——回测按收盘价成交, 冲击成本未建模)
口径: V3 N=10 甲方案选股, 对每次月度调仓的每只入选券, 取调仓日成交额
(本地日线无amount列, amount=volume×close, 成交量单位为张——新浪实时实测
成交额/成交量=93.3≈现价验证)。输出: 全实例成交额分布 + 3.8万买入占比。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta

REAL_COST = 0.001
TOP_N = 25
N_DAYS = 10

logger.info("加载快照...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

price_panel = {}
for code in snap_codes:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists():
        continue
    df = pd.read_parquet(fpath)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    prev = None
    for _, row in df.iterrows():
        d = row["date"]
        p = float(row["close"]) if "close" in row else None
        if p and p > 0:
            if prev is not None and abs(p / prev - 1) > 0.5:
                continue
            price_panel.setdefault(d, {})[code] = p
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板: {len(price_panel)}交易日")


def select_double_low(snap, top=None):
    df = snap.copy()
    # 2026-09-22: 与实盘 cb_signal 一致, 剔除404退市板块债(违约债, 实盘买不进;
    # 且其 dblow 极低(-30~-42)常年霸占排名第1——回测含它们=与实盘不同构)
    df = df[~df["code"].astype(str).str.startswith("404")]
    rating_order = {"AAA": 0, "AA+": 1, "AA": 2, "AA-": 3, "A+": 4,
                    "A": 5, "A-": 6}
    df["rn"] = df["rating"].map(rating_order).fillna(99)
    df = df[df["rn"] <= 6]
    if "size" in df.columns:
        df = df[df["size"] >= 0.5]
    if "price" in df.columns and "premium" in df.columns:
        df = df[~((df["price"] > 130) & (df["premium"] < 5))]
    if "dblow" not in df.columns:
        df["dblow"] = df["price"] + 100 * df["premium"] / 100
    ranked = df.sort_values("dblow")["code"].tolist()
    return ranked[:top] if top else ranked


# ── 逐月选股(甲: 补足25), 收集 (调仓日, code) ──
instances = []  # (date_str, code)
snap_idx = 0
last_snap_date = None
for i, date in enumerate(all_dates):
    date_str = str(date.date())[:10]
    prices_today = price_panel.get(date, {})
    while (snap_idx < len(snap_dates)
           and str(snap_dates[snap_idx].date())[:10] <= date_str):
        last_snap_date = snap_dates[snap_idx]
        snap_idx += 1
    if last_snap_date and str(last_snap_date.date())[:10] == date_str:
        snap = snapshots[snapshots["snap_date"] == last_snap_date]
        if not snap.empty:
            avails = [c for c in snap["code"].unique() if c in prices_today]
            selected = select_double_low(
                snap[snap["code"].isin(avails)], top=TOP_N)
            if len(selected) >= TOP_N:
                for c in selected:
                    instances.append((date_str, c))

logger.info(f"  调仓实例: {len(instances)} (券,月)")

# ── 读取入选券的 (date, volume), 计算 amount=volume×close ──
union = sorted({c for _, c in instances})
vol_map = {}
no_vol_cols = set()
for code in union:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists():
        continue
    df = pd.read_parquet(fpath)
    if "volume" not in df.columns:
        no_vol_cols.add(code)
        continue
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    vol_map[code] = dict(zip(df["date"], df["volume"]))
if no_vol_cols:
    logger.warning(f"  {len(no_vol_cols)}只券无volume列: {sorted(no_vol_cols)[:10]}")

amounts = []
missing = 0
for date_str, code in instances:
    v = vol_map.get(code, {}).get(date_str)
    cp = price_panel.get(pd.Timestamp(date_str), {}).get(code)
    if v is None or not cp:
        missing += 1
        continue
    amounts.append(float(v) * float(cp))

amounts = np.array(amounts)
print("=" * 80)
print(f"CB双低入选券调仓日成交额分布 (N={len(amounts)}实例, 缺{missing})")
print("=" * 80)
for p in [1, 5, 10, 25, 50, 75, 90]:
    print(f"  P{p:>2}: {np.percentile(amounts, p)/1e4:>12.0f} 万元")
print(f"  <50万:   {(amounts < 5e5).mean()*100:5.1f}%")
print(f"  <100万:  {(amounts < 1e6).mean()*100:5.1f}%")
print(f"  <200万:  {(amounts < 2e6).mean()*100:5.1f}%")
print(f"  <500万:  {(amounts < 5e6).mean()*100:5.1f}%")

# 3.8万买入占成交额比例(当前实盘口径: 96万腿/25只≈3.8万; 0.5%冲击阈值粗标)
BUY = 38_000
ratios = BUY / amounts
print(f"\n单券{BUY/1e4:.1f}万买入占当日成交额比例:")
for p in [50, 75, 90, 95, 99]:
    print(f"  P{p:>2}: {np.percentile(ratios, p)*100:6.2f}%")
print(f"  占比>0.5%的实例: {(ratios > 0.005).mean()*100:5.1f}%")
print(f"  占比>1%的实例:   {(ratios > 0.01).mean()*100:5.1f}%")
worst = np.argsort(ratios)[-10:][::-1]
print("\n  占比最高的10个实例(调仓日, 券, 成交额, 占比):")
for idx in worst:
    print(f"    {instances[idx][0]} {instances[idx][1]} "
          f"{amounts[idx]/1e4:>9.0f}万 {ratios[idx]*100:5.2f}%")
