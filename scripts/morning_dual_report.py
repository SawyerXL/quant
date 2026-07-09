"""
早盘双框架报告 — 旧框架(十维) + 新框架(MA10-4d) 并行
取代旧 morning_briefing.py, 融入 2026-07-08 回测优化结论
用法: python scripts/morning_dual_report.py [--send]
      --send: 发邮件, 否则仅打印
"""
import sys, os, json, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
logger.add("logs/morning_dual_report.log", rotation="7 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")

def rt_price(code):
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
        d = r.text.split('"')[1].split(',')
        return float(d[3]) if float(d[3]) > 0 else float(d[2])
    except: return 0

def sf(v):
    try: return float(v)
    except: return 0.0

def run(send_email=False):
    now = datetime.now().strftime('%m/%d %H:%M')
    today_str = date.today().strftime('%Y-%m-%d')

    # Load holdings
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df['code'] = df['code'].str.zfill(6)
    held = df[(df['monitor'] == True) & (df['shares'] > 0)]

    if held.empty:
        print("无持仓"); return

    # Load CSI800 + TOP60
    from data.storage import load_meta, load_daily
    c800 = load_meta('csi800')

    # Turnover ranking
    turnover = {}; prices_recent = {}
    for code in c800['code'].astype(str).str.zfill(6):
        try:
            df_d = load_daily(code, '2026-05-01', today_str)
            if df_d.empty: continue
            df_d['date'] = pd.to_datetime(df_d['date']); df_d = df_d.set_index('date').sort_index()
            amt = pd.to_numeric(df_d.get('amount', pd.Series(dtype=float)), errors='coerce').dropna()
            cl = pd.to_numeric(df_d['close'], errors='coerce').dropna()
            if len(amt) >= 20 and len(cl) >= 40:
                turnover[code] = amt.iloc[-20:].mean()
                prices_recent[code] = cl
        except: pass

    top60 = sorted(turnover.items(), key=lambda x: x[1], reverse=True)[:60]
    top60_codes = set(c for c, _ in top60)
    top60_rank = {c: i+1 for i, (c, _) in enumerate(top60)}

    # ── MCP flow (reference only) ──
    flow_data = {}
    if '09:30' <= datetime.now().strftime('%H:%M') <= '15:00':
        try:
            from data.source.mcp_source import MCPSource
            mcp = MCPSource()
            for _, r in held.iterrows():
                code = r['code']
                try:
                    df_f = mcp.get_capital_flow(code, today_str)
                    if df_f is not None and not df_f.empty:
                        flow_data[code] = sf(df_f.iloc[0].get('主力净额(万元)', 0)) / 1e4
                except: pass
        except: pass

    # ══════════════════════════════════
    # Analyze each holding
    # ══════════════════════════════════
    results = []
    for _, r in held.iterrows():
        code = r['code']; name = r['name']
        cost = float(r['cost_price']); shares = int(r['shares'])
        cur = rt_price(code) or cost
        pnl_pct = (cur/cost - 1) * 100
        mkt_val = cur * shares

        ma10 = 0.0; ma20 = 0.0; ma60 = 0.0
        days_below = 0; dh = 0.0; ret20 = 0.0; rsi = 50.0
        boll_low = 0.0; low60 = 0.0
        old_score = 0; old_plus = []; old_minus = []
        macd_sig = '?'
        in_top60 = code in top60_codes
        turnover_rank = top60_rank.get(code, 0)

        try:
            df_d = load_daily(code, '2026-01-01', today_str)
            if not df_d.empty:
                df_d['dt'] = pd.to_datetime(df_d['date']); df_d = df_d.set_index('dt').sort_index()
                cl = pd.to_numeric(df_d['close'], errors='coerce').dropna()
                if len(cl) >= 60:
                    ma10 = float(cl.iloc[-10:].mean())
                    ma20 = float(cl.iloc[-20:].mean())
                    ma60 = float(cl.iloc[-60:].mean())
                    ret20 = float((cl.iloc[-1]/cl.iloc[-21]-1)*100) if len(cl)>=21 else 0
                    dh = float((cl.iloc[-1]/cl.iloc[-20:].max()-1)*100)
                    boll_low = float(ma20 - 2*cl.iloc[-20:].std())
                    low60 = float(cl.iloc[-60:].min())

                    # Days below MA10
                    for j in range(len(cl)-1, max(0, len(cl)-15), -1):
                        local_ma10 = float(cl.iloc[max(0,j-9):j+1].mean())
                        if cl.iloc[j] < local_ma10: days_below += 1
                        else: break

                    # RSI
                    delta = cl.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
                    rsi_val = 100 - (100/(1 + gain.rolling(14).mean()/loss.rolling(14).mean()))
                    rsi = float(rsi_val.iloc[-1]) if not pd.isna(rsi_val.iloc[-1]) else 50

                    # MACD
                    e12 = cl.ewm(span=12).mean(); e26 = cl.ewm(span=26).mean()
                    dif = e12 - e26; dea = dif.ewm(span=9).mean()
                    mb = float(2*(dif - dea).iloc[-1])
                    macd_sig = '金叉' if mb > 0 and dif.iloc[-1] > dea.iloc[-1] else ('死叉' if mb < 0 else '修复')

                    # ── 旧框架(十维)评分 ──
                    if cur > ma10: old_score += 3; old_plus.append('MA10上')
                    else: old_score -= 3; old_minus.append('MA10下')
                    if dh < -10: old_score += 4; old_plus.append('深调')
                    elif dh < -5: old_score += 3; old_plus.append('回调')
                    elif dh > -3: old_score -= 1; old_minus.append('近高')
                    if ret20 > 30: old_score -= 5; old_minus.append('过热')
                    if rsi < 30: old_score += 3; old_plus.append('超卖')
                    elif rsi > 70: old_score -= 2; old_minus.append('超买')
                    if pnl_pct > 50: old_minus.append('浮盈锁利')
                    if pnl_pct < -15: old_minus.append('深亏')

                    # MCP flow (reference)
                    ft = flow_data.get(code)
                    if ft and ft > 1: old_score += 2; old_plus.append('MCP流入')
                    elif ft and ft < -5: old_score -= 3; old_minus.append('MCP踩踏')
        except: pass

        # ── Old verdict ──
        if old_score >= 5: old_verdict = '🟢 强势'
        elif old_score >= 0: old_verdict = '🟡 中性'
        elif old_score >= -5: old_verdict = '🟠 偏弱'
        else: old_verdict = '🔴 卖出'

        # ── New verdict (MA10-4d + TP) ──
        if pnl_pct >= 60: new_verdict = '🔴 止盈TP2'
        elif pnl_pct >= 30: new_verdict = '🟡 止盈TP1'
        elif days_below >= 4: new_verdict = '🔴 清仓(MA10)'
        elif days_below >= 3: new_verdict = '🟠 预警(明触发)'
        elif days_below >= 2: new_verdict = '🟡 关注'
        else: new_verdict = '🟢 持有'

        # ── Conflict flag ──
        old_bear = old_score < 0
        new_bear = '清仓' in new_verdict or '止盈' in new_verdict
        if old_bear and new_bear: conflict = '✅'
        elif not old_bear and not new_bear: conflict = '✅'
        elif not old_bear and new_bear: conflict = '🔴 旧持新卖'
        else: conflict = '🟡 旧卖新持'

        # ── R/R ──
        rr = 0.0
        if ma20 > 0 and boll_low > 0 and cur > 0:
            up = (ma20/cur - 1)*100
            dn = (cur/boll_low - 1)*100 if boll_low > 0 else 0
            rr = up/dn if dn > 0 else 0

        results.append({
            'code': code, 'name': name, 'cur': cur, 'cost': cost, 'shares': shares,
            'pnl_pct': pnl_pct, 'mkt_val': mkt_val,
            'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
            'days_below': days_below, 'dh': dh, 'ret20': ret20, 'rsi': rsi,
            'boll_low': boll_low, 'low60': low60, 'macd': macd_sig, 'rr': rr,
            'old_score': old_score, 'old_verdict': old_verdict,
            'new_verdict': new_verdict, 'conflict': conflict,
            'in_top60': in_top60, 'rank': turnover_rank,
            'flow': flow_data.get(code, 0),
        })

    # Sort: new framework SELL first, then WATCH, then HOLD
    severity = {'🔴 清仓(MA10)': 0, '🔴 止盈TP2': 1, '🟡 止盈TP1': 2,
                '🟠 预警(明触发)': 3, '🟡 关注': 4, '🟢 持有': 5}
    results.sort(key=lambda x: severity.get(x['new_verdict'], 99))

    # ══════════════════════════════════
    # Build report
    # ══════════════════════════════════
    total_mkt = sum(r['mkt_val'] for r in results)
    total_cost = sum(r['cost']*r['shares'] for r in results)
    new_sells = [r for r in results if '清仓' in r['new_verdict'] or '止盈' in r['new_verdict']]
    conflicts = [r for r in results if '🔴' in r['conflict']]
    in_pool = [r for r in results if r['in_top60']]
    outside_pool = [r for r in results if not r['in_top60']]

    lines = []
    lines.append(f"个人持仓早报 {now}")
    lines.append("")
    lines.append(f"  总市值: ¥{total_mkt:,.0f} | 总盈亏: ¥{total_mkt-total_cost:+,.0f} ({(total_mkt/total_cost-1)*100:+.1f}%)")
    lines.append(f"  新框架触发: {len(new_sells)}只需操作 | 双框架冲突: {len(conflicts)}只")
    lines.append(f"  在TOP60池: {len(in_pool)}只 | 池外: {len(outside_pool)}只")
    lines.append("")

    # Main table
    lines.append(f"  {'代码':<8} {'名称':<6} {'现价':>7} {'盈亏':>7} {'MA10':>7} {'破':>2s} {'距高':>5} {'RSI':>3} {'MACD':>4} {'盈亏比':>5} {'MCP':>6} {'旧框架(十维)':12s} {'新框架(MA10)':14s} {'冲突'}")
    lines.append(f"  {'─'*110}")

    for r in results:
        ft_str = f'{r["flow"]:+.1f}' if r['flow'] else '—'
        pool_tag = f'#{r["rank"]}' if r['in_top60'] else '池外'
        lines.append(f"  {r['code']:<8} {r['name']:<6} {r['cur']:>7.2f} {r['pnl_pct']:>+6.1f}% {r['ma10']:>7.2f} {r['days_below']:>2d} {r['dh']:>+4.0f}% {r['rsi']:>3.0f} {r['macd']:>4} {r['rr']:>4.1f} {ft_str:>6} {r['old_verdict']:12s} {r['new_verdict']:14s}  {r['conflict']} {pool_tag}")

    # MA10-4d action items
    lines.append("")
    lines.append("━" * 50)
    lines.append("  [新框架] MA10-4d 操作清单")
    for r in new_sells:
        if '清仓' in r['new_verdict']:
            lines.append(f"    🔴 卖出 {r['code']} {r['name']}: {r['shares']}股 ≈ ¥{r['mkt_val']:,.0f} (MA10下{r['days_below']}d, 盈亏{r['pnl_pct']:+.1f}%)")
        elif '止盈' in r['new_verdict']:
            sell_frac = 2/3 if 'TP2' in r['new_verdict'] else 1/3
            sell_n = int(r['shares'] * sell_frac)
            lines.append(f"    🔴 止盈 {r['code']} {r['name']}: 卖{sell_n}股 ≈ ¥{sell_n*r['cur']:,.0f} (盈亏{r['pnl_pct']:+.0f}%, 留{int(r['shares']*(1-sell_frac))}股)")

    warn_list = [r for r in results if '预警' in r['new_verdict']]
    if warn_list:
        lines.append(f"    🟠 预警: {', '.join(r['code']+'('+str(r['days_below'])+'d)' for r in warn_list)}")

    # Conflicts
    if conflicts:
        lines.append("")
        lines.append("━" * 50)
        lines.append(f"  [冲突] {len(conflicts)}只 — 旧框架说持有但新框架说卖出")
        lines.append(f"  建议: 按新框架执行(回测年化+9.3% vs 旧+5.7%)")
        for r in conflicts:
            lines.append(f"    {r['code']} {r['name']}: 旧={r['old_verdict']}(评分{r['old_score']:+d}) vs 新={r['new_verdict']}")

    # Outside TOP60
    if outside_pool:
        lines.append("")
        lines.append("━" * 50)
        lines.append(f"  [池外] {len(outside_pool)}只不在TOP60 — 新策略建议逐步换入池内")
        for r in outside_pool:
            v = r['new_verdict']
            lines.append(f"    {r['code']} {r['name']} | 盈亏{r['pnl_pct']:+.1f}% | 新框架: {v}")

    # MCP note
    lines.append("")
    lines.append("━" * 50)
    lines.append(f"  [MCP资金流] 仅供参考, 不决定买卖方向")
    lines.append(f"  MA10定方向, MCP仅影响执行时机(开盘vs尾盘)")

    report = '\n'.join(lines)
    print(report)

    # Send email
    if send_email:
        try:
            from monitoring.alerts import send_alert
            send_alert(report)
            logger.info("双框架报告已发送")
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            print(f"\n  ⚠️ 邮件发送失败: {e}")

if __name__ == '__main__':
    send = '--send' in sys.argv
    run(send_email=send)
