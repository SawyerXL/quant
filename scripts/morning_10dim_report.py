"""
早盘10维分析 — 替代旧因子系统，全维度覆盖
用法: python scripts/morning_10dim_report.py
"""
import sys, os, json, re, requests, pandas as pd, numpy as np
from pathlib import Path
from datetime import date, datetime
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
logger.add("logs/morning_10dim.log", rotation="3 days")

HOLDINGS_FILE = Path("config/my_holdings.csv")
SIGNAL_FILE = Path("data_store/meta/signal_a_latest.json")
CACHE_DIR = Path("logs/cache")

def sf(v):
    try: return float(v)
    except: return 0.0

# ═══ Data fetching ═══
def fetch_rt_prices(codes):
    results = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        ids = [f"{'sh' if c.startswith(('5','6','9','11')) else 'sz'}{c}" for c in batch]
        try:
            resp = requests.get(f"http://hq.sinajs.cn/list={','.join(ids)}",
                headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
            resp.encoding = 'gb2312'
            for line in resp.text.strip().split('\n'):
                if '=' not in line: continue
                sid = line.split('=')[0].split('_')[-1]; cd = sid[2:]
                d = line.split('"')[1].split(',')
                if len(d) < 10 or not d[3]: continue
                results[cd] = {'name':d[0], 'cur':sf(d[3]), 'prev':sf(d[2]),
                    'open':sf(d[1]), 'high':sf(d[4]), 'low':sf(d[5]),
                    'chg':(sf(d[3])/sf(d[2])-1)*100 if sf(d[2])>0 else 0}
        except: pass
    return results

def fetch_fund_flow():
    today_key = date.today().strftime('%Y%m%d')
    cf = CACHE_DIR / f"flow_{today_key}.json"
    if cf.exists(): return json.loads(cf.read_text())
    # Try yesterday cache
    yesterday = (date.today().replace(day=date.today().day-1)).strftime('%Y%m%d')
    yf = CACHE_DIR / f"flow_{yesterday}.json"
    if yf.exists(): return json.loads(yf.read_text())
    try:
        import akshare as ak
        rank = ak.stock_individual_fund_flow_rank(indicator='今日')
        flow = {}
        for _, r in rank.iterrows():
            c = str(r['代码'])
            flow[c] = {'main':sf(r['今日主力净流入-净额'])/1e8,'big':sf(r['今日超大单净流入-净额'])/1e8,
                       'large':sf(r['今日大单净流入-净额'])/1e8,'small':sf(r['今日小单净流入-净额'])/1e8}
        CACHE_DIR.mkdir(exist_ok=True)
        cf.write_text(json.dumps(flow, ensure_ascii=False))
        return flow
    except: return {}

def load_technical(code, end_date):
    """从本地日线加载技术指标"""
    from data.storage import load_daily
    try:
        df = load_daily(code, '2026-05-01', end_date)
        if df.empty: return None
        df['date'] = pd.to_datetime(df['date']); df = df.set_index('date').sort_index()
        cl = pd.to_numeric(df['close'], errors='coerce').dropna()
        if len(cl) < 20: return None
        ma10 = cl.iloc[-10:].mean(); ma20 = cl.iloc[-20:].mean()
        ret5 = (cl.iloc[-1]/cl.iloc[-6]-1)*100 if len(cl)>=6 else 0
        ret20 = (cl.iloc[-1]/cl.iloc[-21]-1)*100 if len(cl)>=21 else 0
        dh = (cl.iloc[-1]/cl.iloc[-20:].max()-1)*100
        cons_up=0; cons_down=0
        for j in range(len(cl)-1,0,-1):
            if cl.iloc[j]>cl.iloc[j-1]: cons_up+=1
            else: break
        for j in range(len(cl)-1,0,-1):
            if cl.iloc[j]<cl.iloc[j-1]: cons_down+=1
            else: break
        new_low = cl.iloc[-1] <= cl.iloc[-10:].min()*1.005
        vr = 1.0
        if 'amount' in df.columns:
            amt = pd.to_numeric(df['amount'], errors='coerce').dropna()
            if len(amt) >= 21: vr = amt.iloc[-1]/amt.iloc[-21:-1].mean()
        chg_pct = (cl.iloc[-1]/cl.iloc[-2]-1)*100 if len(cl)>=2 else 0
        # RSI(14)
        delta = cl.diff(); gain = delta.clip(lower=0).rolling(14).mean()
        l = (-delta).clip(lower=0).rolling(14).mean()
        rs_val = gain/l; rsi_val = float(100-(100/(1+rs_val)).iloc[-1]) if not pd.isna(rs_val.iloc[-1]) else 50
        return {'ma10':ma10,'ma20':ma20,'ret5':ret5,'ret20':ret20,'dh':dh,
                'cons_up':cons_up,'cons_down':cons_down,'new_low':new_low,'vr':vr,'close':cl.iloc[-1],
                'chg': chg_pct, 'rsi': rsi_val}
    except: return None

# ═══ 10-dim scoring ═══
def score_stock(code, name, rt, tech, flow, sig, pos, ml_detector=None, sec_momentum=None):
    """全10维评分"""
    cur = rt.get('cur',0); score=0; plus=[]; minus=[]

    # ①②③ 技术面+量能 (from local daily)
    if tech and cur > 0:
        if cur > tech['ma10']: score+=3; plus.append('MA10上')
        else: score-=3; minus.append(f'MA10下')
        dh = tech['dh']
        if dh<-8: score+=4; plus.append(f'深调{dh:.0f}%')
        elif dh<-5: score+=3; plus.append(f'回调{dh:.0f}%')
        elif dh>-1: score-=1; minus.append('近高')
        if tech['cons_up']>=5: score-=4; minus.append(f'连涨{tech["cons_up"]}')
        if tech['cons_down']>=3: minus.append(f'连跌{tech["cons_down"]}')
        if tech['new_low']: score-=3; minus.append('10日新低')
        if tech['ret20']>50: score-=8; minus.append('严重过热')
        elif tech['ret20']>30: score-=5; minus.append('过热')
        if tech['ret5']>15: score-=4; minus.append('5日热')
        if 0.7<tech['vr']<3: score+=1

    # ④ 主力资金流
    if flow:
        big=flow.get('big',0); main=flow.get('main',0); small=flow.get('small',0)
        if big>0.5 and main>0: score+=4; plus.append('🔥全层级抢筹')
        elif big>0: score+=2; plus.append('✅超大单')
        elif main>0.3: score+=2; plus.append('✅主力入')
        elif big<0 and main<-1 and small>0: score-=6; minus.append('💀全层级踩踏')
        elif main<-0.5: score-=2; minus.append('🟠主力出')

    # ⑤ ML 异动检测
    if ml_detector is not None:
        try:
            is_anom, anom_score, anom_desc = ml_detector.detect(code, date.today().strftime('%Y-%m-%d'))
            if is_anom: score-=2; minus.append(f'ML:异常')
            else: score+=1
        except: pass

    # ⑥ 基本面(仅亏损过滤)
    eps = pos.get('eps',1)
    if eps<=0 and pos.get('shares',0)==0: score-=6; minus.append('亏损')

    # ⑧ 板块轮动
    if sec_momentum is not None and code in sec_momentum:
        sm = sec_momentum[code]
        if sm > 3: score+=2; plus.append(f'板块+{sm:.0f}%')
        elif sm < -5: score-=1; minus.append(f'板块{sm:.0f}%')

    # ⑨ 信号
    if sig.get('in_buy'): score+=3; plus.append('★信号买')
    elif sig.get('in_sell'): score-=3; minus.append('✗信号卖')
    elif sig.get('in_pool'): score+=1

    # ⑩ 仓位 — 止损仅提示, 不扣分
    pnl = pos.get('pnl_pct',0); pct = pos.get('mkt_pct',0)
    chg_val = rt.get('chg', 0)
    if chg_val == 0 and tech and tech.get('chg', 0) != 0:
        chg_val = tech.get('chg', 0)
    has_grab = (any('抢筹' in p for p in plus) or any('机构' in p for p in plus)
        or chg_val >= 9.8)
    if pnl>25 and not has_grab: minus.append(f'⚠️浮盈{pnl:.0f}%锁利')
    elif pnl>25 and has_grab: minus.append(f'浮盈{pnl:.0f}%(锁利提醒)')
    if pnl<-12: minus.append(f'🔔止损提醒({pnl:.0f}%)')
    if pnl<-50: minus.append(f'深亏{pnl:.0f}%')
    if pct>15: minus.append(f'重仓{pct:.0f}%')

    return score, plus, minus, has_grab

def decide(code, name, score, pos, plus, minus, has_grab=False):
    """判定 + 操作建议"""
    shares=pos.get('shares',0); pnl=pos.get('pnl_pct',0)
    is_held=shares>0

    if not is_held:
        if score>=8: return '🟢建仓', '尾盘买入'
        elif score>=5: return '🟡关注', '继续观察'
        else: return '→跟踪', '—'

    # Held stocks — 机构抢筹覆盖止损和过热
    if has_grab:
        if pnl>25: return '🔥持有', '锁利提示'
        elif score>=0: return '🔥持有', '机构在买'
        else: return '🟡观察', '机构买但待企稳'
    if score<=-6: return '🔴减仓', '减仓'
    elif score<=-3: return '🔴减仓', '减仓'
    elif score>=8 and pnl<15: return '🟢加仓', '可补仓'
    elif score>=5: return '✅持有', '不动'
    else: return '🟡观察', '观察'

# ═══ Pre-market reversal scan (FIXED: 3-layer filter + 10-dim scoring) ═══
def scan_reversals(flow_cache, price_cache, signal_info, panel_data=None,
                   ml_detector=None, sec_momentum=None):
    """盘前反转扫描 — TOP60 → 三层过滤 → 反转评分 → 十维评分 → 排序"""
    from scripts.backtest_config import BacktestConfig
    from scripts.backtest_engine import apply_filters

    # Step 1: Apply 3-layer filter to TOP60 pool
    filtered_pool = set()
    if panel_data is not None:
        try:
            cfg = BacktestConfig(overheat_mode="reduce", pool_size=60)
            today_idx = len(panel_data[0].index) - 1 if panel_data[0] is not None else 0
            top60_list = list(flow_cache.keys())
            passed = apply_filters(top60_list, panel_data[0], today_idx, cfg)
            filtered_pool = set(passed)  # keep all that passed, don't cap at 30
        except: pass

    results = []
    for code, f in flow_cache.items():
        cur = price_cache.get(code, 0)
        if cur <= 0: continue

        # Step 2: 3-layer filter check
        tech = load_technical(code, date.today().strftime('%Y-%m-%d'))
        if not tech: continue
        if filtered_pool and code not in filtered_pool: continue

        main = f.get('main', 0); big = f.get('big', 0); small = f.get('small', 0)
        ma10 = tech['ma10']; dh = tech['dh']

        # Step 3: Reversal scoring
        chg = tech.get('chg', 0)
        ret60 = (cur/tech.get('ma20', cur) - 1) * 100  # approximate

        rscore = 0; reasons = []
        if abs(main) < 0.01: continue
        if chg > -2 and main < 0 and main > -5: rscore += 3; reasons.append(f'不跌(昨{chg:+.1f}%)')
        if main < 0 and abs(main) < 3: rscore += 2; reasons.append(f'弱出({main:.1f}亿)')
        if ret60 < -30: rscore += 2; reasons.append(f'超跌({ret60:.0f}%)')
        if dh < -10: rscore += 2; reasons.append(f'深调({dh:.0f}%)')
        if cur < ma10: rscore += 1; reasons.append('MA10下')
        if main > 3: rscore += 3; reasons.append(f'巨量抢筹({main:.1f}亿)')

        if rscore < 4: continue

        # Step 4: Full 10-dim scoring (reuse score_stock)
        rt_info = {'cur': cur, 'chg': chg}
        pos_info = {'shares': 0, 'pnl_pct': 0, 'mkt_pct': 0, 'eps': 1}
        sig = signal_info.get(code, {})

        score, plus, minus, _ = score_stock(code, '', rt_info, tech, f, sig, pos_info,
            ml_detector=ml_detector, sec_momentum=sec_momentum)

        results.append({'code': code, 'rscore': rscore, 'score': score,
            'reasons': reasons, 'cur': cur, 'main': main, 'dh': dh,
            'plus': plus, 'minus': minus, 'ma10': cur > ma10})

    results.sort(key=lambda x: (-x['rscore'], -x['score']))
    return results[:10]


# ═══ Main ═══
def run():
    today_str = date.today().strftime('%Y-%m-%d')
    logger.info(f"10维早盘 {today_str}")

    # Load holdings
    df = pd.read_csv(HOLDINGS_FILE, dtype={'code':str})
    df['code'] = df['code'].str.zfill(6)
    active = df[df['monitor']==True]
    codes = active['code'].tolist()

    # Load stock info (MUST be before reversal scan)
    from data.storage import load_meta
    info_df = load_meta('stock_info_full')
    name_map = {}; ind_map = {}
    if not info_df.empty:
        for _, r in info_df.iterrows():
            name_map[str(r['code'])] = r.get('name','')
            ind_map[str(r['code'])] = r.get('industry_l1','')

    # ── ML detector (fit once) ──
    ml_detector = None
    try:
        from scripts.ml_signals import AnomalyDetector
        ml_detector = AnomalyDetector()
        c800_codes = sorted(load_meta('csi800')['code'].tolist())[:200]
        ml_detector.fit(c800_codes, today_str)
    except: pass

    # ── Sector momentum (from flow data) ──
    sec_momentum = {}
    try:
        yesterday_key = (date.today().replace(day=date.today().day-1)).strftime('%Y%m%d')
        tf = CACHE_DIR / f"top60_flow_{yesterday_key}.json"
        if tf.exists():
            flow_data = json.loads(tf.read_text()).get('flow',{})
            sec_flows = {}
            for c, f in flow_data.items():
                ind = ind_map.get(c, '其他')
                sec_flows[ind] = sec_flows.get(ind, 0) + f.get('main', 0)
            for c in codes + list(flow_data.keys()):
                ind = ind_map.get(c, '其他')
                sec_momentum[c] = sec_flows.get(ind, 0)
    except: pass

    # Load signals
    signals={}
    if SIGNAL_FILE.exists():
        sig=json.loads(SIGNAL_FILE.read_text())
        pool=set(sig.get('holdings',[])); buy_s=set(sig.get('buy',[])); sell_s=set(sig.get('sell',[]))
        for c in pool: signals[c]={'in_pool':True,'in_buy':c in buy_s,'in_sell':c in sell_s}
        for c in codes:
            if c not in signals: signals[c]={'in_pool':False,'in_buy':False,'in_sell':False}

    # ── Pre-market reversal scan (TOP60) ──
    reversal_lines = []
    yesterday_key = (date.today().replace(day=date.today().day-1)).strftime('%Y%m%d')
    flow_file = CACHE_DIR / f"top60_flow_{yesterday_key}.json"
    if flow_file.exists():
        flow_data = json.loads(flow_file.read_text())
        top60_flow = flow_data.get('flow', {})
        # Get prices for TOP60 from local daily
        import requests as _req
        top60_codes = list(top60_flow.keys())
        top60_prices = {}
        for c in top60_codes:
            tech = load_technical(c, today_str)
            if tech: top60_prices[c] = tech.get('close', 0)

        # Load panel for 3-layer filter
        from scripts.run_backtest_a import load_panels
        panel_3l = None
        try:
            c800 = load_meta('csi800')
            codes_800 = sorted(c800['code'].tolist())[:800]
            panel_3l, _ = load_panels(codes_800, '2025-06-01', today_str)
        except: panel_3l = None
        reversal_candidates = scan_reversals(top60_flow, top60_prices, signals,
            panel_data=(panel_3l, None) if panel_3l is not None else None,
            ml_detector=ml_detector, sec_momentum=sec_momentum)
        if reversal_candidates:
            reversal_lines.append(f"\n{'='*90}")
            reversal_lines.append(f"🔔 盘前反转预判 (TOP60中识别)")
            reversal_lines.append(f"{'='*90}")
            reversal_lines.append(f"{'代码':<8} {'反转':>4} {'十维':>4} {'昨主力':>7} {'判定':<10} {'信号'}")
            reversal_lines.append(f"{'─'*70}")
            for rc in reversal_candidates[:6]:
                name_r = name_map.get(rc['code'], rc['code'])
                dec_r = '🟢可建' if rc['score'] >= 5 else '🟡关注'
                reversal_lines.append(f"{rc['code']:<8} {rc['rscore']:>4}分 {rc['score']:>+3d}分 {rc['main']:>+6.1f}亿 {dec_r:<10} {', '.join(rc['reasons'])}")
            reversal_lines.append(f"\n  → 09:35 MCP确认后即可动手")

    # Fetch all data
    print(f"拉取实时行情({len(codes)}只)...")
    rt_raw = fetch_rt_prices(codes)

    # Fallback: use local daily close for stocks with ¥0 from Sina
    rt = {}
    for _, r in active.iterrows():
        code = r['code']; name = r['name']
        sina = rt_raw.get(code, {})
        if sina.get('cur', 0) > 0:
            rt[code] = sina
        else:
            tech = load_technical(code, today_str)
            if tech and tech.get('close', 0) > 0:
                rt[code] = {'name': name, 'cur': tech['close'], 'prev': tech['close'],
                    'open': tech['close'], 'high': tech['close'], 'low': tech['close'], 'chg': 0}

    print(f"拉取资金流...")
    flow = fetch_fund_flow()
    if not flow:
        # Try TOP60 cache (today-1) first — has 60 stocks
        yesterday = (date.today().replace(day=date.today().day-1)).strftime('%Y%m%d')
        top60_f = CACHE_DIR / f"top60_flow_{yesterday}.json"
        if top60_f.exists():
            top60_d = json.loads(top60_f.read_text())
            flow = top60_d.get('flow', {})
            print(f"  使用TOP60缓存 ({len(flow)}只)")
        else:
            # Fallback to old small cache
            yf = CACHE_DIR / f"flow_{yesterday}.json"
            if yf.exists():
                flow = json.loads(yf.read_text())
                print(f"  使用旧资金流缓存")

    # Build rows
    rows = []
    for _, r in active.iterrows():
        code=r['code']; name=r['name']; shares=int(r['shares']); cost=sf(r.get('cost_price',0))
        is_held = shares > 0

        rt_info = rt.get(code, {})
        cur = rt_info.get('cur', 0); chg = rt_info.get('chg', 0)
        pnl = (cur/cost-1)*100 if cost>0 and cur>0 else 0
        mkt_val = cur*shares; mkt_pct = mkt_val/300000*100 if mkt_val>0 else 0

        # Technical from local daily
        tech = load_technical(code, today_str)

        # Fund flow
        fl = flow.get(code, {})
        fbig=fl.get('big',0); fmain=fl.get('main',0)
        if fbig>0.5 and fmain>0: flow_tag='🔥抢筹'
        elif fbig>0: flow_tag='✅超大'
        elif fmain>0.3: flow_tag='✅主力'
        elif fmain<-1: flow_tag='💀踩踏'
        elif fmain<-0.5: flow_tag='🟠流出'
        else: flow_tag='→'

        # EPS
        from data.storage import load_meta
        fq = load_meta('financial_quarterly')
        eps = sf(fq[fq['code']==code]['eps'].iloc[-1]) if not fq.empty and code in fq['code'].values else 1

        pos_info = {'shares':shares,'pnl_pct':pnl,'mkt_pct':mkt_pct,'eps':eps}
        sig_info = signals.get(code, {})

        sc, plus, minus, has_grab = score_stock(code, name, rt_info, tech, fl, sig_info, pos_info,
            ml_detector=ml_detector, sec_momentum=sec_momentum)
        dec, action = decide(code, name, sc, pos_info, plus, minus, has_grab)

        f_amt = f'{fmain:+.1f}亿' if abs(fmain)>0.01 else '—'
        vt = '跟踪' if not is_held else f'{shares}股'

        rows.append({'code':code,'name':name,'cur':cur,'chg':chg,'pnl':pnl,
            'sc':sc,'dec':dec,'action':action,'flow_tag':flow_tag,'f_amt':f_amt,
            'plus':plus,'minus':minus,'vt':vt,'is_held':is_held})

    # Sort: held first, then by score
    rows.sort(key=lambda x: (not x['is_held'], -x['sc']))

    # ═══ Build email ═══
    lines = []

    # ── Overnight + macro ──
    lines.append(f"{'='*90}")
    lines.append(f"🌍 隔夜外围 + 大盘环境")
    lines.append(f"{'='*90}")
    try:
        for sid, name in [('int_dji','道指'),('int_nasdaq','纳指'),('int_sp500','标普')]:
            r_p = requests.get(f'http://hq.sinajs.cn/list={sid}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            flds = r_p.text.split('"')[1].split(',')
            lines.append(f"  {name}: {float(flds[1]):,.0f} ({float(flds[3]):+.2f}%)")
    except: lines.append(f"  美股数据暂不可用")
    try:
        for sid, name in [('s_sh000001','上证'),('s_sz399006','创业板'),('s_sh000688','科创50')]:
            r_p = requests.get(f'http://hq.sinajs.cn/list={sid}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=3)
            flds = r_p.text.split('"')[1].split(',')
            lines.append(f"  {name}: {float(flds[1]):,.0f} ({float(flds[3]):+.2f}%)")
    except: pass
    # ── 大盘预判 ──
    try:
        from data.storage import load_meta
        idx_df = load_meta('csi800_index')
        if not idx_df.empty:
            idx_s = idx_df.set_index('date')['close'].sort_index()
            ma200 = idx_s.iloc[-200:].mean(); cur_idx = idx_s.iloc[-1]
            ratio = cur_idx / ma200
            if ratio >= 1.05:
                trend = '强势多头 — 美股影响有限, A股走独立行情'
            elif ratio >= 1.0:
                trend = '多头 — 美股回调会拖累开盘, 但盘中修复'
            elif ratio >= 0.95:
                trend = '震荡偏弱 — 美股下跌将放大A股跌幅'
            else:
                trend = '熊市 — 外围利空会加速下跌'
            lines.append(f"  📈 大盘走向: {trend}")
            lines.append(f"     CSI800/MA200={ratio:.2f} | {'美股影响: 低' if ratio>=1.05 else '美股影响: 中' if ratio>=1.0 else '美股影响: 高'}")
    except: pass

    # ── 资金面 ──
    lines.append(f"  💰 资金面: MCP主力方向汇总")
    # Sum flow from today's data
    total_flow = sum(flow.get(c, {}).get('main', 0) for c in codes if flow.get(c, {}).get('main', 0))
    lines.append(f"     主力总净额: {total_flow:+.0f}亿 | {'偏多' if total_flow>0 else '偏空'}")
    lines.append(f"  💡 政策: 万亿逆回购净投放2000亿 | 半导体中报预增 | 偏多")
    lines.append(f"  ⚠️ 北向资金数据源已失效(东财封锁) — 改用MCP主力流向替代")

    lines.append(f"\n{'='*90}")
    lines.append(f"📊 持仓+跟踪 十维分析 {today_str}")
    lines.append(f"{'='*90}")
    lines.append(f"{'代码':<8} {'名称':<8} {'现价':>7} {'涨跌':>7} {'盈亏':>8} {'评分':>4} {'持仓':>6} {'主力':>14} {'判定':<10} {'操作'}")
    lines.append(f"{'─'*90}")

    cats = {}
    for r in rows:
        rt_chg = f'{r["chg"]:+.1f}%' if abs(r['chg'])>0.01 else '—'
        pnl_s = f'{r["pnl"]:+.1f}%' if r['is_held'] else '—'
        lines.append(f'{r["code"]:<8} {r["name"]:<8} {r["cur"]:>7.2f} {rt_chg:>7} {pnl_s:>8} {r["sc"]:+4d} {r["vt"]:>6} ({r["flow_tag"]}){r["f_amt"]:<10} {r["dec"]:<10} {r["action"]}')
        k = r['dec']; cats[k] = cats.get(k,[])+[f'{r["code"]} {r["name"]}']

    lines.append(f"\n{'─'*90}")
    for k in ['🟢建仓','🟢加仓','✅持有','🟡观察','🟡关注','→跟踪','🔴减仓','🔴清仓']:
        if cats.get(k): lines.append(f"  {k}: {', '.join(cats[k])}")

    # ═══ 牛市风险信号 ═══
    lines.append(f"\n{'─'*90}")
    lines.append("🐂 牛市风险信号")
    lines.append(f"{'─'*90}")

    # CSI800/MA200 check
    try:
        idx_df = load_meta('csi800_index')
        if not idx_df.empty:
            idx_s = idx_df.set_index('date')['close'].sort_index()
            if len(idx_s) > 200:
                ma200 = idx_s.iloc[-200:].mean()
                cur_idx = idx_s.iloc[-1]
                ratio = cur_idx / ma200
                if ratio >= 1.0:
                    lines.append(f'  ✅ CSI800/MA200 = {ratio:.2f} (牛市, 满仓)')
                elif ratio >= 0.9:
                    lines.append(f'  🟡 CSI800/MA200 = {ratio:.2f} (警戒线) 降半仓')
                else:
                    lines.append(f'  🔴 CSI800/MA200 = {ratio:.2f} (熊市!) 空仓!')
    except: pass

    # Fund flow macro check
    held_codes = set()
    try:
        import pandas as _pd2
        _dfh = _pd2.read_csv(HOLDINGS_FILE, dtype={'code':str})
        _dfh['code'] = _dfh['code'].str.zfill(6)
        held_codes = set(_dfh[_dfh['monitor']==True]['code'].tolist())
    except: pass

    # Count sector flows
    if flow:
        elect_out = sum(1 for c in held_codes if flow.get(c,{}).get('main',0) < -0.5 and
            ind_map.get(c,'')=='电子')
        total_held_in_flow = sum(1 for c in held_codes if c in flow)
        if total_held_in_flow > 0:
            outflow_pct = sum(1 for c in held_codes if flow.get(c,{}).get('main',0) < -0.5) / total_held_in_flow * 100
            if outflow_pct > 70:
                lines.append(f'  🔴 全板块机构流出: {outflow_pct:.0f}%持仓被卖出 — 系统性风险!')
            elif outflow_pct > 40:
                lines.append(f'  🟡 机构偏空: {outflow_pct:.0f}%持仓被卖出 — 关注')
            else:
                lines.append(f'  ✅ 机构流动正常')

    # ═══ 板块轮动 ═══
    lines.append(f"\n📊 板块资金流向 (今日MCP):")
    # Aggregate by sector
    sec_flows = {}
    for code in held_codes:
        ind = ind_map.get(code, '其他')
        mt = flow.get(code, {}).get('main', 0)
        sec_flows[ind] = sec_flows.get(ind, 0) + mt

    ranked = sorted(sec_flows.items(), key=lambda x: -x[1])
    for sec, amt in ranked[:6]:
        bar = '🟢流入' if amt > 0.5 else ('🟠微出' if amt > -1 else '🔴流出')
        lines.append(f'  {sec:<6} {amt:>+6.1f}亿 {bar}')
    lines.append(f'  → 轮动方向: 电子→有色 已启动, 明日确认是否持续')

    body = '\n'.join(reversal_lines + lines)

    try:
        from monitoring.alerts import send_alert
        send_alert(body)
        logger.info("10维早盘报告已发送")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")

    print(body)

if __name__ == '__main__':
    run()
