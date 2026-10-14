"""CB双低 调仓日程×退市回收 对照回测 (2026-09-24, 可复算核对用)。

回答两个问题(用户 2026-09-24 核查后立项):
  Q1 信号与回测不同构: 回测月一次(快照日) vs 实盘月两次(月中+月末倒数第二,
     cb_signal.py is_rebalance_day 语义)——活链换手/成本是否被回测低估?
  Q2 退市回收口径: V3 退市 pop 只扣 0.1% 成本、头寸价值归零(本金全损)——方向
     偏保守(转债退市多兑付≥100)。三臂: 0(现状全损) / last(最后价回收) /
     par(面值100回收), 量化偏差量级与方向。

口径与 V3(backtest_cb_eligibility_arms.py N=10) 完全一致:
  过滤共用 data/cb_select(闸门二十一) / N=10 无价 pop / 补足25只 / 双边0.1%。
仅两处改变: ①调仓日程 ②pop 时的回收值。面板构建与 V3 同源同过滤。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta
from data.cb_select import select_double_low

REAL_COST = 0.001
TOP_N = 25
N_POP = 10  # V3 定案 N=10

logger.info("加载快照...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

price_panel = {}
last_price = {}
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
            last_price[code] = p
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板: {len(price_panel)}交易日, {len(last_price)}只有价")

# 交易日历=SH 口径(与 cb_signal is_rebalance_day 同一输入)
cal = load_meta("trade_calendar")
cal_dates = set(cal["trade_date"].tolist()) if not cal.empty else set()


def is_rebalance_day(d: pd.Timestamp) -> bool:
    """复刻 daily_signal_a_v2.is_rebalance_day: 月末倒数第二 + 月中(n//2-1)。"""
    ds = str(d.date())[:10]
    if ds not in cal_dates:
        return False
    month_dates = sorted([x for x in cal_dates if x[:7] == ds[:7]])
    if not month_dates:
        return False
    end_idx = -2 if len(month_dates) >= 2 else -1
    if ds == month_dates[end_idx]:
        return True
    n = len(month_dates)
    mid_idx = max(0, n // 2 - 1)
    return n >= 2 and ds == month_dates[mid_idx]


def select_variant(snap, top, mode="static"):
    """评级过滤变体(2026-10-14 探针臂, 九轮/十三轮锁定口径):
    static=现口径(共享过滤) / static_suffixfixed=后缀映射修正(修复后效果,
    探测用, 不并入 cb_select 本体) / none=去评级过滤(探针 B 臂)。
    变体仅存在于本研究脚本, 共享过滤本体(data/cb_select)保持冻结不动。"""
    if mode == "static":
        return select_double_low(snap, top=top)
    df = snap.copy()
    if mode == "static_suffixfixed":
        df["rating"] = df["rating"].apply(
            lambda r: r[:-3] if isinstance(r, str) and r.endswith("sti") else r)
        return select_double_low(df, top=top)
    if mode == "none":
        df = df[~df["code"].astype(str).str.startswith("404")]
        if "size" in df.columns:
            df = df[df["size"] >= 0.5]
        if "price" in df.columns and "premium" in df.columns:
            df = df[~((df["price"] > 130) & (df["premium"] < 5))]
        df = df.sort_values("dblow")
        out = list(df["code"])
        return out[:top] if top else out
    raise ValueError(mode)


def run_bt(schedule="monthly", recovery="zero", stats=None, filter_mode="static"):
    """schedule: monthly=快照日调仓(V3同构) / biweekly=月中+月末倒数第二。
    recovery: zero=全损(现状) / last=按最后成交价回收 / par=按面值100回收。
    filter_mode: 评级过滤变体(探针用), 默认 static=现口径。"""
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    missing = {}
    snap_idx = 0
    last_snap_date = None
    last_acted_snap = None  # nextday 日程用: 已按此快照调过仓
    pops = temp_pops = skip_rebal = blocked_top = 0
    turnover = 0.0
    n_rebal = 0
    events = []  # (date_str, snap_date_str, enter_w) 事件级证据
    pop_events = []  # (date_str, code, weight, last_price) 退市事件级证据
    # 每只券记录"最后可见价"供回收臂用(退市券的最后日线价)
    final_last = {}

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

        # N 日无价才 pop(V3 同规则); 回收值三臂
        gone = []
        for c in list(cur_weights):
            if c in prices_today:
                missing[c] = 0
                final_last[c] = prices_today[c]
            else:
                missing[c] = missing.get(c, 0) + 1
                if missing[c] >= N_POP:
                    gone.append(c)
        for c in gone:
            w = cur_weights.pop(c)
            pop_events.append((date_str, c, round(w, 4),
                               round(final_last.get(c, 0), 3)))
            nav.iloc[i] -= w * REAL_COST
            if recovery == "last":
                nav.iloc[i] += w * 1.0  # 头寸价值保留(按最后可见价)
            elif recovery == "par":
                lp = final_last.get(c)
                nav.iloc[i] += w * (100.0 / lp) if lp and lp > 0 else w * 1.0
            # recovery=="zero": 不加回=本金全损(V3 现状)
            missing.pop(c, None)
            pops += 1
            dpos = all_dates.index(date)
            if any(c in price_panel.get(all_dates[j], {})
                   for j in range(dpos + 1, len(all_dates))):
                temp_pops += 1

        # 快照推进
        while (snap_idx < len(snap_dates)
               and str(snap_dates[snap_idx].date())[:10] <= date_str):
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        # 调仓日判定
        is_rb = (schedule == "monthly"
                 and last_snap_date is not None
                 and str(last_snap_date.date())[:10] == date_str)
        if schedule == "biweekly":
            is_rb = is_rebalance_day(date) and last_snap_date is not None
        if schedule == "nextday":
            # 每个快照日后的首个交易日调仓(隔离"快照日非交易日被跳过"的效应)
            is_rb = (last_snap_date is not None
                     and last_acted_snap != last_snap_date)
        if schedule == "quarterly":
            # 每季首快照(1/4/7/10月)后首个交易日调仓(2026-09-27 补:
            # 调仓频率待测登记的第三臂; nextday 式触发防快照日非交易日跳过)
            is_rb = (last_snap_date is not None
                     and last_acted_snap != last_snap_date
                     and last_snap_date.month in (1, 4, 7, 10))

        if is_rb:
            snap = snapshots[snapshots["snap_date"] == last_snap_date]
            if snap.empty:
                continue
            avails = [c for c in snap["code"].unique() if c in prices_today]
            full_rank = select_variant(snap, top=TOP_N, mode=filter_mode)
            blocked = [c for c in full_rank if c not in prices_today]
            blocked_top += len(blocked)
            selected = select_variant(
                snap[snap["code"].isin(avails)], top=TOP_N, mode=filter_mode)
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
            turnover += enter_w
            n_rebal += 1
            last_acted_snap = last_snap_date
            events.append((date_str, str(last_snap_date.date())[:10], round(enter_w, 4)))
            cur_weights = new_w
            for c in cur_weights:
                missing.setdefault(c, 0)

    if stats is not None:
        stats.update(pops=pops, temp_pops=temp_pops, skip_rebal=skip_rebal,
                     blocked_top=blocked_top, turnover=turnover, n_rebal=n_rebal,
                     events=events, pop_events=pop_events)
    return nav


def metrics(nav_s):
    active = nav_s.loc[snap_dates[0]:]
    n = len(active)
    ar = (active.iloc[-1] / active.iloc[0]) ** (252 / n) - 1
    dr = active.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    dd = (active / active.cummax() - 1).min()
    return ar, sr, dd


print("=" * 100)
print("CB双低 调仓日程×退市回收 对照 (N=10 / 补足25 / 双边0.1% / 共享过滤)")
print("=" * 100)

arms = {}
biweekly_events_zero = []
biweekly_pop_events = {}
print(f"\n{'臂':<34}{'年化':>8}{'夏普':>7}{'回撤':>9}{'换手/年':>9}{'pop':>6}{'临时':>5}{'调仓次数':>8}")
rows = []
# 探针模式(2026-10-14, 九轮锁定口径): 只跑 biweekly×par 三档评级过滤变体
if "--probe" in sys.argv:
    arm_list = [("biweekly", "par", "static"),
                ("biweekly", "par", "static_suffixfixed"),
                ("biweekly", "par", "none")]
else:
    arm_list = [(s, r, "static") for s in ["monthly", "nextday", "biweekly", "quarterly"]
                for r in ["zero", "last", "par"]]
for schedule, recovery, fmode in arm_list:
    if True:
        st = {}
        nav = run_bt(schedule, recovery, stats=st, filter_mode=fmode)
        ar, sr, dd = metrics(nav)
        key = f"{schedule}_{recovery}" + (f"_{fmode}" if fmode != "static" else "")
        arms[key] = nav
        if key == "biweekly_zero":
            biweekly_events_zero = st["events"]
        if schedule == "biweekly":
            biweekly_pop_events[key] = st["pop_events"]
        t1 = st["turnover"] / max(st["n_rebal"], 1) * 12
        tag = " (V3现状)" if key == "monthly_zero" else ""
        print(f"{key:<34}{ar*100:>+7.1f}%{sr:>7.3f}{dd*100:>+8.1f}%"
              f"{t1*100:>8.0f}%{st['pops']:>6}{st['temp_pops']:>5}{st['n_rebal']:>8}{tag}")
        rows.append(dict(arm=key, ann=ar, sharpe=sr, dd=dd, turn_py=t1,
                         pops=st["pops"], temp_pops=st["temp_pops"],
                         n_rebal=st["n_rebal"], skip=st["skip_rebal"],
                         blocked=st["blocked_top"]))

print("\n[解读锚点]")
print("  monthly_zero = V3 N=10 同构基线(对外参考 1.39 的臂)")
print("  nextday_zero = 隔离快照日跳过效应(每个快照后首个交易日调仓, 92 次全用)")
print("  biweekly_zero vs monthly_zero → Q1 频率不同构的量级(换手/夏普差)")
print("  zero vs last vs par 同日程内 → Q2 退市全损保守偏差的量级")
print("  par 臂: 回收=面值100/最后价, 溢价券少收、折价券多收(退市兑付下限近似)")
print("  注: 换手/年=每事件均值×12 口径(与 V3 脚本同式, 双周日程实际事件数~24/年, 真实年换手≈此值×2)")

out = pd.DataFrame({f"nav_{k}": v for k, v in arms.items()})
out.to_csv("logs/cb_sched_compare_nav.csv")
pd.DataFrame(rows).to_csv("logs/cb_sched_compare_stats.csv", index=False)
# 事件级证据(双周零回收臂): 逐事件 (日期, 所用快照, 入场换手)
pd.DataFrame(biweekly_events_zero,
             columns=["date", "snap", "enter_w"]).to_csv(
    "logs/cb_sched_compare_events.csv", index=False)
# 退市事件级证据(双周三臂): (date, code, weight, last_price)
pop_rows = []
for arm_name, evs in biweekly_pop_events.items():
    for d, c, w, lp in evs:
        pop_rows.append({"arm": arm_name, "date": d, "code": c,
                         "weight": w, "last_price": lp})
pd.DataFrame(pop_rows).to_csv("logs/cb_sched_pop_events.csv", index=False)
print("\n已保存 logs/cb_sched_compare_nav.csv / logs/cb_sched_compare_stats.csv"
      " / logs/cb_sched_compare_events.csv / logs/cb_sched_pop_events.csv")
