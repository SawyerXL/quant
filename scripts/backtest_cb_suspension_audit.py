"""CB双低回测停牌污染审计 (2026-09-22, 用户优先级#1)
问题: 回测宇宙中停牌券是否污染夏普1.34?
A. 僵尸普查: 文件死尾但仍出现在后续快照的(code, 快照月)
B. V0 baseline 重跑(复现现有口径) + 事件计数(pop/临时停牌/换仓跳过/被avails挡掉的TOP25候选)
C. V1: pop后剩余权重再归一(停牌即重配, 无闲置现金)
   V2: 僵尸券从宇宙剔除(文件已死尾的code不得进入任何快照选股)
口径与 backtest_cb_doublelow.py 逐行一致, 仅加计数与变体。
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
CASH_YIELD = 0.02

logger.info("加载快照...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())
logger.info(f"  快照: {len(snap_dates)}个月, {len(snapshots)}条记录")

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

# ── 面板构建(与原脚本一致) + 每券最后有价日 ──
price_panel = {}
last_price_date = {}
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
            last_price_date[code] = d
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板: {len(price_panel)}交易日")

# ── A. 僵尸普查: 文件死尾 < 该券最后一次出现在快照 ──
last_snap_month = snapshots.groupby("code")["snap_date"].max()
zombie_codes = []
for code, death in last_price_date.items():
    if code in last_snap_month.index and death < last_snap_month[code]:
        zombie_codes.append(code)
logger.info(f"A. 僵尸券(文件死尾但后续快照仍含): {len(zombie_codes)} 只")
zombie_set = set(zombie_codes)
# 僵尸(copy, 快照月)计数
zom_records = 0
for code in zombie_codes:
    death = last_price_date[code]
    later = snapshots[(snapshots["code"] == code) &
                      (snapshots["snap_date"] > death)]
    zom_records += len(later)
logger.info(f"   僵尸(券,月)记录: {zom_records}")


def select_double_low(snap):
    df = snap.copy()
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
    return df.sort_values("dblow").head(TOP_N)["code"].tolist()


def run_bt(strategy="dblow", variant="V0", stats=None):
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    snap_idx = 0
    last_snap_date = None
    pops, temp_pops, skip_rebal, blocked_top = [], 0, 0, 0

    for i, date in enumerate(all_dates):
        date_str = str(date.date())[:10]
        prices_today = price_panel.get(date, {})

        if cur_weights and i > 0:
            ret = 0.0
            prev_prices = price_panel.get(all_dates[i - 1], {})
            for code, w in cur_weights.items():
                pp = prev_prices.get(code)
                cp = prices_today.get(code)
                if pp and cp and pp > 0:
                    ret += w * (cp / pp - 1)
            nav.iloc[i] = nav.iloc[i - 1] * (1 + ret)
        else:
            nav.iloc[i] = nav.iloc[i - 1] if i > 0 else 1.0

        # 无价持仓 pop(停牌首日/退市)
        gone = [c for c in cur_weights if c not in prices_today]
        for c in gone:
            w = cur_weights.pop(c)
            nav.iloc[i] -= w * REAL_COST
            pops.append((c, date_str))
            # 该券之后是否重新出现在面板(临时停牌 vs 真退市)
            dpos = all_dates.index(date)
            if any(c in price_panel.get(all_dates[j], {})
                   for j in range(dpos + 1, len(all_dates))):
                temp_pops += 1
        if gone and variant == "V1":
            # 停牌即重配: 剩余权重按比例归一
            s = sum(cur_weights.values())
            if s > 0:
                cur_weights = {c: w / s for c, w in cur_weights.items()}

        while (snap_idx < len(snap_dates)
               and str(snap_dates[snap_idx].date())[:10] <= date_str):
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        if last_snap_date and str(last_snap_date.date())[:10] == date_str:
            snap = snapshots[snapshots["snap_date"] == last_snap_date]
            if snap.empty:
                continue
            if variant == "V2":
                snap = snap[~snap["code"].isin(zombie_set)]
            avails = [c for c in snap["code"].unique() if c in prices_today]

            if strategy == "dblow":
                # 计数: 若僵尸券没有被avails挡住, 它们会占据TOP25吗
                full_rank = select_double_low(snap)
                blocked = [c for c in full_rank if c not in prices_today]
                blocked_top += len(blocked)
                selected = select_double_low(
                    snap[snap["code"].isin(avails)])
                if len(selected) < TOP_N:
                    skip_rebal += 1
                    continue
                selected = selected[:TOP_N]
            else:
                df = snap[snap["code"].isin(avails)].copy()
                rating_order = {"AAA": 0, "AA+": 1, "AA": 2, "AA-": 3,
                                "A+": 4, "A": 5, "A-": 6}
                df["rn"] = df["rating"].map(rating_order).fillna(99)
                df = df[df["rn"] <= 6]
                if "size" in df.columns:
                    df = df[df["size"] >= 0.5]
                if "price" in df.columns and "premium" in df.columns:
                    df = df[~((df["price"] > 130) & (df["premium"] < 5))]
                selected = df["code"].unique().tolist()
                if len(selected) < 10:
                    skip_rebal += 1
                    continue

            n = len(selected)
            old_set = set(cur_weights.keys())
            new_set = set(selected)
            new_w = {c: 1.0 / n for c in selected}
            enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w) / 2 * REAL_COST * 2)
            cur_weights = new_w

    if stats is not None:
        stats["pops"] = len(pops)
        stats["temp_pops"] = temp_pops
        stats["skip_rebal"] = skip_rebal
        stats["blocked_top"] = blocked_top
    return nav


def metrics(nav_s):
    # 活跃窗口口径(与 backtest_cb_doublelow.py 2026-09-22 修复一致):
    # 自首快照月起算, 排除无持仓平段——全序列口径(含平段)会系统性低估夏普
    active = nav_s.loc[snap_dates[0]:]
    n = len(active)
    ar = (active.iloc[-1] / active.iloc[0]) ** (252 / n) - 1
    dr = active.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    dd = (active / active.cummax() - 1).min()
    vol = dr.std() * np.sqrt(252)
    return ar, sr, dd, vol


print("=" * 90)
print("CB双低停牌污染审计 (V0=原口径 / V1=pop即重配 / V2=僵尸券剔宇宙)")
print("=" * 90)
navs = {}
for v in ["V0", "V1", "V2"]:
    st = {}
    navs[v] = run_bt("dblow", variant=v, stats=st)
    ar, sr, dd, vol = metrics(navs[v])
    print(f"\n{v}: 年化 {ar*100:+7.1f}% | 夏普 {sr:5.2f} | 回撤 {dd*100:+6.1f}%"
          f" | 波动 {vol*100:+5.1f}%")
    print(f"    事件: pop {st['pops']} 次(其中临时停牌 {st['temp_pops']})"
          f" | 换仓跳过 {st['skip_rebal']} 月 | TOP25候选被avails挡住 "
          f"{st['blocked_top']} 只次")

# 等权基准同样三变体
print("\n" + "-" * 90)
print("全市场等权基准对照:")
bnavs = {}
for v in ["V0", "V1", "V2"]:
    bnavs[v] = run_bt("equal", variant=v)
    ar, sr, dd, vol = metrics(bnavs[v])
    print(f"\n{v}: 年化 {ar*100:+7.1f}% | 夏普 {sr:5.2f} | 回撤 {dd*100:+6.1f}%"
          f" | 波动 {vol*100:+5.1f}%")

# 双低-等权超额(V0 vs V2)
for v in ["V0", "V2"]:
    d = navs[v]
    e = bnavs[v]
    cm = d.index.intersection(e.index)
    dm = d[cm].resample("ME").last().pct_change(fill_method=None).dropna()
    em = e[cm].resample("ME").last().pct_change(fill_method=None).dropna()
    ex = (dm - em).dropna()
    am = np.mean(ex)
    aa = (1 + am) ** 12 - 1
    res = ex - am
    nwl = min(3, len(ex) // 4)
    S = np.sum(res ** 2) / len(ex)
    for lag in range(1, nwl + 1):
        w = 1 - lag / (nwl + 1)
        S += 2 * w * np.sum(res[lag:] * res[:-lag]) / len(ex)
    se = np.sqrt(S / len(ex))
    t = am / se if se > 0 else 0
    print(f"\n[{v}] 双低超额(月频): {aa*100:+.2f}%/年, HAC t={t:+.2f}")

# 保存
out = pd.DataFrame({f"nav_{v}": navs[v] for v in ["V0", "V1", "V2"]})
out.to_csv("logs/cb_suspension_audit_nav.csv")
print("\n已保存 logs/cb_suspension_audit_nav.csv")
