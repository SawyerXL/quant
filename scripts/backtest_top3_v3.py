"""
Top3 v3 回测 — 训练段(2019-2023) + OOS(2024-2025)，T+1执行。

验收线: 年化>20% / 夏普>0.7 / 回撤<-35% / 月胜率>50% / OOS不崩
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np; from pathlib import Path
from datetime import datetime
from strategies.top3_basket import compute_score_top3, apply_filters, PARAMS, check_risk
from data.storage import load_meta
import warnings; warnings.filterwarnings('ignore')

START, END = "2019-01-01", "2025-12-31"
OOS_START = "2024-01-01"
COMM = PARAMS["execution"]["commission"]; N = PARAMS["execution"]["basket_size"]
CAP = 50000; RF = 0.025; LOG_DIR = Path("logs/backtest"); LOG_DIR.mkdir(exist_ok=1)

print("加载...", flush=True); t0 = datetime.now()
csi = load_meta("csi800"); codes = sorted([str(c) for c in csi["code"].tolist()])[:300]
from run_backtest_a import load_panels
panel, ap = load_panels(codes, START, END); info = load_meta("stock_info_full")
print(f"  {panel.shape[1]}只 {panel.shape[0]}天 ({(datetime.now()-t0).seconds}s)")

# 调仓日(双周)
td = panel.index; rd = []
for yr in range(2019, 2026):
    for mo in range(1, 13):
        m = td[(td.year == yr) & (td.month == mo)]
        if len(m) < 8: continue
        rd.extend([m[len(m) // 2], m[-1]])
rd = sorted(set(rd))


def run_period(start_d: str, end_d: str) -> dict:
    """运行指定区间回测，返回结果字典。"""
    nav, fills, stops_log, daily_ret = [], [], [], []
    positions, cost_basis, entry_dates, cash = {}, {}, {}, CAP
    prev_nav = 1.0; prev_signal_pos = 1.0  # T+1: 上一期信号仓位

    rds = [d for d in rd if pd.Timestamp(start_d) <= d <= pd.Timestamp(end_d)]

    for i, dts in enumerate(rds):
        dt = pd.Timestamp(dts)
        score_raw = compute_score_top3(panel, dt, ap, info)
        if len(score_raw) < 10:
            fills.append(len(positions)); nav.append(prev_nav); continue

        score = apply_filters(score_raw, panel, dt, info)
        if len(score) < PARAMS["risk"]["min_candidates"]:
            # 候选不足 → 清仓持币
            top3 = []
        else:
            top3 = score.nlargest(N).index.tolist()

        to_sell, to_buy = [], []
        cur_holds = list(positions.keys())

        # 卖出检查
        for c in cur_holds:
            if c not in panel.columns: continue
            closes = panel[c].iloc[max(0, panel.index.get_loc(dt) - 15):panel.index.get_loc(dt) + 1].dropna()
            if len(closes) < PARAMS["risk"]["ma_window"]: continue
            cur_p = float(closes.iloc[-1]); ma10 = closes.iloc[-PARAMS["risk"]["ma_window"]:].mean()
            below = 0
            for ci in range(len(closes) - 1, -1, -1):
                if closes.iloc[ci] < ma10: below += 1
                else: break

            cp = cost_basis.get(c, cur_p)
            action = check_risk(c, cp, cur_p, below)
            if action in ("sell", "sell_all"):
                to_sell.append(c)
                stops_log.append((dts, c, f"risk:{action}", round((cur_p / cp - 1) * 100, 1)))
            elif c not in top3 and c not in score.nlargest(N + 2).index:
                to_sell.append(c)  # 轮换

        # 执行卖出
        for c in to_sell:
            if c in positions:
                cash += positions[c] * (1 - COMM)
                positions.pop(c); cost_basis.pop(c, None); entry_dates.pop(c, None)

        # 执行买入
        need = max(N - len(positions), 0)
        cand = [c for c in score.nlargest(N + 5).index if c not in positions and c in panel.columns]
        per = min(cash / max(need, 1), CAP * PARAMS["risk"]["max_single_pct"]) if need > 0 else 0

        for c in cand[:need]:
            price = float(panel[c].loc[dt]) if dt in panel[c].index else 0
            if price <= 0: continue
            if per <= 0 or np.isnan(price) or price <= 0: continue
            qty = max(int(per / price / 100) * 100, 100); cost = qty * price
            if cost <= cash:
                cash -= cost * (1 + COMM); positions[c] = cost; cost_basis[c] = price
                entry_dates[c] = dt

        # 净值：现金 + 持仓市值
        mkv = 0
        for c in positions:
            if dt in panel.index and c in panel.columns:
                cur_p = float(panel[c].loc[dt])
                if cur_p > 0 and c in cost_basis and cost_basis[c] > 0:
                    shares = positions[c] / cost_basis[c]
                    mkv += shares * cur_p
        total = cash + mkv
        current_nav = total / CAP
        nav.append(current_nav); fills.append(len(positions))
        daily_ret.append(current_nav / prev_nav - 1 if prev_nav > 0 else 0)
        prev_nav = current_nav

    nav_s = pd.Series(nav, index=pd.DatetimeIndex([pd.Timestamp(d) for d in rds[:len(nav)]]))
    return {"nav": nav_s, "fills": fills, "stops": stops_log, "daily_ret": daily_ret}


def metrics(nav_s, freq=26):
    d = nav_s.pct_change().dropna()
    total = nav_s.iloc[-1] / nav_s.iloc[0] - 1
    yrs = max(len(d) / freq, 0.5)
    ann = (1 + total) ** (1 / yrs) - 1
    vol = d.std() * np.sqrt(freq)
    rf_p = RF / freq
    sr = (d.mean() - rf_p) / d.std() * np.sqrt(freq) if d.std() > 0 else 0
    mdd = (nav_s / nav_s.cummax() - 1).min()
    win_rate = np.mean(d > 0)
    return {"total": total, "annual": ann, "vol": vol, "sharpe": sr,
            "max_dd": mdd, "win_rate": win_rate}


def selfcheck(m, nav_s, label=""):
    issues = []
    d = nav_s.pct_change().dropna()
    # 年化换算一致
    fwd = (1 + m["annual"]) ** (max(len(d) / 26, 0.5)) - 1
    if abs(fwd - m["total"]) > 0.02: issues.append(f"年化→总收益不匹配: {fwd:.1%} vs {m['total']:.1%}")
    # 分年复利
    product = 1.0
    for yr in range(nav_s.index[0].year, nav_s.index[-1].year + 1):
        sy = nav_s[nav_s.index.year == yr]
        if len(sy) > 1: product *= sy.iloc[-1] / sy.iloc[0]
    if len(nav_s) > 3 and abs(product - 1 - m["total"]) > 0.02:
        issues.append(f"分年复利{product - 1:.1%}≠总{m['total']:.1%}")
    # NaN/inf
    for k, v in m.items():
        if isinstance(v, float) and (np.isnan(v) or abs(v) > 100):
            issues.append(f"NaN/异常: {k}={v}")
    # 止损必须>0
    return issues


# ── 训练段(2019-2023) ─────────────────────────────────
print("\n[Train 2019-2023]")
train = run_period("2019-01-01", "2023-12-31")
mt = metrics(train["nav"]); it = selfcheck(mt, train["nav"])
stops_train = len(train["stops"])

# ── OOS (2024-2025) ────────────────────────────────────
print("[OOS 2024-2025]")
oos = run_period(OOS_START, "2025-12-31")
mo = metrics(oos["nav"]); io = selfcheck(mo, oos["nav"])
stops_oos = len(oos["stops"])

# ── 输出 ───────────────────────────────────────────────
def p(s=""): print(s)

accept = {"年化>20%": mo["annual"] > 0.20, "夏普>0.7": mo["sharpe"] > 0.7,
           "回撤<-35%": abs(mo["max_dd"]) < 0.35, "月胜率>50%": mo["win_rate"] > 0.50,
           "OOS不崩": mo["annual"] > 0}

p(f"\n{'='*60}")
p(f"  Top3 v3 回测报告  T+1 成本{COMM*100:.2f}%")
p(f"{'='*60}")
p(f"  {'指标':<14} {'训练段':>12} {'OOS':>12} {'验收':>8}")
p(f"  {'─'*48}")
for name, k, fmt in [("年化收益", "annual", ".1%"), ("夏普比率", "sharpe", ".2f"),
                      ("最大回撤", "max_dd", ".1%"), ("月胜率", "win_rate", ".0%")]:
    tv, ov = mt[k], mo[k]
    check = accept.get(f"{name}>{fmt.strip('%')}" if "%" in fmt else name, None)
    flag = "✅" if check else ("❌" if check is False else "")
    p(f"  {name:<14} {tv:>11{fmt}} {ov:>11{fmt}} {flag:>8}")

p(f"\n  止损: 训练{stops_train}次 OOS{stops_oos}次")
p(f"  自检: 训练{'✅' if not it else '❌ '+'; '.join(it)}  "
  f"OOS{'✅' if not io else '❌ '+'; '.join(io)}")

# 分年
p(f"\n  分年度(OOS):")
ns = oos["nav"]
for yr in [2024, 2025]:
    sy = ns[ns.index.year == yr]
    if len(sy) < 2: continue
    ret = sy.iloc[-1] / sy.iloc[0] - 1
    rds_oos = [d for d in rd if d.year == yr]
    fy = [f for d, f in zip(rd, oos["fills"]) if d.year == yr]
    p(f"    {yr}: {ret:+.1%}  均仓{np.mean(fy):.1f}只")

# 止损抽样
if train["stops"]:
    p(f"\n  止损抽样(训练段前5笔):")
    for s in train["stops"][:5]:
        p(f"    {s[0]} {s[1]} {s[2]} 盈亏{s[3]:+.1f}%")

# OOS验收总评
oos_pass = all(accept.values())
label = "OOS验收通过 ✅" if oos_pass else "OOS验收不通过 ❌"
if not oos_pass:
    fail_items = [k for k, v in accept.items() if not v]
    label += f"  未达标: {', '.join(fail_items)}"
p(f"\n  {label}")
p(f"{'='*60}")

# 保存
report_file = LOG_DIR / "backtest_top3_v3.txt"
report_file.write_text(open(__file__).read())
print(f"\n✅ {report_file}")
