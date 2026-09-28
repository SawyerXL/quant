"""CB 全期回测可成交性审计 (2026-09-28 用户立项, 优先级高于扩容)。

背景: 流动性三档首跑实锤 128028@2019-11-01 全天仅 1263 张成交(13万),
回测按收盘价成交 2.4万=当日量的 18%——现实中做不出来。若 2019 段超额主要
来自这类不可成交交易, V3 夏普 1.39 含假段。

方法(用户给): 扫描全期回测交易, 标出 成交额 > 当日总成交额×10% 的笔数,
按年份统计; 变体=跳过不可成交的买入(向下补足), 对比剔除前后 2019 及全期贡献。
口径: 与 V3(backtest_cb_eligibility_arms N=10) 同构; 每券交易额按现行
2.4万(60万/25)为主, 3.8万(96万满配)为参考档。
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
N_POP = 10
SHARE_THRESH = 0.10  # 单笔 > 当日成交额×10% → 标不可成交

logger.info("加载快照/面板...")
snapshots = load_meta("cb_snapshots")
snapshots["snap_date"] = pd.to_datetime(snapshots["snap_date"])
snap_dates = sorted(snapshots["snap_date"].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots["code"].unique())

price_panel = {}
turnover_panel = {}   # (date, code) -> 当日成交额(元) = volume×close
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
            v = row.get("volume")
            if v is not None and float(v) > 0:
                turnover_panel.setdefault(d, {})[code] = float(v) * p
            prev = p

all_dates = sorted(price_panel.keys())
logger.info(f"  面板 {len(price_panel)}交易日")


def run_bt(skip_untradable, trade_amt, flag_log):
    """skip_untradable=True: 买入占比>10% 的券跳过并向下补足(变体);
    False: 基线(V3 同构)。flag_log 收集被标不可成交的 (date, code, 金额, 占比)。"""
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    missing = {}
    snap_idx = 0
    last_snap_date = None

    for i, date in enumerate(all_dates):
        date_str = str(date.date())[:10]
        prices_today = price_panel.get(date, {})
        turn_today = turnover_panel.get(date, {})

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
                if missing[c] >= N_POP:
                    gone.append(c)
        for c in gone:
            w = cur_weights.pop(c)
            nav.iloc[i] -= w * REAL_COST
            missing.pop(c, None)
            # 卖出侧标定: pop 清仓(退市/停牌)无当日量→不可成交性按最后可见日记
            flag_log.append((date_str, c, trade_amt, "pop"))

        while (snap_idx < len(snap_dates)
               and str(snap_dates[snap_idx].date())[:10] <= date_str):
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        if last_snap_date and str(last_snap_date.date())[:10] == date_str:
            snap = snapshots[snapshots["snap_date"] == last_snap_date]
            if snap.empty:
                continue
            avails = [c for c in snap["code"].unique() if c in prices_today]
            full_rank = select_double_low(snap, top=None)  # 全排名供补足
            full_rank = [c for c in full_rank if c in avails]
            if len(full_rank) < TOP_N:
                continue
            selected = []
            for c in full_rank:
                t = turn_today.get(c)
                if t and trade_amt > SHARE_THRESH * t:
                    flag_log.append((date_str, c, trade_amt,
                                     trade_amt / t))
                    if skip_untradable:
                        continue
                selected.append(c)
                if len(selected) >= TOP_N:
                    break
            if len(selected) < TOP_N:
                continue
            n = len(selected)
            new_w = {c: 1.0 / n for c in selected}

            old_set = set(cur_weights.keys())
            new_set = set(new_w.keys())
            # 卖出侧标定: 轮出券按当日成交额占比, 同一 10% 阈值
            for c in old_set - new_set:
                t = turn_today.get(c)
                if t and trade_amt > SHARE_THRESH * t:
                    flag_log.append((date_str, c, trade_amt, trade_amt / t, "sell"))
            enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w) / 2 * REAL_COST * 2)
            cur_weights = new_w
            for c in cur_weights:
                missing.setdefault(c, 0)
    return nav


def metrics(nav_s, lo=None, hi=None):
    active = nav_s.loc[snap_dates[0]:] if lo is None else nav_s[(nav_s.index >= lo) & (nav_s.index <= hi)]
    n = len(active)
    ar = (active.iloc[-1] / active.iloc[0]) ** (252 / n) - 1
    dr = active.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    return ar, sr


for tier_name, TRADE_AMT in [("现行2.4万/券(60万档)", 24_000),
                             ("参考3.8万/券(96万满配)", 38_000)]:
    print("=" * 96)
    print(f"可成交性审计 | {tier_name} | 阈值=单笔>{SHARE_THRESH:.0%}×当日成交额")
    print("=" * 96)
    base_flags, var_flags = [], []
    nav_base = run_bt(skip_untradable=False, trade_amt=TRADE_AMT, flag_log=base_flags)
    nav_var = run_bt(skip_untradable=True, trade_amt=TRADE_AMT, flag_log=var_flags)

    # 标定笔分类: 买入=(date,code,amt,share)4元 / pop=(date,code,amt,"pop")4元 /
    # 卖出=(date,code,amt,share,"sell")5元——按 len 与第4元素类型区分, 不按 len 猜。
    def classify(f):
        if len(f) == 5:
            return "sell"
        return "buy" if isinstance(f[3], float) else "pop"

    rows = [(f[0], f[1], f[3], classify(f))
            for f in base_flags
            if (len(f) == 5) or (len(f) == 4 and isinstance(f[3], float))]
    fl = pd.DataFrame(rows, columns=["date", "code", "share", "side"])
    fl["year"] = fl["date"].str[:4]
    n_buy = int((fl["side"] == "buy").sum())
    n_sell = int((fl["side"] == "sell").sum())
    n_pop = sum(1 for f in base_flags if len(f) == 4 and not isinstance(f[3], float))
    n_var_skip = sum(1 for f in var_flags
                     if len(f) == 4 and isinstance(f[3], float))
    print(f"\n被标笔数: 买入 {n_buy} (变体实跳 {n_var_skip}) | 卖出 {n_sell} | pop {n_pop}")
    for side, nm in [("buy", "买入侧"), ("sell", "卖出侧")]:
        sub = fl[fl["side"] == side]
        print(f"  [{nm} 按年份]")
        for y in range(2019, 2027):
            yy = sub[sub["year"] == str(y)]
            if len(yy) == 0:
                continue
            print(f"    {y}: {len(yy):>3} 笔 | 最大占比 {yy['share'].max()*100:5.1f}%"
                  f" | P95 {np.percentile(yy['share'], 95)*100:5.1f}%")

    a0, s0 = metrics(nav_base)
    a1, s1 = metrics(nav_var)
    print(f"\n全期: 基线 年化{a0*100:+.1f}% 夏普{s0:.3f} | 剔不可成交买入后 年化{a1*100:+.1f}% 夏普{s1:.3f}"
          f" | 差 {a1*100-a0*100:+.2f}pp")

    print("逐年(基线 vs 剔除):")
    for y in range(2019, 2027):
        lo, hi = f"{y}-01-01", f"{y}-12-31"
        try:
            a0y, _ = metrics(nav_base, lo, hi)
            a1y, _ = metrics(nav_var, lo, hi)
            print(f"  {y}: {a0y*100:+6.1f}% → {a1y*100:+6.1f}%  差{a1y*100-a0y*100:+6.1f}pp")
        except Exception:
            print(f"  {y}: 样本不足")
    print()
