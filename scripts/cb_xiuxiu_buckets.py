"""下修史标记分桶执行 (2026-10-01, 任务#20; 判读模板 logs/xiuxiu_template_locked.md
已锁, 判据先于数据)。

口径(模板): 分桶=PIT下修次数{0,1,2+}; 窗口=持有月收益(调仓日选入后到下次调仓);
对比一{1,2+}vs{0}, 对比二{2+}vs{1}; 双侧t/每对比α=0.025/按券聚类; 每桶
券-月≥50且独立券≥10; 三结论全合法。
**盲区声明(10-01 QC)**: 事件表漏计"初始转股价→首个有值事件"间的大幅下修
(API 无事件行)——下修桶可能欠计数, 本结果为初步, 节后盲区修复后重跑定稿。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
from data.storage import load_meta
from data.cb_select import select_double_low
from cb_xiuxiu_marker import load_events, pit_mark

logger.remove()
TOP_N = 25

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

# 月度选择实例(与流动性审计同口径: 快照日调仓, 补足25)
instances = []
snap_idx = 0
last_snap = None
for i, date in enumerate(all_dates):
    ds = str(date.date())[:10]
    pt = price_panel.get(date, {})
    while snap_idx < len(snap_dates) and str(snap_dates[snap_idx].date())[:10] <= ds:
        last_snap = snap_dates[snap_idx]
        snap_idx += 1
    if last_snap is not None and str(last_snap.date())[:10] == ds:
        snap = snapshots[snapshots["snap_date"] == last_snap]
        if snap.empty:
            continue
        avails = [c for c in snap["code"].unique() if c in pt]
        sel = select_double_low(snap[snap["code"].isin(avails)], top=TOP_N)
        if len(sel) >= TOP_N:
            for c in sel:
                instances.append((date, c, last_snap))

events = load_events()

# 持有月收益: (调仓日, 券) → 下次调仓日的券收益
rows = []
rebal_dates = sorted({d for d, _, _ in instances})
for k, (d, code, snap_d) in enumerate(instances):
    nxt = None
    for nd in rebal_dates:
        if nd > d:
            nxt = nd
            break
    if nxt is None:
        continue
    p0 = price_panel.get(d, {}).get(code)
    p1 = price_panel.get(nxt, {}).get(code)
    if not p0 or not p1:
        continue
    ret = p1 / p0 - 1.0
    mark = pit_mark(events, code, snap_d)  # PIT: 快照日为判定时点
    rows.append((code, d, ret, mark))

df = pd.DataFrame(rows, columns=["code", "date", "ret", "mark"])
df["bucket"] = df["mark"].map({0: "0无", 1: "1次", 2: "2+次"})
print(f"持有月观测: {len(df)} 券-月 | 独立券: {df['code'].nunique()}")
print(df["bucket"].value_counts().to_string())
print("\n[模板判据执行] 每桶 N≥50 且独立券≥10; 对比双侧 t, 每对比 α=0.025")


def stats(sub, label):
    n = len(sub)
    nc = sub["code"].nunique()
    mu = sub["ret"].mean()
    # 按券聚类 SE: 券级均值
    per = sub.groupby("code")["ret"].mean()
    se_c = per.std(ddof=1) / np.sqrt(len(per)) if len(per) > 1 else np.nan
    t_c = mu / se_c if se_c > 0 else np.nan
    print(f"  {label:<14} N={n:>5} 券={nc:>4} 月均{mu*100:+.2f}% "
          f"券级SE={se_c*100:.2f}% 聚类t={t_c:+.2f}")
    return mu, se_c


print("\n[桶统计]")
b0 = df[df["mark"] == 0]
b1 = df[df["mark"] == 1]
b2 = df[df["mark"] >= 2]
for sub, lab in [(b0, "0无"), (b1, "1次"), (b2, "2+次")]:
    stats(sub, lab)
print("\n[对比]")
you = df[df["mark"] >= 1]
if len(b0) >= 50 and len(you) >= 50 and b0["code"].nunique() >= 10 and you["code"].nunique() >= 10:
    d1 = you.groupby("code")["ret"].mean() - b0.groupby("code")["ret"].mean()
    mu1 = you["ret"].mean() - b0["ret"].mean()
    se1 = d1.std(ddof=1) / np.sqrt(len(d1))
    t1 = mu1 / se1 if se1 > 0 else np.nan
    print(f"  对比一(有vs无): Δ={mu1*100:+.2f}% SE={se1*100:.2f}% t={t1:+.2f} "
          f"{'显著(α=0.025)' if abs(t1) > 2.24 else '不显著'}")
else:
    print(f"  对比一(有vs无): 样本不足(N无={len(b0)}/N有={len(you)})")
if len(b1) >= 50 and len(b2) >= 50 and b1["code"].nunique() >= 10 and b2["code"].nunique() >= 10:
    d2 = b2.groupby("code")["ret"].mean() - b1.groupby("code")["ret"].mean()
    mu2 = b2["ret"].mean() - b1["ret"].mean()
    se2 = d2.std(ddof=1) / np.sqrt(len(d2))
    t2 = mu2 / se2 if se2 > 0 else np.nan
    print(f"  对比二(多次vs单次): Δ={mu2*100:+.2f}% SE={se2*100:.2f}% t={t2:+.2f} "
          f"{'显著(α=0.025)' if abs(t2) > 2.24 else '不显著'}")
else:
    print(f"  对比二(多次vs单次): 样本不足(N单={len(b1)}/N多={len(b2)})")
print("\n盲区声明: 事件表漏计初始→首事件的大幅下修, 下修桶欠计数, 本结果为初步;")
print("          节后盲区修复后重跑定稿(模板六要素不变)。")
