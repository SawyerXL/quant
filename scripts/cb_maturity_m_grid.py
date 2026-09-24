"""CB双低 到期禁买阈值 M 网格 (2026-09-24, 判据已预登记: 收工两记 09-24)
判据(用户锁死, 出数前): 参照 N 的做法——禁 M=∞(不禁买); M 只在 {90,120,150}
选; 三值取中位表现者(反拟合, 不取最优); 接受线=夏普不劣于 N=60 无带参照 −0.05。
语义(与 cb_signal.maturity_band 一致): 距最后交易日 < N=60 一律剔除;
N~M 缓冲带 = 已持有保留(目标=当前持仓)、未持有禁买; ≥M 正常。
口径声明同 cb_maturity_grid: 到期日=死亡日代理+jsl 当前公告覆盖, PANEL_END
伪死亡修正; 暂定参数, 一年后 jsl PIT 快照以真实到期日重验。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta
from data.cb_select import select_double_low

REAL_COST = float(os.environ.get("CB_COST", "0.001"))
TOP_N = 25
N_DAYS = 10          # 停牌过滤 N(与 V3 定案一致)
N_MATURITY = 60      # 到期剔除线(09-24 定案暂定)

logger.info("加载快照...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

price_panel = {}
death_date = {}      # code -> 最后有价日(面板口径的"到期代理")
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
            death_date[code] = d
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板: {len(price_panel)}交易日")
PANEL_END = all_dates[-1]

# 叠加 jsl 当前公告的最后交易日(与 N 网格同源同口径)
try:
    jsl = pd.read_parquet("data_store/meta/cb_expiry_watch.parquet")
    jsl = jsl[jsl["最后交易日"].notna()]
    for _, r in jsl.iterrows():
        c = str(r["代码"])
        d = pd.Timestamp(r["最后交易日"])
        if c in death_date:
            death_date[c] = d
    logger.info(f"  叠加公告最后交易日 {len(jsl)} 条")
except Exception as e:
    logger.warning(f"jsl 缓存不可用, 纯死亡日代理: {e}")


def _days_left(code: str, snap_date: pd.Timestamp):
    """距最后交易日天数(代理口径); 存续券死亡日=面板末端 → 大数。"""
    dd = death_date.get(code)
    if dd is None:
        return 9999
    return (dd - snap_date).days


def run_bt(M=None, stats=None):
    """V3 N=10 + 到期 N=60 剔除 + 可选 M 缓冲带。M=None=无带参照(N=60 单层)。
    缓冲带语义: 已持有且 N≤days<M → 强制保留(权重1/25); 未持有同带 → 禁买。"""
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    missing = {}
    snap_idx = 0
    last_snap_date = None
    pops = temp_pops = skip_rebal = blocked_top = 0

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

        gone = []
        for c in list(cur_weights):
            if c in prices_today:
                missing[c] = 0
            else:
                missing[c] = missing.get(c, 0) + 1
                if missing[c] >= N_DAYS:
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
            # 到期 N=60: 距死亡 < N 且真实死亡(<面板末端) → 剔除(伪死亡修正)
            avails = [
                c for c in avails
                if not (c in death_date and death_date[c] < PANEL_END
                        and 0 <= _days_left(c, last_snap_date) < N_MATURITY)
            ]
            if M is not None:
                # M 缓冲带: 未持有且 N≤days<M → 禁买; 已持有 → 强制保留
                forced = [c for c in cur_weights
                          if c in avails
                          and N_MATURITY <= _days_left(c, last_snap_date) < M]
                buyable = [c for c in avails
                           if _days_left(c, last_snap_date) >= M]
                forced = sorted(set(forced))
                full_rank = select_double_low(
                    snap[snap["code"].isin(buyable)], top=TOP_N)
                if len(forced) >= TOP_N:
                    selected = forced[:TOP_N]
                else:
                    selected = forced + full_rank[:TOP_N - len(forced)]
            else:
                full_rank = select_double_low(snap, top=TOP_N)
                blocked = [c for c in full_rank if c not in prices_today]
                blocked_top += len(blocked)
                selected = select_double_low(
                    snap[snap["code"].isin(avails)], top=TOP_N)
            if len(selected) < TOP_N:
                skip_rebal += 1
                continue
            selected = selected[:TOP_N]
            n = len(selected)
            new_w = {c: 1.0 / n for c in selected}

            old_set = set(cur_weights.keys())
            new_set = set(new_w.keys())
            enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w) / 2 * REAL_COST * 2)
            cur_weights = new_w
            for c in cur_weights:
                missing.setdefault(c, 0)

    if stats is not None:
        stats.update(pops=pops, temp_pops=temp_pops, skip_rebal=skip_rebal,
                     blocked_top=blocked_top)
    return nav


def metrics(nav_s):
    active = nav_s.loc[snap_dates[0]:]
    n = len(active)
    ar = (active.iloc[-1] / active.iloc[0]) ** (252 / n) - 1
    dr = active.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    dd = (active / active.cummax() - 1).min()
    return ar, sr, dd


print("=" * 92)
print("CB双低 到期禁买阈值 M 网格 (判据已锁: 三值{90,120,150}取中位, 禁M=∞)")
print("=" * 92)

st0 = {}
nav0 = run_bt(None, stats=st0)
ar0, sr0, dd0 = metrics(nav0)
print(f"\n参照(N=60 无带): 夏普 {sr0:.3f} | 年化 {ar0*100:+6.1f}% | 回撤 {dd0*100:+6.1f}%"
      f" | pop {st0['pops']}(临时{st0['temp_pops']})")

results = {}
for M in [90, 120, 150]:
    st = {}
    nav = run_bt(M, stats=st)
    ar, sr, dd = metrics(nav)
    results[M] = (nav, sr, ar, dd, st)
    print(f"M={M:>3}天(带):   夏普 {sr:.3f} | 年化 {ar*100:+6.1f}% | 回撤 {dd*100:+6.1f}%"
          f" | pop {st['pops']}(临时{st['temp_pops']}) | 被挡 {st['blocked_top']}只次")

print("\n[判据执行] (夏普不劣于参照−0.05 → 可接受; 三M取中位表现者)")
sr_list = [results[M][1] for M in [90, 120, 150]]
med = float(pd.Series(sr_list).median())
M_med = [M for M in [90, 120, 150] if abs(results[M][1] - med) < 1e-9][0]
bar = sr0 - 0.05
passing = {M: (results[M][1] >= bar) for M in [90, 120, 150]}
print(f"  门槛: 夏普 ≥ {sr0:.3f}−0.05 = {bar:.3f}")
for M in [90, 120, 150]:
    print(f"  M={M}: 夏普 {results[M][1]:.3f} → {'PASS' if passing[M] else 'FAIL'}")
print(f"  中位表现者: M={M_med} (夏普 {med:.3f})")
if passing[M_med]:
    verdict = f"选定 M={M_med} (中位表现者且过线)"
else:
    verdict = (f"中位者 M={M_med} 不过线({results[M_med][1]:.3f} < {bar:.3f})"
               f"——结构性理由仍主张禁买带, 需人工裁定具体 M")
print(f"\n★ 裁定: {verdict}")
print("  口径标注: 死亡日代理+jsl 覆盖, 暂定参数; 一年后以 jsl PIT 快照真实到期日重验。")

out = pd.DataFrame({"nav_ref": nav0})
for M in [90, 120, 150]:
    out[f"nav_M{M}"] = results[M][0]
out.to_csv("logs/cb_maturity_m_grid_nav.csv")
print("已保存 logs/cb_maturity_m_grid_nav.csv")
