"""
09:35 MCP实时资金流确认 — 盘前反转候选验证 + 持仓主力方向
用法: python scripts/mcp_morning_update.py
"""
import sys, os, json, requests, re, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/mcp_morning.log", rotation="3 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")
CACHE_DIR = Path("logs/cache")
SIGNAL_FILE = Path("data_store/meta/signal_a_latest.json")

def sf(v):
    try: return float(v)
    except: return 0.0

def run():
    today_str = date.today().strftime('%Y-%m-%d')
    logger.info(f"MCP早盘确认 {today_str}")

    # Load holdings
    import pandas as pd
    df_h = pd.read_csv(HOLDINGS_FILE, dtype={'code': str})
    df_h['code'] = df_h['code'].str.zfill(6)
    active = df_h[df_h['monitor'] == True]
    codes = active['code'].tolist()

    # Load names
    from data.storage import load_meta
    info = load_meta('stock_info_full')
    name_map = {}
    if not info.empty:
        for _, r in info.iterrows():
            name_map[str(r['code'])] = r.get('name', '')

    # Load signals
    signals = {}
    if SIGNAL_FILE.exists():
        sig = json.loads(SIGNAL_FILE.read_text())
        pool = set(sig.get('holdings', []))
        buy_s = set(sig.get('buy', []))
        for c in pool:
            signals[c] = {'in_buy': c in buy_s, 'in_pool': True}

    # ── Pull today's MCP fund flow ──
    print("拉取MCP实时资金流...")
    from data.source.mcp_source import MCPSource
    mcp = MCPSource()
    flow_today = {}

    # Build extended pull list: holdings + tracking + TOP60 reversal candidates
    tracking_codes = [str(r['code']).zfill(6) for _, r in active.iterrows() if int(r['shares']) == 0]
    top60_candidates = []
    try:
        yesterday_key = (date.today().replace(day=date.today().day-1)).strftime('%Y%m%d')
        t60f = CACHE_DIR / f'top60_flow_{yesterday_key}.json'
        if t60f.exists():
            top60_data = json.loads(t60f.read_text()).get('flow', {})
            held_set = set(codes)
            for code, f in top60_data.items():
                if code in held_set: continue
                main = f.get('main', 0)
                if main < 0 and abs(main) < 5:
                    top60_candidates.append(code)
    except: pass

    all_pull = set(list(codes) + tracking_codes + top60_candidates[:10])
    logger.info(f"MCP拉取: {len(all_pull)}只 (持仓{len(codes)}+跟踪{len(tracking_codes)}+候选{len(top60_candidates[:10])})")
    for code in all_pull:
        try:
            df = mcp.get_capital_flow(code, today_str)
            if df is not None and not df.empty:
                r = df.iloc[0]
                flow_today[code] = {
                    'main': sf(r.get('主力净额(万元)', 0)) / 1e4,
                    'big': sf(r.get('超大单资金净额(万元)', 0)) / 1e4,
                    'large': sf(r.get('大单资金净额(万元)', 0)) / 1e4,
                    'small': sf(r.get('小单资金净额(万元)', 0)) / 1e4,
                }
        except: pass
    logger.info(f"获取到 {len(flow_today)} 只资金流")

    # Load yesterday's flow for comparison
    yesterday = (date.today().replace(day=date.today().day - 1)).strftime('%Y%m%d')
    flow_yest = {}
    yf = CACHE_DIR / f"top60_flow_{yesterday}.json"
    if yf.exists():
        flow_yest = json.loads(yf.read_text()).get('flow', {})

    # Pull real-time prices
    rt = {}
    for code in codes:
        exch = 'sh' if code.startswith('6') else 'sz'
        try:
            r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
                headers={'Referer': 'https://finance.sina.com.cn'}, timeout=3)
            flds = r.text.split('"')[1].split(',')
            rt[code] = {'cur': sf(flds[3]), 'chg': (sf(flds[3]) / sf(flds[2]) - 1) * 100 if sf(flds[2]) > 0 else 0}
        except: pass

    # ── Build report (redesigned for clarity) ──
    lines = []
    lines.append(f"⚡ 主力资金流 {datetime.now().strftime('%m/%d %H:%M')}")
    # Macro
    try:
        for sid, name in [('int_dji','道指'),('s_sh000001','上证'),('s_sz399006','创业板')]:
            r_p = requests.get(f'http://hq.sinajs.cn/list={sid}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            flds = r_p.text.split('"')[1].split(',')
            lines.append(f"  {name}: {float(flds[1]):,.0f} ({float(flds[3]):+.2f}%)")
    except: pass
    lines.append(f"  💡 券商三利好 | 存储提价 | 央行放水 | 偏多")
    lines.append("")

    # 1. 🚨 反转告警 (only show important ones)
    reversals_in = []  # 卖出→买入
    reversals_out = []  # 买入→卖出
    for code in codes:
        ft = flow_today.get(code, {})
        fy_f = flow_yest.get(code, {})
        main_t = ft.get('main', 0); main_y = fy_f.get('main', 0)
        if abs(main_t) < 0.01: continue
        name = name_map.get(code, code)

        if main_y < -1 and main_t > 0.5:
            reversals_in.append((code, name, main_y, main_t))
        elif main_y > 0.5 and main_t < -1:
            reversals_out.append((code, name, main_y, main_t))

    if reversals_in:
        lines.append("🔄 昨出今进 → 买入信号")
        for code, name, y, t in reversals_in:
            rt_i = rt.get(code, {})
            lines.append(f"  {code} {name} ¥{rt_i.get('cur',0):.2f}  昨{y:+.0f}→今{t:+.0f}亿")
        lines.append("")

    if reversals_out:
        lines.append("🔴 昨买今卖 → 减仓信号")
        for code, name, y, t in reversals_out:
            rt_i = rt.get(code, {})
            lines.append(f"  {code} {name} ¥{rt_i.get('cur',0):.2f}  昨{y:+.0f}→今{t:+.0f}亿")
        lines.append("")

    # 2. 📊 主力方向一览 (compact table)
    lines.append("📊 持仓主力方向")
    lines.append(f"  {'代码':<8} {'名称':<8} {'昨':>5} {'今':>6} {'方向':<10}")
    lines.append(f"  {'─'*40}")
    for code in codes:
        ft = flow_today.get(code, {})
        fy_f = flow_yest.get(code, {})
        main_t = ft.get('main', 0); main_y = fy_f.get('main', 0)
        if abs(main_t) < 0.01 and abs(main_y) < 0.01: continue
        name = name_map.get(code, code[:6])
        arrow = '🟢↑' if main_t > 0.5 else ('🔴↓' if main_t < -1 else '→')
        trend = '继续流入' if main_t>0.5 and main_y>0 else ('反转流入!' if main_t>0.5 and main_y<-1 else
                '继续流出' if main_t<-1 and main_y<-1 else ('反转流出!' if main_t<-1 and main_y>0.5 else '—'))
        lines.append(f"  {code:<8} {name:<8} {main_y:>+4.0f} {main_t:>+5.0f} {arrow} {trend:<10}")

    # 3. 🎯 建仓候选确认
    candidates = list(set(tracking_codes + top60_candidates[:10]))
    lines.append(f"\n🎯 外部候选确认")
    found_any = False
    for code in candidates:
        ft = flow_today.get(code, {})
        if abs(ft.get('main', 0)) < 0.01: continue
        main_t = ft.get('main', 0)
        name = name_map.get(code, code)
        rt_i = rt.get(code, {})
        cur = rt_i.get('cur', 0)
        found_any = True
        if main_t > 0.5:
            lines.append(f"  ✅ {code} {name} ¥{cur:.2f} 今{main_t:+.1f}亿 — 可建仓")
        elif main_t > 0:
            lines.append(f"  🟡 {code} {name} ¥{cur:.2f} 今{main_t:+.1f}亿 — 关注")
        else:
            lines.append(f"  ❌ {code} {name} ¥{cur:.2f} 今{main_t:+.1f}亿 — 放弃")
    if not found_any:
        lines.append("  无候选(均被MCP排除)")

    # 4. 💰 操作建议
    lines.append(f"\n💰 操作")
    held = set(active[active['shares'] > 0]['code'].tolist())
    buy_count = 0
    for code in candidates:
        ft = flow_today.get(code, {})
        if ft.get('main', 0) > 0.5 and code not in held:
            name = name_map.get(code, code)
            cur = rt.get(code, {}).get('cur', 0)
            shares = int(10000 / cur / 100) * 100 if cur > 0 else 100
            lines.append(f"  🟢 建仓 {code} {name}: {shares}股≈¥{shares*cur:,.0f}")
            buy_count += 1
    for rv in reversals_in:
        if rv[0] in held:
            lines.append(f"  🟢 加仓 {rv[0]} {rv[1]}: 反转确认")
            buy_count += 1
    if buy_count == 0:
        lines.append(f"  无建仓 — MCP确认前不动")
    lines.append(f"  持有: 紫金(抢筹) 亚威(锁利) 洛阳(持有)")
    lines.append(f"  减仓: 工业富联/大族(踩踏中但减速)")

    body = '\n'.join(lines)
    try:
        from monitoring.alerts import send_alert
        send_alert(body)
        logger.info("MCP确认邮件已发送")
    except Exception as e:
        logger.error(f"邮件失败: {e}")
    print(body)

if __name__ == '__main__':
    run()
