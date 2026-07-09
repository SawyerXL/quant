"""
个人账户业绩追踪 v3 — 双框架: MA10-4d(决策) + 十维评分(参考) + 增强深挖
用法: python scripts/personal_pnl_tracker.py [cash_amount]
"""
import sys, json, requests, re
from pathlib import Path
from datetime import datetime, date
import pandas as pd, numpy as np
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger

HOLDINGS_FILE = Path("config/my_holdings.csv")
HISTORY_FILE = Path("logs/personal_nav_history.parquet")
logger.add("logs/pnl_tracker.log", rotation="7 days")

# ══════════════════════════════════
# Data Layer
# ══════════════════════════════════
def _rt_price(code):
    exch = "sh" if code.startswith(("6","68")) else "sz"
    try:
        resp = requests.get(f"http://hq.sinajs.cn/list={exch}{code}",
            headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
        return float(resp.text.split('"')[1].split(",")[3])
    except: return 0

def _load_holdings():
    df = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return df[(df["monitor"] == True) & (df["shares"] > 0)]

def _tech_analysis(code):
    """返回完整技术指标: (days_below, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60)"""
    from data.storage import load_daily
    try:
        df = load_daily(code, "2026-01-01", date.today().strftime("%Y-%m-%d"))
        if df.empty: return None
        df["dt"] = pd.to_datetime(df["date"]); df = df.set_index("dt").sort_index()
        cl = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(cl) < 60: return None
        ma10 = float(cl.iloc[-10:].mean())
        ma20 = float(cl.iloc[-20:].mean())
        ma60 = float(cl.iloc[-60:].mean())
        ret20 = float((cl.iloc[-1]/cl.iloc[-21]-1)*100) if len(cl)>=21 else 0.0
        dh = float((cl.iloc[-1]/cl.iloc[-20:].max()-1)*100)
        boll_low = float(ma20 - 2*cl.iloc[-20:].std())
        low60 = float(cl.iloc[-60:].min())
        days_below = 0
        for j in range(len(cl)-1, max(0, len(cl)-15), -1):
            if cl.iloc[j] < float(cl.iloc[max(0,j-9):j+1].mean()):
                days_below += 1
            else: break
        delta = cl.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        rsi_v = 100 - (100/(1 + gain.rolling(14).mean()/loss.rolling(14).mean()))
        rsi = float(rsi_v.iloc[-1]) if not pd.isna(rsi_v.iloc[-1]) else 50.0
        return (days_below, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60)
    except: return None

def _correct_with_live(tech, live_price):
    """用实时价修正本地MA10数据: 若实时价>MA10则重置跌破天数"""
    if tech is None or live_price <= 0: return tech
    db, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60 = tech
    if ma10 > 0 and live_price > ma10:
        db = 0  # 实时价站上MA10 → 重置计数器
    return (db, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60)

def _top60_check():
    from data.storage import load_meta, load_daily
    try:
        c800 = load_meta("csi800")
        codes_all = sorted(c800["code"].astype(str).str.zfill(6))
        turnover = {}
        for c in codes_all:
            try:
                df = load_daily(c, "2026-05-01", date.today().strftime("%Y-%m-%d"))
                if df.empty: continue
                df["date"] = pd.to_datetime(df["date"]); df = df.set_index("date").sort_index()
                amt = pd.to_numeric(df.get("amount", pd.Series(dtype=float)), errors="coerce").dropna()
                if len(amt) >= 20: turnover[c] = amt.iloc[-20:].mean()
            except: pass
        return set(c for c, _ in sorted(turnover.items(), key=lambda x: x[1], reverse=True)[:60])
    except: return set()

# ══════════════════════════════════
# Main
# ══════════════════════════════════
def run(cash=0):
    today = date.today().strftime("%Y-%m-%d")
    df = _load_holdings()
    top60 = _top60_check()

    holdings = []
    total_mkt = 0; total_cost = 0

    for _, r in df.iterrows():
        code = r["code"]; name = r["name"]
        cost = float(r["cost_price"]); shares = int(r["shares"])
        cur = _rt_price(code) or cost
        pnl_pct = (cur/cost - 1) * 100 if cost > 0 else 0
        mkt_val = cur * shares
        total_mkt += mkt_val; total_cost += cost * shares

        tech = _tech_analysis(code)
        if tech:
            tech = _correct_with_live(tech, cur)  # ← 实时价修正MA10
            db, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60 = tech
        else:
            db, ma10, ma20, ma60, ret20, dh, rsi, boll_low, low60 = 0, 0.0, 0.0, 0.0, 0.0, 0.0, 50.0, 0.0, 0.0

        in_pool = code in top60

        # ═══ 旧框架: 十维评分 ═══
        old_score = 0; old_plus = []; old_minus = []
        if db == 0: old_score += 3; old_plus.append("MA10上")
        elif db >= 4: old_score -= 5; old_minus.append(f"MA10下{db}d")
        elif db >= 2: old_score -= 2; old_minus.append(f"MA10下{db}d")
        if dh < -10: old_score += 4; old_plus.append(f"深调{dh:.0f}%")
        elif dh < -5: old_score += 3; old_plus.append(f"回调{dh:.0f}%")
        elif dh > -3: old_score -= 1; old_minus.append("近高")
        if ret20 > 30: old_score -= 5; old_minus.append("过热")
        elif ret20 > 50: old_score -= 3; old_minus.append("严重过热")
        if rsi < 30: old_score += 3; old_plus.append("超卖")
        elif rsi > 70: old_score -= 2; old_minus.append("超买")
        if pnl_pct > 50: old_minus.append("浮盈锁利")
        if pnl_pct < -15: old_minus.append(f"深亏{pnl_pct:.0f}%")
        if ma60 > 0 and cur > ma60: old_score += 2; old_plus.append("多头排列")

        if old_score >= 5: old_v = "🟢强势"
        elif old_score >= 0: old_v = "🟡中性"
        elif old_score >= -5: old_v = "🟠偏弱"
        else: old_v = "🔴卖出"

        # ═══ 新框架: MA10-4d + TP ═══
        if pnl_pct >= 60: new_v = "🔴TP2卖2/3"; new_act = "SELL"
        elif pnl_pct >= 30: new_v = "🟡TP1卖1/3"; new_act = "SELL"
        elif db >= 4: new_v = "🔴清仓"; new_act = "SELL"
        elif db >= 3: new_v = "🟠预警"; new_act = "WARN"
        elif db >= 2: new_v = "🟡关注"; new_act = "WATCH"
        else: new_v = "🟢持有"; new_act = "HOLD"

        # ═══ 增强深挖触发 ═══
        deep_reasons = []
        if abs(old_score) >= 5: deep_reasons.append("评分极端")
        if pnl_pct > 50 or pnl_pct < -20: deep_reasons.append("盈亏极端")
        if rsi > 75 or rsi < 25: deep_reasons.append("RSI极端")
        need_deep = len(deep_reasons) > 0

        holdings.append({
            "code": code, "name": name, "shares": shares, "cost": cost,
            "cur": cur, "pnl_pct": pnl_pct, "mkt_val": mkt_val,
            "ma10": ma10, "ma20": ma20, "ma60": ma60, "boll_low": boll_low, "low60": low60,
            "days_below": db, "ret20": ret20, "dh": dh, "rsi": rsi,
            "old_score": old_score, "old_v": old_v, "old_plus": old_plus, "old_minus": old_minus,
            "new_v": new_v, "new_act": new_act,
            "deep_reasons": deep_reasons, "need_deep": need_deep,
            "in_pool": in_pool,
        })

    real_pnl = total_mkt - total_cost
    real_ret = (real_pnl / total_cost * 100) if total_cost > 0 else 0

    # ── NAV History ──
    if HISTORY_FILE.exists():
        hist = pd.read_parquet(HISTORY_FILE)
    else:
        hist = pd.DataFrame(columns=["date","mkt_val","cost_basis","cash","real_pnl","real_ret"])

    # 判断持仓是否变动(与上次运行比): cost变化>5%视为调仓
    if len(hist) > 0:
        last_cost = hist.iloc[-1]["cost_basis"]
        cost_change_pct = abs(total_cost - last_cost) / last_cost * 100 if last_cost > 0 else 0
    else:
        cost_change_pct = 0

    new_row = pd.DataFrame([{"date": today, "mkt_val": total_mkt, "cost_basis": total_cost,
        "cash": cash, "real_pnl": real_pnl, "real_ret": round(real_ret, 2)}])
    hist = pd.concat([hist, new_row], ignore_index=True)
    hist = hist.drop_duplicates(subset=["date"], keep="last")
    hist.to_parquet(HISTORY_FILE, index=False)

    # ── 时间加权收益 (排除资金进出干扰) ──
    hist = hist.sort_values("date").reset_index(drop=True)
    n = len(hist)
    organic_ret = []  # 时间加权日收益
    true_nav = [1.0]  # 修正后净值
    rebalance_flags = []  # 标记调仓日
    if n >= 2:
        for i in range(1, n):
            mkt_t = hist.iloc[i]["mkt_val"]; mkt_p = hist.iloc[i-1]["mkt_val"]
            cost_t = hist.iloc[i]["cost_basis"]; cost_p = hist.iloc[i-1]["cost_basis"]
            cash_in = max(0, cost_t - cost_p)
            cash_out = max(0, cost_p - cost_t)
            cost_chg_pct = abs(cost_t - cost_p) / cost_p * 100 if cost_p > 0 else 0
            is_rebalance = cost_chg_pct > 5  # 成本变化>5%=调仓日

            if is_rebalance:
                # 调仓日: 持仓快照变了, 跳过这天的收益计算
                rebalance_flags.append(i)
                true_nav.append(true_nav[-1])  # 净值延续
            elif mkt_p > 0:
                r = (mkt_t - cash_in + cash_out - mkt_p) / mkt_p * 100
                organic_ret.append(r)
                true_nav.append(true_nav[-1] * (1 + r/100))
            else:
                organic_ret.append(0.0)
                true_nav.append(true_nav[-1])

    # ══════════════════════════════════
    # 输出
    # ══════════════════════════════════
    print(f"\n{'='*95}")
    print(f"  个人账户双框架分析  {today}")
    print(f"  决策: MA10-4d + TP30/60%  |  参考: 十维评分 + 增强深挖")
    print(f"{'='*95}")

    # ── 绩效 (时间加权) ──
    total_capital = total_cost + cash  # 总投入 = 持仓成本 + 闲置现金
    print(f"\n  💰 总览: 市值¥{total_mkt:,.0f} | 总投入¥{total_capital:,.0f} | 累计盈亏¥{total_mkt-total_cost:+,.0f} ({(total_mkt/total_cost-1)*100:+.2f}%) | 现金¥{cash:,.0f}")

    if len(organic_ret) > 0:
        valid_ret = [r for r in organic_ret if r is not None]
        org_mean = np.mean(valid_ret) if valid_ret else 0; org_std = np.std(valid_ret) if valid_ret else 0
        sharpe = (org_mean*252-0.02)/(org_std*np.sqrt(252)) if org_std > 0 else 0
        nav_arr = np.array(true_nav)
        cummax = np.maximum.accumulate(nav_arr)
        max_dd = min((nav_arr-cummax)/cummax)*100
        win_rate = (np.array(valid_ret) > 0).mean()*100 if valid_ret else 0
        total_ret = (true_nav[-1] - 1) * 100
        n_valid = len(valid_ret)
        ann_ret = (true_nav[-1]**(252/n_valid) - 1) * 100 if n_valid > 0 else 0
        print(f"  📈 绩效({n_valid}个有效交易日, 时间加权, 调仓日跳过):")
        print(f"  累计收益: {total_ret:+.2f}% | 年化: {ann_ret:+.2f}% | 日收益均值: {org_mean:+.2f}% | 日波动: {org_std:.2f}%")
        if n_valid >= 5: print(f"  夏普: {sharpe:.2f} | 最大回撤: {max_dd:+.1f}% | 胜率: {win_rate:.0f}%")
        else: print(f"  胜率: {win_rate:.0f}% (夏普/回撤需≥5天)")

        # 逐日
        print(f"\n  📊 逐日:")
        ret_idx = 0
        for i in range(1, n):
            dt = str(hist.iloc[i]['date'])[:10]
            if i in rebalance_flags:
                print(f"  {dt}: 🔄 调仓日(持仓变动, 跳过) | 净值 {true_nav[i]:.4f}")
            elif ret_idx < len(organic_ret):
                r = organic_ret[ret_idx]
                flag = ' ⚠️周末' if pd.to_datetime(dt).weekday() >= 5 else ''
                print(f"  {dt}: {r:+.2f}% | 净值 {true_nav[i]:.4f}{flag}")
                ret_idx += 1

    try:
        from data.storage import load_meta
        idx = load_meta("csi800_index")
        if not idx.empty:
            idx_s = idx.set_index("date")["close"].sort_index()
            idx_ret = (idx_s.iloc[-1]/idx_s.iloc[-2]-1)*100 if len(idx_s)>=2 else 0
            print(f"  CSI800: {idx_ret:+.2f}%")
    except: pass

    # ── 双框架表格 ──
    sev = {"SELL":0, "WARN":1, "WATCH":2, "HOLD":3}
    holdings.sort(key=lambda h: (sev.get(h["new_act"],99), h["pnl_pct"]))

    print(f"\n  {'─'*95}")
    print(f"  {'代码':<8} {'名称':<6} {'现价':>7} {'盈亏':>7} {'MA10':>7} {'破':>3s} {'距高':>6} {'RSI':>4} {'旧评':>4} {'旧':<8} {'新框架':<16} {'增强'} {'池'}")
    print(f"  {'─'*95}")
    for h in holdings:
        ma_s = f"{h['ma10']:.1f}" if h['ma10'] > 0 else "N/A"
        deep = "⚠️" if h['need_deep'] else "—"
        pool = "内" if h['in_pool'] else "外"
        print(f"  {h['code']:<8} {h['name']:<6} {h['cur']:>7.2f} {h['pnl_pct']:>+6.1f}% {ma_s:>7} {h['days_below']:>2d}d {h['dh']:>+5.0f}% {h['rsi']:>4.0f} {h['old_score']:>+3d} {h['old_v']:<8} {h['new_v']:<16} {deep}  {pool}")

    # ── 新框架操作 ──
    sells = [h for h in holdings if h['new_act'] == 'SELL']
    warns = [h for h in holdings if h['new_act'] == 'WARN']
    watches = [h for h in holdings if h['new_act'] == 'WATCH']

    if sells or warns:
        print(f"\n  {'─'*95}")
        print(f"  [操作清单] 新框架触发:")
        for h in sells:
            if 'TP' in h['new_v']:
                frac = 2/3 if 'TP2' in h['new_v'] else 1/3
                n_sell = int(h['shares']*frac)
                print(f"  🔴 {h['code']} {h['name']}: 止盈卖{n_sell}股留{h['shares']-n_sell}股 ≈ ¥{n_sell*h['cur']:,.0f} | 盈亏{h['pnl_pct']:+.0f}%")
            else:
                print(f"  🔴 {h['code']} {h['name']}: 清仓{h['shares']}股 ≈ ¥{h['mkt_val']:,.0f} | MA10下{h['days_below']}d | 盈亏{h['pnl_pct']:+.1f}%")
        if warns: print(f"  🟠 预警: {', '.join(h['code'] for h in warns)}")

    if watches:
        print(f"  🟡 关注: {', '.join(h['code'] for h in watches)}")

    # ── 增强深挖 ──
    deep_needed = [h for h in holdings if h['need_deep']]
    if deep_needed:
        print(f"\n  {'─'*95}")
        print(f"  [增强深挖] {len(deep_needed)}只需关注:")
        for h in deep_needed:
            print(f"\n  ▸ {h['code']} {h['name']} ¥{h['cur']:.2f} | 盈亏{h['pnl_pct']:+.1f}% | 触发: {', '.join(h['deep_reasons'])}")
            sigs = (', '.join(h['old_plus']) if h['old_plus'] else '') + ('; ' if h['old_plus'] and h['old_minus'] else '') + (', '.join(h['old_minus']) if h['old_minus'] else '')
            print(f"    十维: 评分{h['old_score']:+d} | {sigs if sigs else '—'}")
            print(f"    关键位: MA10={h['ma10']:.1f} MA20={h['ma20']:.1f} MA60={h['ma60']:.1f} BOLL下={h['boll_low']:.1f}")
            if h['ma20'] > 0 and h['boll_low'] > 0 and h['cur'] > 0:
                up = (h['ma20']/h['cur']-1)*100; dn = (h['cur']/h['boll_low']-1)*100
                rr = up/dn if dn > 0 else 0
                print(f"    盈亏比: →MA20 +{up:.1f}% / BOLL -{dn:.1f}% = {rr:.1f}:1")
            print(f"    新框架: {h['new_v']} | TOP60: {'池内' if h['in_pool'] else '池外'}")

    # ── 集中度 + 池外 ──
    print(f"\n  {'─'*95}")
    print(f"  ⚠️ 集中度:")
    warned = False
    for h in holdings:
        pct = h['mkt_val']/total_mkt*100 if total_mkt > 0 else 0
        if pct > 15: print(f"  🔴 {h['code']} {h['name']}: {pct:.1f}% (>15%超标)"); warned = True
        elif pct > 10: print(f"  🟡 {h['code']} {h['name']}: {pct:.1f}% (>10%偏重)"); warned = True
    if not warned: print(f"  ✅ 无超标")

    outside = [h for h in holdings if not h['in_pool']]
    if outside:
        print(f"  📤 池外({len(outside)}只): {', '.join(h['code'] for h in outside)}")

    # ── 日亏损告警 ──
    valid_ret_list = [r for r in organic_ret if r is not None]
    if len(valid_ret_list) > 0:
        today_ret = valid_ret_list[-1]
        if today_ret <= -5:
            try:
                from monitoring.alerts import send_alert
                send_alert(f"🔴【日亏损告警】{today}\n日亏损: {today_ret:.1f}%\n持仓市值: ¥{total_mkt:,.0f}\n真实盈亏: ¥{real_pnl:+,.0f}")
            except: pass

    print(f"\n{'='*95}\n")

if __name__ == "__main__":
    cash = float(sys.argv[1]) if len(sys.argv) > 1 else 0
    run(cash)
