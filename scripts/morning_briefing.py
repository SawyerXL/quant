"""
早盘简报 — MCP资金流 + 增强分析结论 + 止损扫描
取代旧的 mcp_holdings_fast.py, 整合今天优化成果
用法: python scripts/morning_briefing.py
"""
import sys, os, json, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
logger.add("logs/morning_briefing.log", rotation="7 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")

def sf(v):
    try: return float(v)
    except: return 0.0

def rt_price(code):
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
        d = r.text.split('"')[1].split(',')
        return float(d[3]) if float(d[3]) > 0 else float(d[2])
    except: return 0

def get_quick_verdict(code, cur, cost=None):
    """快速版增强结论: 趋势+盈亏比+止损, 不调用财务API(保持速度)"""
    from data.storage import load_daily
    try:
        df = load_daily(code, '2026-03-01', '2026-07-08')
        if df.empty: return None, None, None, None
        df['dt'] = pd.to_datetime(df['date']); df = df.set_index('dt').sort_index()
        cl = pd.to_numeric(df['close'], errors='coerce').dropna()
        if len(cl) < 60: return None, None, None, None

        ma20 = cl.iloc[-20:].mean(); ma60 = cl.iloc[-60:].mean()
        boll_low = ma20 - 2*cl.iloc[-20:].std()
        low60 = cl.iloc[-60:].min()

        # Trend
        if cur > ma60: trend, trend_s = '🟢多头', 2
        elif cur > ma20: trend, trend_s = '🟡震荡', 1
        elif cur > boll_low: trend, trend_s = '🟠弱势', 0
        else: trend, trend_s = '🔴超卖', -1

        # R/R
        up = (ma60/cur - 1)*100
        dn = (cur/(low60*0.98) - 1)*100 if low60 > 0 else 0
        rr = up/dn if dn > 0 else 0

        # Stop-loss
        stop_triggered = cur < boll_low
        if cost:
            pnl = (cur/cost - 1)*100
            hard_stop = pnl < -15
        else:
            pnl = 0; hard_stop = False

        # Score
        score = trend_s * 15 + min(rr*10, 30) - (20 if stop_triggered else 0) - (10 if hard_stop else 0) + 20
        if score >= 60: verdict = '🟢持有'
        elif score >= 40: verdict = '🟢观察'
        elif score >= 20: verdict = '🟡谨慎'
        else: verdict = '🔴关注'

        return verdict, trend, stop_triggered or hard_stop, pnl
    except:
        return None, None, None, None

def run():
    today_str = date.today().strftime('%Y-%m-%d')
    now = datetime.now().strftime('%H:%M')

    # Load holdings
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df['code'] = df['code'].str.zfill(6)
    held = df[(df['monitor'] == True) & (df['shares'] > 0)]

    if held.empty:
        logger.warning("无持仓数据")
        return

    # MCP flow (only if market open)
    flow_today = {}
    if '09:30' <= now <= '15:00':
        try:
            from data.source.mcp_source import MCPSource
            mcp = MCPSource()
            for _, r in held.iterrows():
                code = r['code']
                try:
                    df_f = mcp.get_capital_flow(code, today_str)
                    if df_f is not None and not df_f.empty:
                        flow_today[code] = sf(df_f.iloc[0].get('主力净额(万元)', 0)) / 1e4
                except: pass
        except: pass

    # Build report
    lines = []
    lines.append(f"早盘简报 {now}")
    lines.append("")

    # Market
    try:
        for sid, name in [('s_sh000001','上证'),('s_sz399006','创业板')]:
            r_p = requests.get(f'http://hq.sinajs.cn/list={sid}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            flds = r_p.text.split('"')[1].split(',')
            if float(flds[2]) > 0:
                chg = (float(flds[3])/float(flds[2])-1)*100
                lines.append(f"  {name}: {float(flds[3]):,.0f} ({chg:+.2f}%)")
    except: pass
    lines.append("")

    # Holdings table with verdict
    lines.append(f"  {'代码':<8} {'名称':<6} {'现价':>7} {'盈亏':>7} {'主力':>7} {'趋势':<6} {'判定':<8}")
    lines.append(f"  {'─'*55}")

    alerts = []
    verdict_counts = {}

    for _, r in held.iterrows():
        code = r['code']; name = r['name']
        cost = float(r['cost_price']); shares = int(r['shares'])
        cur = rt_price(code)
        pnl = (cur/cost - 1)*100 if cost > 0 else 0
        ft = flow_today.get(code)

        ft_str = f'{ft:+.1f}亿' if ft else '—'
        verdict, trend, stopped, _ = get_quick_verdict(code, cur, cost)

        if verdict is None:
            verdict = '—'; trend = '—'

        lines.append(f"  {code:<8} {name:<6} {cur:>7.2f} {pnl:>+6.1f}% {ft_str:>7} {trend:<6} {verdict:<8}")

        if stopped:
            alerts.append(f"  🔴 {code} {name}: 触发止损 (现价{cur:.2f})")

        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    # Summary
    lines.append("")
    lines.append(f"  判定分布: {', '.join(f'{k}×{v}' for k,v in sorted(verdict_counts.items()))}")

    if alerts:
        lines.append(f"\n  ⚠️ 止损提醒:")
        lines.extend(alerts)

    # Portfolio totals
    total_cost = sum(float(r['cost_price'])*int(r['shares']) for _,r in held.iterrows())
    total_mkt = sum(rt_price(r['code'])*int(r['shares']) for _,r in held.iterrows())
    lines.append(f"\n  总市值: ¥{total_mkt:,.0f} | 盈亏: {(total_mkt/total_cost-1)*100:+.1f}%")

    body = '\n'.join(lines)

    try:
        from monitoring.alerts import send_alert
        send_alert(body)
        logger.info("早盘简报已发送")
    except Exception as e:
        logger.error(f"邮件失败: {e}")

    print(body)

if __name__ == '__main__':
    run()
