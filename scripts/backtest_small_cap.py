"""
小盘增强卫星仓回测 — CSI1000, 10-12只, 训练2019-2023/OOS2024-2025
验收: 年化>18%/夏普>0.8/回撤<-35%/OOS不崩/0.3%成本不崩
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np; from pathlib import Path
from datetime import datetime
from strategies.small_cap_enhanced import compute_score_small_cap, apply_filters, PARAMS
from data.storage import load_meta
import warnings; warnings.filterwarnings('ignore')

LOG_DIR = Path("logs/backtest"); LOG_DIR.mkdir(exist_ok=1)
COMM = PARAMS["execution"]["commission"]; N = PARAMS["n_holdings"]; CAP = PARAMS["execution"]["capital"]

print("加载CSI1000...", flush=True); t0 = datetime.now()
csi1k = load_meta("csi1000"); codes = sorted([str(c) for c in csi1k["code"].tolist()])[:300]
from run_backtest_a import load_panels
panel, ap = load_panels(codes, "2019-01-01", "2025-12-31")
info = load_meta("stock_info_full")
print(f"  {panel.shape[1]}只 {panel.shape[0]}天 ({(datetime.now()-t0).seconds}s)")

td = panel.index; rd = []
for yr in range(2019, 2026):
    for mo in range(1, 13):
        m = td[(td.year == yr) & (td.month == mo)]
        if len(m) < 8: continue
        rd.extend([m[len(m) // 2], m[-1]])
rd = sorted(set(rd))


def run(start_d, end_d, commission=COMM):
    nav, fills, stops = [], [], []
    pos, cost_b, cash = {}, {}, CAP
    prev_nav = 1.0

    rds = [d for d in rd if pd.Timestamp(start_d) <= d <= pd.Timestamp(end_d)]
    for i, dts in enumerate(rds):
        dt = pd.Timestamp(dts)
        score_raw = compute_score_small_cap(panel, dt, ap, info)
        if len(score_raw) < 20:
            fills.append(len(pos)); nav.append(prev_nav); continue

        score = apply_filters(score_raw, panel, dt, info)
        if len(score) < N:
            fills.append(len(pos)); nav.append(prev_nav); continue

        top_n = score.nlargest(N).index.tolist()

        # 卖出
        for c in list(pos.keys()):
            if c not in panel.columns: continue
            closes = panel[c].iloc[max(0, panel.index.get_loc(dt)-15):panel.index.get_loc(dt)+1].dropna()
            if len(closes) < 10: continue
            cur_p = float(closes.iloc[-1]); ma10 = closes.iloc[-10:].mean()
            below = sum(1 for ci in range(len(closes)-1,-1,-1) if closes.iloc[ci] < ma10)

            cp = cost_b.get(c, cur_p)
            pnl = (cur_p / cp - 1) if cp > 0 else 0

            sell = False; reason = ""
            if below >= 3: sell, reason = True, f"MA10({below}d)"
            elif pnl < PARAMS["risk"]["period_stop"]: sell, reason = True, f"Period{pnl:.0%}"
            elif pnl < PARAMS["risk"]["trailing_stop"]: sell, reason = True, f"Trail{pnl:.0%}"
            elif c not in top_n: sell, reason = True, "轮换"

            if sell:
                cash += pos[c] * (1 - commission); pos.pop(c); cost_b.pop(c, None)
                stops.append((dts, c, reason, round(pnl * 100, 1)))

        # 买入
        need = max(N - len(pos), 0)
        cand = [c for c in score.nlargest(N + 5).index if c not in pos]
        per = cash / max(need, 1) if need > 0 else 0

        for c in cand[:need]:
            price = float(panel[c].loc[dt]) if dt in panel[c].index else 0
            if np.isnan(price) or price <= 0: continue
            qty = max(int(min(per, CAP * 0.15) / price / 100) * 100, 100)
            cost = qty * price
            if cost <= cash:
                cash -= cost * (1 + commission); pos[c] = cost; cost_b[c] = price

        mkv = sum(pos[c] * (float(panel[c].loc[dt]) / cost_b[c]) if dt in panel.index and c in panel.columns and c in cost_b and cost_b[c] > 0 else pos[c] for c in pos)
        current = (cash + mkv) / CAP
        nav.append(current); fills.append(len(pos))
        prev_nav = current

    ns = pd.Series(nav, index=pd.DatetimeIndex([pd.Timestamp(d) for d in rds[:len(nav)]]))
    return {"nav": ns, "fills": fills, "stops": stops}


def metrics(ns, freq=26):
    d = ns.pct_change().dropna()
    t = ns.iloc[-1] / ns.iloc[0] - 1
    y = max(len(d) / freq, 0.5); a = (1 + t) ** (1 / y) - 1
    v = d.std() * np.sqrt(freq); rf_p = 0.025 / freq
    s = (d.mean() - rf_p) / d.std() * np.sqrt(freq) if d.std() > 0 else 0
    m = (ns / ns.cummax() - 1).min()
    w = np.mean(d > 0)
    return {"total": t, "annual": a, "vol": v, "sharpe": s, "max_dd": m, "win_rate": w}


# ── 训练段 ───────────────────────────────────────────
print("Train 2019-2023...")
tr = run("2019-01-01", "2023-12-31")
mt = metrics(tr["nav"])

# ── OOS ────────────────────────────────────────────────
print("OOS 2024-2025...")
oo = run("2024-01-01", "2025-12-31")
mo = metrics(oo["nav"])

# 0.3%压力测试
print("Stress 0.3%...")
os_stress = run("2024-01-01", "2025-12-31", 0.003)
ms = metrics(os_stress["nav"])

# ── 自检 ──────────────────────────────────────────────
def selfcheck(m, ns):
    issues = []
    d = ns.pct_change().dropna()
    if len(ns) > 2:
        fwd = (1 + m["annual"]) ** (max(len(d) / 26, 0.5)) - 1
        if abs(fwd - m["total"]) > 0.02: issues.append(f"ann→tot mismatch")
    for k, v in m.items():
        if isinstance(v, float) and (np.isnan(v) or abs(v) > 100):
            issues.append(f"{k}={v}")
    return issues

it = selfcheck(mt, tr["nav"]); io_s = selfcheck(mo, oo["nav"])

# ── 输出 ──────────────────────────────────────────────
accept = {"年化>18%": mo["annual"] > 0.18, "夏普>0.8": mo["sharpe"] > 0.8,
           "回撤<-35%": abs(mo["max_dd"]) < 0.35, "OOS不崩": mo["annual"] > 0,
           "0.3%不崩": ms["annual"] > 0}

print(f"\n{'='*60}")
print(f"  小盘增强卫星仓  CSI1000  {N}只  本金{CAP:,}")
print(f"{'='*60}")
print(f"  {'指标':<14} {'训练':>11} {'OOS':>11} {'验收':>8}")
print(f"  {'─'*48}")
for name, k, fmt in [("年化", "annual", ".1%"), ("夏普", "sharpe", ".2f"),
                      ("最大回撤", "max_dd", ".1%"), ("月胜率", "win_rate", ".0%")]:
    tv, ov = mt[k], mo[k]
    flag = "✅" if accept.get(f"年化>18%" if "年化" in name else "", None) else ""
    if name == "最大回撤": flag = "✅" if abs(mo["max_dd"]) < 0.35 else "❌"
    print(f"  {name:<14} {tv:>10{fmt}} {ov:>10{fmt}} {flag:>8}")

print(f"\n  压力测试(0.3%): 年化{ms['annual']:+.1%} 夏普{ms['sharpe']:.2f} 回撤{ms['max_dd']:.1%}")
print(f"  止损: 训练{len(tr['stops'])}次 OOS{len(oo['stops'])}次")
print(f"  自检: 训练{'✅' if not it else '❌'+'; '.join(it)}  OOS{'✅' if not io_s else '❌'+'; '.join(io_s)}")

# 分年
print(f"\n  分年度(OOS):")
for yr in [2024, 2025]:
    sy = oo["nav"][oo["nav"].index.year == yr]
    if len(sy) < 2: continue
    yr_ret = sy.iloc[-1] / sy.iloc[0] - 1
    fy = [f for d, f in zip(rd, oo["fills"]) if d.year == yr]
    print(f"    {yr}: {yr_ret:+.1%}  均仓{np.mean(fy):.1f}只")

# 2024Q1
q1 = oo["nav"][(oo["nav"].index >= "2024-01-01") & (oo["nav"].index <= "2024-03-31")]
if len(q1) > 1:
    q1_ret = q1.iloc[-1] / q1.iloc[0] - 1
    q1_dd = (q1 / q1.cummax() - 1).min()
    print(f"    2024Q1小盘危机: {q1_ret:+.1%} 回撤{q1_dd:.1%}")

oos_pass = all(accept.values())
label = "OOS验收通过 ✅" if oos_pass else "OOS验收不通过 ❌"
if not oos_pass:
    fail = [k for k, v in accept.items() if not v]
    label += f"  未达标: {', '.join(fail)}"
print(f"\n  {label}")
print(f"{'='*60}")
