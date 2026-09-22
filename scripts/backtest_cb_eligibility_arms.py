"""CB双低 甲乙丙方案回测 (2026-09-22 用户判据已锁, 出数前锁)
V3 = 时点新鲜度口径(N日规则: 持有券连续无价≥N个交易日才pop; 停牌前正常参与/
     停牌后排除/复牌后可再入选; 选股=可交易池补足25只) — 这是甲乙的基准。
甲 = V3 同机制(从可交易池补足25只)。换手增量vs V3 = 0(同一臂), 判据为守卫。
乙 = 全快照排名取TOP25, 仅保留可交易者, 权重按实际数量重分配(1/n)。
判据(用户2026-09-22锁定):
  甲: 年化换手增量 ≤ 30pp 且 夏普 ≥ V3−0.05
  乙: 夏普 ≥ V3 且 单券权重 ≤ 8% 且 回撤不劣于 V3+2pp
  甲乙都过 → 夏普高者; Δ夏普<0.05 → 甲(集中度低)
  都不过 → 丙, 闲置资金必须入货基/短债
N 取值: 只试 3/5/10, 取夏普最接近中位者, 不取最优者。
回测eligibility用"调仓日有价"(avails) — 与实时信号N日规则在调仓日等价;
执行侧另加实时停牌过滤(双保险)。
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


def run_bt(N, fillup=True, stats=None):
    """N: 无价连续交易日≥N才pop(N=1即V0日1pop); fillup=False=乙(不补足,重分配)"""
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    missing = {}
    snap_idx = 0
    last_snap_date = None
    pops = temp_pops = skip_rebal = blocked_top = 0
    turnover = 0.0
    n_rebal = 0
    max_w = 0.0

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

        # N日无价才pop
        gone = []
        for c in list(cur_weights):
            if c in prices_today:
                missing[c] = 0
            else:
                missing[c] = missing.get(c, 0) + 1
                if missing[c] >= N:
                    gone.append(c)
        for c in gone:
            w = cur_weights.pop(c)
            nav.iloc[i] -= w * REAL_COST
            missing.pop(c, None)
            pops += 1
            dpos = all_dates.index(date)
            if any(c in price_panel.get(all_dates[j], {})
                   for j in range(dpos + 1, len(all_dates))):
                temp_pops += 1

        while (snap_idx < len(snap_dates)
               and str(snap_dates[snap_idx].date())[:10] <= date_str):
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        if last_snap_date and str(last_snap_date.date())[:10] == date_str:
            snap = snapshots[snapshots["snap_date"] == last_snap_date]
            if snap.empty:
                continue
            avails = [c for c in snap["code"].unique() if c in prices_today]

            if fillup:
                full_rank = select_double_low(snap)
                blocked = [c for c in full_rank if c not in prices_today]
                blocked_top += len(blocked)
                selected = select_double_low(
                    snap[snap["code"].isin(avails)])
                if len(selected) < TOP_N:
                    skip_rebal += 1
                    continue
                selected = selected[:TOP_N]
                n = len(selected)
                new_w = {c: 1.0 / n for c in selected}
            else:
                # 乙: 全快照排名TOP25, 只保留可交易, 权重1/n重分配
                full_rank = select_double_low(snap)
                kept = [c for c in full_rank if c in avails]
                blocked_top += TOP_N - len(kept)
                if len(kept) < 10:
                    skip_rebal += 1
                    continue
                n = len(kept)
                new_w = {c: 1.0 / n for c in kept}
            max_w = max(max_w, max(new_w.values()))

            old_set = set(cur_weights.keys())
            new_set = set(new_w.keys())
            enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w) / 2 * REAL_COST * 2)
            turnover += enter_w
            n_rebal += 1
            cur_weights = new_w
            for c in cur_weights:
                missing.setdefault(c, 0)

    if stats is not None:
        stats["pops"] = pops
        stats["temp_pops"] = temp_pops
        stats["skip_rebal"] = skip_rebal
        stats["blocked_top"] = blocked_top
        stats["turnover"] = turnover
        stats["n_rebal"] = n_rebal
        stats["max_w"] = max_w
    return nav


def metrics(nav_s):
    active = nav_s.loc[snap_dates[0]:]
    n = len(active)
    ar = (active.iloc[-1] / active.iloc[0]) ** (252 / n) - 1
    dr = active.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    dd = (active / active.cummax() - 1).min()
    vol = dr.std() * np.sqrt(252)
    return ar, sr, dd, vol


print("=" * 92)
print("CB双低 甲乙丙回测 (判据已锁: 见脚本 docstring)")
print("=" * 92)

# ── V3: N ∈ {3,5,10} ──
arms = {}
for N in [3, 5, 10]:
    st = {}
    arms[f"V3_N{N}"] = (run_bt(N, fillup=True, stats=st), st)
    ar, sr, dd, vol = metrics(arms[f"V3_N{N}"][0])
    st["metrics"] = (ar, sr, dd, vol)

print("\n[V3 网格] N ∈ {3,5,10}:")
for N in [3, 5, 10]:
    _, st = arms[f"V3_N{N}"]
    ar, sr, dd, vol = st["metrics"]
    t1 = st["turnover"] / max(st["n_rebal"], 1) * 12
    print(f"  N={N}: 夏普 {sr:.3f} | 年化 {ar*100:+6.1f}% | 回撤 {dd*100:+6.1f}%"
          f" | 换手 {t1*100:5.0f}%/年 | pop {st['pops']}(临时{st['temp_pops']})"
          f" | 被挡 {st['blocked_top']} 只次")

# 取夏普最接近中位者
sharps = {N: arms[f"V3_N{N}"][1]["metrics"][1] for N in [3, 5, 10]}
med = float(pd.Series(list(sharps.values())).median())
N_pick = min(sharps, key=lambda n: abs(sharps[n] - med))
print(f"\n中位夏普 {med:.3f} → 选定 N={N_pick} (夏普 {sharps[N_pick]:.3f})")

# ── 甲 = V3 同臂; 乙 = 不补足重分配 ──
st_jia = arms[f"V3_N{N_pick}"][1]
st_yi = {}
nav_yi = run_bt(N_pick, fillup=False, stats=st_yi)
st_yi["metrics"] = metrics(nav_yi)

ar_v, sr_v, dd_v, vol_v = st_jia["metrics"]
ar_y, sr_y, dd_y, vol_y = st_yi["metrics"]
t_jia = st_jia["turnover"] / max(st_jia["n_rebal"], 1) * 12
t_yi = st_yi["turnover"] / max(st_yi["n_rebal"], 1) * 12

print("\n" + "-" * 92)
print(f"甲(=V3 N={N_pick}, 补足25只): 夏普 {sr_v:.3f} | 年化 {ar_v*100:+6.1f}%"
      f" | 回撤 {dd_v*100:+6.1f}% | 换手 {t_jia*100:.0f}%/年"
      f" | 最大单券权重 {st_jia['max_w']*100:.1f}%")
print(f"乙(不补足, 重分配1/n):      夏普 {sr_y:.3f} | 年化 {ar_y*100:+6.1f}%"
      f" | 回撤 {dd_y*100:+6.1f}% | 换手 {t_yi*100:.0f}%/年"
      f" | 最大单券权重 {st_yi['max_w']*100:.1f}%")
print(f"   乙事件: pop {st_yi['pops']}(临时{st_yi['temp_pops']})"
      f" | 跳过 {st_yi['skip_rebal']} 月 | 被挡 {st_yi['blocked_top']} 只次")

# ── 判据(已锁) ──
print("\n[判据执行]")
jia_pass = (0 <= 30 / 100) and (sr_v >= sr_v - 0.05)  # 甲=V3: 换手增量0, 夏普差0
yi_c1 = sr_y >= sr_v
yi_c2 = st_yi["max_w"] <= 0.08
yi_c3 = dd_y >= dd_v - 0.02
yi_pass = yi_c1 and yi_c2 and yi_c3
print(f"  甲: 换手增量 0pp ≤ 30pp ✓ | 夏普差 0 ≥ −0.05 ✓ → {'PASS' if jia_pass else 'FAIL'}")
print(f"  乙: 夏普 {sr_y:.3f} ≥ {sr_v:.3f} → {'✓' if yi_c1 else '✗'}"
      f" | 单券上限 {st_yi['max_w']*100:.1f}% ≤ 8% → {'✓' if yi_c2 else '✗'}"
      f" | 回撤 {dd_y*100:.1f}% ≥ {dd_v*100:.1f}%−2pp → {'✓' if yi_c3 else '✗'}"
      f" → {'PASS' if yi_pass else 'FAIL'}")
if jia_pass and yi_pass:
    if sr_y - sr_v >= 0.05:
        verdict = "乙(夏普显著更高)"
    else:
        verdict = "甲(Δ夏普<0.05, 取集中度低者)"
elif jia_pass:
    verdict = "甲(乙不过线)"
else:
    verdict = "丙 + 闲置资金入货基/短债(甲乙都不过线)"
print(f"\n★ 裁定: {verdict}")

out = pd.DataFrame({f"nav_{k}": arms[k][0] for k in arms})
out["nav_yi"] = nav_yi
out.to_csv("logs/cb_eligibility_arms_nav.csv")
print("已保存 logs/cb_eligibility_arms_nav.csv")
