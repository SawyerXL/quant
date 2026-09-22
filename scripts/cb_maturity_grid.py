"""CB双低 到期剩余期限过滤网格 (2026-09-22 立项批准, 判据用户锁死)
判据(用户): 剔除正当性=结构性(期权时间价值归零→牛市掉队+熊市债底伪装alpha),
不由夏普决定; N 只在 {30,60,90} 选, 禁 N=0; 夏普不劣于不剔 −0.05 即可接受;
三N取中位表现者(反拟合)。

口径声明(2026-09-22 数据受限): 东财详情接口(EXPIRE_DATE)从本机被限流(1行残
响应), 到期日用"本地日线最后 bar 日期"近似(=最后交易日, 通常早于到期日 3~10
天)——方向更保守(更早排除); 存续券(到期日在样本期之后)永不触发, 无影响。
盲区: 强赎/违约摘牌的最后 bar 也计入"到期代理"(双重过滤无害); 精确到期日待
东财接口恢复后用 cb_maturity_jsl 复核。
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


def run_bt(maturity_days=None, stats=None):
    """V3 N=10 甲方案 + 可选到期过滤: 距最后交易日 < maturity_days 的券
    在选股前剔除(从余下池补足)。maturity_days=None = 不剔(基线)。"""
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
            if maturity_days is not None:
                # 到期代理过滤: 最后有价日 - 快照日 < N 天 → 剔除
                avails = [
                    c for c in avails
                    if not (c in death_date
                            and 0 <= (death_date[c] - last_snap_date).days
                            < maturity_days)
                ]
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
        stats["pops"] = pops
        stats["temp_pops"] = temp_pops
        stats["skip_rebal"] = skip_rebal
        stats["blocked_top"] = blocked_top
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
print("CB双低 到期剩余期限过滤网格 (判据已锁: 见 docstring)")
print("=" * 92)

st0 = {}
nav0 = run_bt(None, stats=st0)
ar0, sr0, dd0, vol0 = metrics(nav0)
print(f"\n基线(不剔):   夏普 {sr0:.3f} | 年化 {ar0*100:+6.1f}% | 回撤 {dd0*100:+6.1f}%"
      f" | pop {st0['pops']}(临时{st0['temp_pops']})")

results = {}
for M in [30, 60, 90]:
    st = {}
    nav = run_bt(M, stats=st)
    ar, sr, dd, vol = metrics(nav)
    results[M] = (nav, sr, ar, dd, st)
    print(f"N={M:>3}天:       夏普 {sr:.3f} | 年化 {ar*100:+6.1f}% | 回撤 {dd*100:+6.1f}%"
          f" | pop {st['pops']}(临时{st['temp_pops']}) | 被挡 {st['blocked_top']}只次")

# ── 判据(锁死) ──
print("\n[判据执行] (夏普不劣于不剔−0.05 → 可接受; 三N取中位表现者)")
sr_list = [results[M][1] for M in [30, 60, 90]]
med = float(pd.Series(sr_list).median())
M_med = [M for M in [30, 60, 90] if results[M][1] == med][0]
bar = sr0 - 0.05
passing = {M: (results[M][1] >= bar) for M in [30, 60, 90]}
print(f"  门槛: 夏普 ≥ {sr0:.3f}−0.05 = {bar:.3f}")
for M in [30, 60, 90]:
    print(f"  N={M}: 夏普 {results[M][1]:.3f} → {'PASS' if passing[M] else 'FAIL'}")
print(f"  中位表现者: N={M_med} (夏普 {med:.3f})")
if passing[M_med]:
    verdict = f"选定 N={M_med} (中位表现者且过线)"
else:
    verdict = (f"中位者 N={M_med} 不过线({results[M_med][1]:.3f} < {bar:.3f})"
               f"——结构性理由仍主张剔除, 需人工裁定具体 N")
print(f"\n★ 裁定: {verdict}")

out = pd.DataFrame({"nav_base": nav0})
for M in [30, 60, 90]:
    out[f"nav_M{M}"] = results[M][0]
out.to_csv("logs/cb_maturity_grid_nav.csv")
print("已保存 logs/cb_maturity_grid_nav.csv")
