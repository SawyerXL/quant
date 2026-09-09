"""
增强分析器 v2 — 基本面+高级技术面+盈亏比+分档操作+行业对比+量价背离+止损提醒
借鉴问财框架, 综合十维分析
用法: python scripts/enhanced_analyzer.py <code> [code2...]
"""
import sys, pandas as pd, numpy as np, akshare as ak, requests, json
from pathlib import Path
from datetime import date, datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.storage import load_daily, load_meta

def sf(v):
    """解析带单位的财务数字"""
    try: return float(v)
    except:
        if isinstance(v, str):
            v = v.replace(',','').replace('%','').strip()
            if '亿' in v: return float(v.replace('亿','')) * 1e8
            if '万' in v: return float(v.replace('万','')) * 1e4
            if v and v != '-': return float(v)
        return 0.0

def rt_price(code):
    exch = 'sh' if code.startswith(('6','68')) else 'sz'
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={exch}{code}',
            headers={'Referer':'https://finance.sina.com.cn'}, timeout=5)
        d = r.text.split('"')[1].split(',')
        return {'name': d[0], 'cur': float(d[3]) if float(d[3])>0 else float(d[2]),
                'prev': float(d[2]), 'high': float(d[4]), 'low': float(d[5]),
                'vol': float(d[8]) if len(d)>8 else 0, 'amount': float(d[9]) if len(d)>9 else 0}
    except: return None

# ══════════════════════════════════════════════
# 1. 基本面 (修复版)
# ══════════════════════════════════════════════
FIN_CACHE = {}
def get_financials(code, cur_price=None):
    if code in FIN_CACHE: return FIN_CACHE[code]
    try:
        df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty: return None
        latest = df.iloc[-1]
        d = {
            'eps': sf(latest.get('基本每股收益', 0)),
            'bps': sf(latest.get('每股净资产', 0)),
            'revenue': sf(latest.get('营业总收入', 0)),
            'revenue_yoy': sf(latest.get('营业总收入同比增长率', 0)),
            'profit': sf(latest.get('净利润', 0)),
            'profit_yoy': sf(latest.get('净利润同比增长率', 0)),
            'gross_margin': sf(latest.get('销售毛利率', 0)),
            'net_margin': sf(latest.get('销售净利率', 0)),
            'roe': sf(latest.get('净资产收益率', 0)),
            'debt_ratio': sf(latest.get('资产负债率', 0)),
            'current_ratio': sf(latest.get('流动比率', 0)),
        }
        if cur_price:
            d['pe'] = cur_price/d['eps'] if d['eps']>0 else 0
            d['pb'] = cur_price/d['bps'] if d['bps']>0 else 0
        # Financial health tag
        if d['revenue_yoy']>10 and d['profit_yoy']>10: d['tag']='🟢成长型'
        elif d['revenue_yoy']>0: d['tag']='🟡稳定型'
        elif d['profit_yoy']>-20: d['tag']='🟠承压型'
        else: d['tag']='🔴困境型'
        FIN_CACHE[code] = d
        return d
    except: return None

# ══════════════════════════════════════════════
# 2. 技术面 (含量价背离)
# ══════════════════════════════════════════════
def get_technicals(code):
    try:
        end_dt = str(date.today())
        df = load_daily(code, '2026-01-01', end_dt)
        if df.empty: return None
        df['dt'] = pd.to_datetime(df['date']); df = df.set_index('dt').sort_index()
        cl = pd.to_numeric(df['close'], errors='coerce').dropna()
        vol = pd.to_numeric(df['volume'], errors='coerce')
        if len(cl) < 60: return None

        ma20 = cl.iloc[-20:].mean(); ma60 = cl.iloc[-60:].mean()
        ma120 = cl.iloc[-120:].mean() if len(cl)>=120 else ma60
        boll_low = ma20 - 2*cl.iloc[-20:].std()
        low60 = cl.iloc[-60:].min(); high60 = cl.iloc[-60:].max() 

        # RSI14
        delta = cl.diff(); gain = delta.clip(lower=0); loss=(-delta).clip(lower=0)
        rsi = 100-(100/(1+gain.rolling(14).mean()/loss.rolling(14).mean()))

        # ATR14
        hi = pd.to_numeric(df['high'], errors='coerce'); lo = pd.to_numeric(df['low'], errors='coerce')
        tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()

        # MACD
        e12=cl.ewm(span=12).mean(); e26=cl.ewm(span=26).mean()
        dif=e12-e26; dea=dif.ewm(span=9).mean(); macd=2*(dif-dea)

        # ═══ 量价背离 ═══
        price_5d = (cl.iloc[-1]/cl.iloc[-6]-1)*100 if len(cl)>=6 else 0
        vol_5d = vol.iloc[-5:].mean()/vol.iloc[-10:-5].mean()-1 if len(vol)>=10 else 0
        divergence = None
        if price_5d < -3 and vol_5d > 0.3:
            divergence = '🔴价跌量增(可能吸筹)' 
        elif price_5d < -5 and vol_5d < -0.3:
            divergence = '价跌量缩(无人接盘)'
        elif price_5d > 5 and vol_5d < -0.2:
            divergence = '🟡价涨量缩(动能衰竭)'
        elif price_5d > 5 and vol_5d > 0.5:
            divergence = '🟢价涨量增(健康)'

        cur = cl.iloc[-1]
        return {
            'cur': cur, 'ma20': ma20, 'ma60': ma60, 'ma120': ma120,
            'boll_low': boll_low, 'low60': low60, 'high60': high60,
            'rsi14': rsi.iloc[-1], 'atr_pct': atr.iloc[-1]/cur*100,
            'macd_bar': macd.iloc[-1], 'dif': dif.iloc[-1], 'dea': dea.iloc[-1],
            'ret20': (cur/cl.iloc[-21]-1)*100 if len(cl)>=21 else 0,
            'ret60': (cur/cl.iloc[-61]-1)*100 if len(cl)>=61 else 0,
            'price_5d': price_5d, 'vol_5d': vol_5d,
            'divergence': divergence,
        }
    except: return None

# ══════════════════════════════════════════════
# 3. 行业对比 (同行业相对强弱)
# ══════════════════════════════════════════════
SECTOR_CACHE = {}
def get_sector_peers(code, sector_name=None):
    """获取同行业股票并对比相对强弱"""
    if 'all_stocks' not in SECTOR_CACHE:
        # Load industry data
        try:
            info = load_meta('stock_info_full')
            SECTOR_CACHE['all_stocks'] = info
        except:
            SECTOR_CACHE['all_stocks'] = pd.DataFrame()

    info = SECTOR_CACHE['all_stocks']
    if info.empty: return None

    # Find the stock's industry
    row = info[info['code'].astype(str).str.zfill(6) == code]
    if row.empty: return None
    if sector_name is None:
        sector_name = row['industry_l1'].iloc[0] if 'industry_l1' in info.columns else None
    if not sector_name: return None

    peers = info[info['industry_l1'] == sector_name]
    peer_codes = peers['code'].astype(str).str.zfill(6).tolist()[:10]

    # Get recent returns for peers
    peer_data = []
    for pc in peer_codes:
        t = get_technicals(pc)
        if t:
            peer_data.append({'code': pc, 'name': peers[peers['code'].astype(str).str.zfill(6)==pc]['name'].iloc[0] if 'name' in peers.columns else pc,
                             'ret20': t['ret20'], 'cur': t['cur']})

    if len(peer_data) < 2: return None

    # Rank
    peer_data.sort(key=lambda x: -x['ret20'])
    rank = next((i+1 for i, p in enumerate(peer_data) if p['code']==code), len(peer_data))
    avg_ret = sum(p['ret20'] for p in peer_data)/len(peer_data)

    # The stock's own ret
    t = get_technicals(code)
    own_ret = t['ret20'] if t else 0

    return {
        'sector': sector_name, 'peers': len(peer_data),
        'rank': rank, 'total': len(peer_data),
        'sector_avg': avg_ret, 'own_ret': own_ret,
        'vs_sector': own_ret - avg_ret,
    }

# ══════════════════════════════════════════════
# 4. 止损提醒
# ══════════════════════════════════════════════
def check_stoploss(code, name, t, cost_basis=None):
    """检查是否触发止损"""
    alerts = []
    cur = t['cur']

    # Technical stop: below BOLL lower band
    if cur < t['boll_low']:
        alerts.append(f"跌破BOLL下轨{t['boll_low']:.1f}")

    # Trend stop: below 60-day low * 0.98
    stop2 = t['low60'] * 0.98
    if cur < stop2:
        alerts.append(f"跌破60日低点止损{stop2:.1f}")

    # Cost basis stop: -15% from cost
    if cost_basis:
        pnl = (cur/cost_basis - 1)*100
        if pnl < -15:
            alerts.append(f"浮亏{pnl:.0f}%触发硬止损")

    if alerts:
        return f"🔴 {code} {name}: {', '.join(alerts)}"
    return None

# ══════════════════════════════════════════════
# 5. 综合输出
# ══════════════════════════════════════════════
def analyze(code, name='', cost_basis=None):
    rt = rt_price(code)
    if not rt: return f"{code}: 无法获取实时行情"
    name = name or rt['name']; cur = rt['cur']

    t = get_technicals(code)
    if not t: return f"{code} {name}: 技术数据不足"
    t['cur'] = cur  # Use realtime price

    fin = get_financials(code, cur)
    sector = get_sector_peers(code)
    stop_alert = check_stoploss(code, name, t, cost_basis)

    # ═══ Output ═══
    lines = []
    lines.append(f"\n{'='*55}")
    lines.append(f"  {code} {name} 增强分析 ¥{cur:.2f}")
    lines.append(f"{'='*55}")

    # Row 1: Trend + Alerts
    if cur > t['ma60']: trend='🟢多头'
    elif cur > t['ma20']: trend='🟡震荡'
    elif cur > t['boll_low']: trend='🟠弱势'
    else: trend='🔴超卖'

    macd_sig = '金叉↑' if t['macd_bar']>0 and t['dif']>t['dea'] else ('修复中' if t['macd_bar']>0 else '死叉↓')
    lines.append(f"  趋势:{trend} | RSI14:{t['rsi14']:.0f} | MACD:{macd_sig} | ATR:{t['atr_pct']:.1f}%")

    # Row 2: Volume-price divergence
    if t['divergence']:
        lines.append(f"  量价: {t['divergence']} (价{t['price_5d']:+.1f}%/量{t['vol_5d']*100:+.0f}%)")

    # Row 3: Fundamentals
    if fin:
        rev_u = '亿' if fin['revenue']>1e8 else '万'
        prf_u = '亿' if fin['profit']>1e8 else '万'
        lines.append(f"  财务: {fin['tag']} | PE{fin['pe']:.0f}x PB{fin['pb']:.1f}x")
        lines.append(f"  营收:{fin['revenue']/(1e8 if rev_u=='亿' else 1e4):.1f}{rev_u}(YoY{fin['revenue_yoy']:+.1f}%) "
                     f"利润:{fin['profit']/(1e8 if prf_u=='亿' else 1e4):.1f}{prf_u}(YoY{fin['profit_yoy']:+.1f}%) "
                     f"毛利{fin['gross_margin']:.1f}% ROE{fin['roe']:.1f}%")
    else:
        lines.append(f"  财务: 数据暂缺")

    # Row 4: Sector comparison
    if sector:
        better = '跑赢' if sector['vs_sector']>0 else '跑输'
        lines.append(f"  行业({sector['sector']}): 排{sector['rank']}/{sector['total']} | 20日{sector['own_ret']:+.1f}% vs 均值{sector['sector_avg']:+.1f}% → {better}{abs(sector['vs_sector']):.1f}pp")

    # Row 5: Key levels
    lines.append(f"  关键位: MA20={t['ma20']:.1f} MA60={t['ma60']:.1f} BOLL下={t['boll_low']:.1f} 60日低={t['low60']:.1f}")

    # Row 6: Risk/Reward
    target1=t['ma20']; stop1=t['boll_low']
    up1=(target1/cur-1)*100; dn1=(cur/stop1-1)*100 if stop1>0 else 0
    rr1=up1/dn1 if dn1>0 else 0

    target2=t['ma60']; stop2=t['low60']*0.98
    up2=(target2/cur-1)*100; dn2=(cur/stop2-1)*100 if stop2>0 else 0
    rr2=up2/dn2 if dn2>0 else 0

    lines.append(f"  盈亏比A: {rr1:.1f}:1 (→MA20 +{up1:.1f}% / 止损 -{dn1:.1f}%)")
    lines.append(f"  盈亏比B: {rr2:.1f}:1 (→MA60 +{up2:.1f}% / 止损 -{dn2:.1f}%)")

    # Row 7: Action
    if cost_basis:
        pnl = (cur/cost_basis-1)*100
        lines.append(f"  持有成本: ¥{cost_basis:.2f} | 盈亏: {pnl:+.1f}%")

    if cur > t['ma60']:
        lines.append(f"  ✅ 建议: 持有为主, 回踩{t['ma20']:.0f}可加仓")
    elif cur > t['ma20']:
        lines.append(f"  ✅ 建议: 持有观察, 止损{t['boll_low']:.0f}")
    elif rr2 > 2:
        lines.append(f"  ⚠️ 建议: 试探仓{t['boll_low']:.0f}元(20-30%仓位), 加仓{t['low60']:.0f}, 止损{stop2:.0f}")
    else:
        lines.append(f"  🔴 建议: 等右侧确认 — 放量站上{t['ma20']:.0f}再考虑")

    # Row 8: Stop-loss alert
    if stop_alert:
        lines.append(f"  {stop_alert}")

    # ═══ 最终结论 ═══
    lines.append(f"\n  ── 最终结论 ──")

    # 综合评分 (权重: 趋势25% + 盈亏比25% + 行业20% + 财务15% + 量价15%)
    verdict_score = 0
    verdict_items = []

    # 趋势 (25%)
    if cur > t['ma60']:
        verdict_score += 25; verdict_items.append('✅ 趋势多头')
    elif cur > t['ma20']:
        verdict_score += 15; verdict_items.append('🟡 趋势震荡')
    elif cur > t['boll_low']:
        verdict_score += 5; verdict_items.append('🟠 趋势偏弱')
    else:
        verdict_score -= 10; verdict_items.append('🔴 超卖区域')

    # 盈亏比 (25%)
    if rr2 > 3: verdict_score += 25; verdict_items.append('✅ 盈亏比优秀')
    elif rr2 > 1.5: verdict_score += 15; verdict_items.append('🟡 盈亏比合理')
    elif rr2 > 0.5: verdict_score += 5; verdict_items.append('🟠 盈亏比偏低')
    else: verdict_score -= 5; verdict_items.append('🔴 盈亏比不利')

    # 行业相对强弱 (20%)
    if sector:
        if sector['vs_sector'] > 3:
            verdict_score += 20; verdict_items.append(f"✅ 跑赢同行{sector['vs_sector']:.0f}pp")
        elif sector['vs_sector'] > 0:
            verdict_score += 10; verdict_items.append(f"🟡 略赢同行")
        elif sector['vs_sector'] > -5:
            verdict_score += 0; verdict_items.append(f"🟠 跑输同行")
        else:
            verdict_score -= 10; verdict_items.append(f"🔴 大幅跑输")

    # 财务 (15%)
    if fin:
        if fin['tag'] in ('🟢成长型',): verdict_score += 15
        elif fin['tag'] in ('🟡稳定型',): verdict_score += 8
        elif fin['tag'] in ('🟠承压型',): verdict_score += 2
        verdict_items.append(f"{fin['tag']} PE{fin['pe']:.0f}x")

    # 量价信号 (15%)
    if t['divergence']:
        if '吸筹' in t['divergence']:
            verdict_score += 10; verdict_items.append('🟢价跌量增(吸筹)')
        elif '衰竭' in t['divergence']:
            verdict_score -= 5; verdict_items.append('🟡量价背离')
        elif '健康' in t['divergence']:
            verdict_score += 15; verdict_items.append('🟢量价健康')

    # 止损状态
    if stop_alert: verdict_score -= 20; verdict_items.append('⚠️触发止损')

    # 最终判定
    if verdict_score >= 60:   action = '🟢 持有/加仓'; detail = '各项指标健康，可继续持有或逢低加仓'
    elif verdict_score >= 40: action = '🟢 持有观察'; detail = '整体偏正面，持有但暂不加仓'
    elif verdict_score >= 20: action = '🟡 谨慎持有'; detail = '多空交织，小仓位持有，密切观察'
    elif verdict_score >= 0:  action = '🟠 减仓观望'; detail = '信号偏弱，建议减仓或等右侧确认'
    else:                     action = '🔴 止损/清仓'; detail = '多项指标恶化，建议止损或大幅减仓'

    lines.append(f"  综合评分: {verdict_score}/100")
    lines.append(f"  判定: {action} | {detail}")

    if cost_basis:
        pnl = (cur/cost_basis-1)*100
        lines.append(f"  持有盈亏: {pnl:+.1f}%")
    lines.append(f"  关注: {', '.join(verdict_items)}")

    return '\n'.join(lines)

# ══════════════════════════════════════════════
# Batch: 扫描所有持仓的止损
# ══════════════════════════════════════════════
def scan_all_stoploss(holdings_dict):
    """扫描所有持仓, 返回触发止损的列表"""
    alerts = []
    for code, (name, cost, shares) in holdings_dict.items():
        t = get_technicals(code)
        if not t: continue
        rt = rt_price(code)
        if rt: t['cur'] = rt['cur']
        alert = check_stoploss(code, name, t, cost)
        if alert:
            alerts.append(alert)
    return alerts

if __name__ == '__main__':
    codes = sys.argv[1:] if len(sys.argv) > 1 else ['600893']
    for c in codes:
        print(analyze(c))

    # Also scan all personal holdings for stop-loss
    if len(sys.argv) == 1 or '--scan' in sys.argv:
        print(f"\n{'='*55}")
        print(f"  止损扫描")
        print(f"{'='*55}")
        try:
            df_h = pd.read_csv('config/my_holdings.csv', dtype={'code':str})
            df_h['code'] = df_h['code'].str.zfill(6)
            held = df_h[df_h['shares']>0]
            hd = {}
            for _,r in held.iterrows():
                hd[r['code']] = (r['name'], float(r['cost_price']), int(r['shares']))
            alerts = scan_all_stoploss(hd)
            if alerts:
                for a in alerts: print(f"  {a}")
            else:
                print(f"  ✅ 全部正常, 无止损触发")
        except Exception as e:
            print(f"  扫描失败: {e}")
