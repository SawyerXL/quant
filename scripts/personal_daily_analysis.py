"""
个人持仓每日综合分析 — 十维筛查 + 增强深挖
用法: python scripts/personal_daily_analysis.py
"""
import sys, json, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config.settings import ROOT, LOG_DIR

logger.add(LOG_DIR / "personal_analysis.log", rotation="7 days")
TODAY = str(date.today())

def sf(v):
    try: return float(v)
    except:
        if isinstance(v, str):
            v = v.replace(',','').replace('%','').strip()
            if '亿' in v: return float(v.replace('亿','')) * 1e8
            if '万' in v: return float(v.replace('万','')) * 1e4
        return 0.0

# ══════════════════════════════════
# Part A: 十维快速筛查 (所有持仓)
# ══════════════════════════════════
def ten_dim_screen(holdings, flow_data):
    """十维评分, 返回排序后的结果"""
    from data.storage import load_daily

    results = []
    for code, (name, cost, shares) in holdings.items():
        # 现价
        exch = 'sh' if code.startswith(('6','68')) else 'sz'
        try:
            r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            cur = float(r.text.split('"')[1].split(',')[3])
        except:
            cur = cost

        pnl_pct = (cur/cost - 1) * 100 if cost > 0 else 0
        ft = flow_data.get(code)

        # 技术面
        score = 0; plus = []; minus = []
        try:
            df = load_daily(code, '2026-04-01', TODAY)
            if not df.empty:
                df['dt'] = pd.to_datetime(df['date']); df = df.set_index('dt').sort_index()
                cl = pd.to_numeric(df['close'], errors='coerce').dropna()
                if len(cl) >= 20:
                    ma10 = cl.iloc[-10:].mean(); ma20 = cl.iloc[-20:].mean()
                    ret20 = (cl.iloc[-1]/cl.iloc[-21]-1)*100 if len(cl)>=21 else 0
                    dh = (cl.iloc[-1]/cl.iloc[-20:].max()-1)*100
                    if cur > ma10: score += 3; plus.append('MA10上')
                    else: score -= 3; minus.append('MA10下')
                    if dh < -10: score += 4; plus.append(f'深调{dh:.0f}%')
                    elif dh < -5: score += 3; plus.append(f'回调{dh:.0f}%')
                    if ret20 > 30: score -= 5; minus.append('过热')
                    if ret20 > 50: score -= 3; minus.append('严重过热')
        except: pass

        # 资金面
        if ft and ft > 1: score += 4; plus.append('抢筹')
        elif ft and ft > 0.3: score += 2; plus.append('流入')
        elif ft and ft < -5: score -= 6; minus.append('踩踏')
        elif ft and ft < -1: score -= 2; minus.append('流出')

        # 盈亏
        if pnl_pct > 50 and shares > 0: minus.append(f'浮盈{pnl_pct:.0f}%锁利')
        if pnl_pct < -15: minus.append(f'深亏{pnl_pct:.0f}%')

        results.append({
            'code': code, 'name': name, 'cur': cur, 'cost': cost, 'shares': shares,
            'pnl_pct': pnl_pct, 'score': score, 'plus': plus, 'minus': minus,
            'flow': ft, 'mkt_val': cur * shares,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results

# ══════════════════════════════════
# Part B: 增强深挖 (分数极端或用户指定)
# ══════════════════════════════════
def enhanced_deep_dive(code, name, cost, shares):
    """单只股票深度分析 — 盈亏比+技术面+基本面+分档操作"""
    from data.storage import load_daily
    import akshare as ak

    # 实时行情
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
        cur = float(r.text.split('"')[1].split(',')[3])
    except:
        cur = cost

    # 技术面
    try:
        df = load_daily(code, '2026-01-01', TODAY)
        if df.empty: return None
        df['dt'] = pd.to_datetime(df['date']); df = df.set_index('dt').sort_index()
        cl = pd.to_numeric(df['close'], errors='coerce').dropna()
        if len(cl) < 60: return None

        ma20 = cl.iloc[-20:].mean(); ma60 = cl.iloc[-60:].mean()
        ma120 = cl.iloc[-120:].mean() if len(cl)>=120 else ma60
        boll_mid = ma20; boll_low = ma20 - 2*cl.iloc[-20:].std()
        low60 = cl.iloc[-60:].min()

        # RSI14
        delta = cl.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        rsi14 = 100 - (100 / (1 + gain.rolling(14).mean()/loss.rolling(14).mean()))
    except:
        return None

    # 基本面
    fin_info = None
    try:
        df_f = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
        if df_f is not None and not df_f.empty:
            r = df_f.iloc[-1]
            eps = sf(r.get('基本每股收益', 0))
            bps = sf(r.get('每股净资产', 0))
            fin_info = {
                'pe': cur/eps if eps>0 else 0, 'pb': cur/bps if bps>0 else 0,
                'revenue': sf(r.get('营业总收入', 0)),
                'revenue_yoy': sf(r.get('营业总收入同比增长率', 0)),
                'profit': sf(r.get('净利润', 0)),
                'profit_yoy': sf(r.get('净利润同比增长率', 0)),
                'gross_margin': sf(r.get('销售毛利率', 0)),
                'net_margin': sf(r.get('销售净利率', 0)),
                'roe': sf(r.get('净资产收益率', 0)),
                'debt_ratio': sf(r.get('资产负债率', 0)),
            }
    except: pass

    # 盈亏比
    target1 = ma20; stop1 = boll_low
    upside1 = (target1/cur-1)*100; downside1 = (cur/stop1-1)*100 if stop1>0 else 0
    rr1 = upside1/downside1 if downside1>0 else 0

    target2 = ma60; stop2 = low60*0.98
    upside2 = (target2/cur-1)*100; downside2 = (cur/stop2-1)*100 if stop2>0 else 0
    rr2 = upside2/downside2 if downside2>0 else 0

    # 趋势判定
    if cur > ma60: trend = '多头'; trend_s = 2
    elif cur > ma20: trend = '震荡偏多'; trend_s = 1
    elif cur > boll_low: trend = '弱势震荡'; trend_s = 0
    else: trend = '超卖'; trend_s = -1

    # 操作建议
    if trend_s >= 2:
        action = f"持有为主, 回踩{ma20:.1f}可加仓"
    elif trend_s == 1:
        action = f"持有观察, 止损{stop1:.1f}"
    elif trend_s == 0:
        action = f"试探仓{boll_low:.1f}元({int(20)}-{int(30)}%仓位), 止损{stop2:.0f}"
    else:
        action = f"等右侧: 放量站上{ma20:.0f}再考虑, 不割肉"

    return {
        'cur': cur, 'ma20': ma20, 'ma60': ma60, 'boll_low': boll_low, 'low60': low60,
        'rsi14': rsi14.iloc[-1], 'trend': trend,
        'rr1': rr1, 'upside1': upside1, 'target1': target1, 'stop1': stop1,
        'rr2': rr2, 'upside2': upside2, 'target2': target2, 'stop2': stop2,
        'fin': fin_info, 'action': action,
    }

# ══════════════════════════════════
# Main: 综合输出
# ══════════════════════════════════
def run():
    # Load holdings
    df_h = pd.read_csv('config/my_holdings.csv', dtype={'code': str})
    df_h['code'] = df_h['code'].str.zfill(6)
    held = df_h[df_h['shares'] > 0]
    holdings = {}
    for _, r in held.iterrows():
        holdings[r['code']] = (r['name'], float(r['cost_price']), int(r['shares']))

    # MCP flow (from last pull - approximate)
    flow = {}

    now = datetime.now().strftime('%H:%M')

    # ═══ Part A: 十维筛查 ═══
    print(f"\n{'='*60}")
    print(f"  个人持仓综合分析 {now}")
    print(f"{'='*60}")
    print(f"\n  ── 十维筛查 ──")
    print(f"  {'代码':<8} {'名称':<6} {'现价':>7} {'盈亏':>7} {'评分':>4} {'信号'}")
    print(f"  {'─'*50}")

    screen = ten_dim_screen(holdings, flow)
    actionable = []  # 需要深挖的

    for s in screen:
        flags = []
        if s['score'] >= 5: flags.append('🟢强势'); actionable.append(s)
        elif s['score'] <= -5: flags.append('🔴弱势'); actionable.append(s)
        elif s['pnl_pct'] > 50: flags.append('⚠️浮盈'); actionable.append(s)
        elif s['pnl_pct'] < -20: flags.append('🔴深亏'); actionable.append(s)
        flag_str = ','.join(flags) if flags else '—'
        print(f"  {s['code']:<8} {s['name']:<6} {s['cur']:>7.2f} {s['pnl_pct']:>+6.1f}% {s['score']:>+3d}  {flag_str}")

    # ═══ Part B: 增强深挖 ═══
    if actionable:
        print(f"\n  ── 增强深挖 ({len(actionable)}只需关注) ──")
        for s in actionable[:3]:  # Top 3 only
            d = enhanced_deep_dive(s['code'], s['name'], s['cost'], s['shares'])
            if not d:
                print(f"  {s['code']} {s['name']}: 数据不足")
                continue

            print(f"\n  ▸ {s['code']} {s['name']} ¥{d['cur']:.2f} | 趋势:{d['trend']} | RSI14:{d['rsi14']:.0f}")
            if d['fin']:
                print(f"    PE:{d['fin']['pe']:.0f}x PB:{d['fin']['pb']:.1f}x | 营收:{d['fin']['revenue']/1e8:.1f}亿 YoY{d['fin']['revenue_yoy']:+.0f}%")
            print(f"    盈亏比A(→MA20): {d['rr1']:.1f}:1 | 盈亏比B(→MA60): {d['rr2']:.1f}:1")
            print(f"    关键位: 目标1={d['target1']:.1f} 目标2={d['target2']:.1f} 止损={d['stop1']:.1f}")
            print(f"    建议: {d['action']}")

    # ═══ 汇总 ═══
    total_mkt = sum(s['mkt_val'] for s in screen)
    total_cost = sum(s['cost'] * s['shares'] for s in screen)
    print(f"\n  ── 汇总 ──")
    print(f"  总市值: ¥{total_mkt:,.0f} | 浮亏: ¥{total_mkt-total_cost:+,.0f} ({(total_mkt/total_cost-1)*100:+.1f}%)")
    print(f"{'='*60}\n")

if __name__ == '__main__':
    run()
