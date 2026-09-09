"""
资金流向监控 — 多指标综合判断资金进场/撤出
数据源: sina(指数+涨跌家数) + akshare(融资余额) + 本地数据(成交额)
用法: python scripts/flow_monitor.py
输出: 资金面评分(-100~+100) + 信号灯(🟢🟡🟠🔴) + 预警
集成: 被 morning_dual_report.py 调用
"""
import sys, requests, pandas as pd, numpy as np, json
from pathlib import Path
from datetime import date, datetime, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger

# ══════════════════════════════════
# Data Sources
# ══════════════════════════════════

def _get_market_data():
    """获取上证+深证成交额(新浪), 返回总量(亿)和涨跌家数"""
    try:
        total_amt = 0; up_idx = 0; down_idx = 0
        for code, exch in [('s_sh000001','sh'), ('s_sz399001','sz'), ('s_sz399006','sz')]:
            r = requests.get(f'http://hq.sinajs.cn/list={code}',
                headers={'Referer':'https://finance.sina.com.cn'}, timeout=5)
            d = r.text.split('"')[1].split(',')
            # Format: name, cur, change, pct, volume(手), amount(万)
            cur = float(d[1]) if len(d) > 1 else 0
            if cur > 0 and len(d) >= 6:
                amt = float(d[5]) / 1e4  # 万→亿
                if exch == 'sh':
                    total_amt += amt
                elif code == 's_sz399001':
                    total_amt += amt
            # Breadth: major indices
            if len(d) >= 4:
                change_pct = float(d[3]) if '.' in d[3] else 0
                if change_pct > 0: up_idx += 1
                elif change_pct < 0: down_idx += 1
        return {
            'total_amount': total_amt,  # 两市成交额(亿, 近似)
            'up_indices': up_idx,
            'down_indices': down_idx,
            'sh_close': 0,  # filled below
        }
    except: return None

def _get_margin_balance():
    """获取融资余额(akshare)"""
    try:
        import akshare as ak
        df = ak.stock_margin_sse(start_date='20260101', end_date=date.today().strftime('%Y%m%d'))
        if df is None or df.empty: return None
        cols = df.columns.tolist()
        # Find the right column for 融资余额
        date_col = cols[0]
        # 融资余额 is usually named 融资余额 or similar
        bal_col = [c for c in cols if '融资余额' in c or 'margin' in c.lower()]
        if not bal_col:
            # Try: 沪深两市融资余额 is usually second column
            bal_col = [cols[1]] if len(cols) > 1 else []
        if not bal_col: return None
        df = df.rename(columns={date_col: 'date', bal_col[0]: 'balance'})
        df['date'] = pd.to_datetime(df['date'])
        latest = df[df['balance'].notna()].iloc[-1] if not df.empty else None
        if latest is not None:
            return {'date': str(latest['date'])[:10], 'balance': float(latest['balance']), 'df': df}
    except Exception as e:
        logger.debug(f"融资余额获取失败: {e}")
    return None

def _get_recent_volume():
    """从本地数据获取近期日均成交额"""
    from data.storage import load_meta, load_daily
    try:
        idx = load_meta('csi800_index')
        if idx.empty: return None
        idx['date'] = pd.to_datetime(idx['date'])
        idx = idx.set_index('date').sort_index()
        recent = idx.tail(30)
        if 'amount' in recent.columns:
            return recent['amount']
        return None
    except: return None

# ══════════════════════════════════
# Analysis
# ══════════════════════════════════

def analyze():
    """综合资金面分析, 返回 (score: int, signal: str, alerts: list, details: dict)"""
    score = 0
    alerts = []
    details = {}

    # ── 1. 指数成交额 + 市场宽度 ──
    mkt = _get_market_data()
    if mkt and mkt['total_amount'] > 0:
        amt = mkt['total_amount']
        details['两市成交额(亿)'] = f'{amt:.0f}'

        if amt > 35000: score += 20; details['量能'] = '🟢 极度活跃(>3.5万亿)'
        elif amt > 28000: score += 10; details['量能'] = '🟢 活跃(2.8-3.5万亿)'
        elif amt > 20000: score += 0; details['量能'] = '🟡 正常(2.0-2.8万亿)'
        elif amt > 15000: score -= 10; details['量能'] = '🟠 偏低(1.5-2.0万亿)'
        else: score -= 20; alerts.append('🔴 成交额<1.5万亿, 流动性枯竭警戒'); details['量能'] = '🔴 枯竭(<1.5万亿)'

        # Market breadth
        up = mkt.get('up_indices', 0); down = mkt.get('down_indices', 0)
        if up + down > 0:
            details['主要指数涨跌'] = f'{up}涨{down}跌'
            if up >= 3: score += 5
            elif down >= 3: score -= 5
    margin = _get_margin_balance()
    if margin and 'df' in margin:
        df = margin['df'].sort_values('date')
        bal = df[df['balance'].notna()]
        if len(bal) >= 5:
            latest_bal = float(bal['balance'].iloc[-1])
            bal_5d = float(bal['balance'].iloc[-5])
            bal_10d = float(bal['balance'].iloc[-10]) if len(bal) >= 10 else bal_5d
            chg_5d = (latest_bal/bal_5d - 1) * 100
            chg_10d = (latest_bal/bal_10d - 1) * 100
            details['融资余额(亿)'] = f'{latest_bal/1e8:.0f}'
            details['融资5日变化'] = f'{chg_5d:+.1f}%'

            if chg_5d > 2: score += 15; details['融资趋势'] = '🟢 加速流入'
            elif chg_5d > 0: score += 5; details['融资趋势'] = '🟢 温和流入'
            elif chg_5d > -2: score += 0; details['融资趋势'] = '🟡 小幅流出'
            elif chg_5d > -5: score -= 10; alerts.append('🟠 融资余额连续下降, 杠杆资金撤退'); details['融资趋势'] = '🟠 持续流出'
            else: score -= 20; alerts.append('🔴 融资余额大幅下降, 杠杆资金恐慌'); details['融资趋势'] = '🔴 恐慌流出'

            # Consecutive decline days
            cons_down = 0
            for i in range(len(bal)-1, max(0, len(bal)-20), -1):
                if float(bal['balance'].iloc[i]) < float(bal['balance'].iloc[i-1]):
                    cons_down += 1
                else: break
            details['融资连降天数'] = cons_down
            if cons_down >= 5: score -= 15; alerts.append(f'🔴 融资余额连降{cons_down}天!')

    # ── 4. 近期成交额趋势 ──
    vol_hist = _get_recent_volume()
    if vol_hist is not None and len(vol_hist) >= 10:
        vol_5d = float(vol_hist.iloc[-5:].mean())
        vol_10d = float(vol_hist.iloc[-10:-5].mean())
        vol_trend = (vol_5d/vol_10d - 1) * 100 if vol_10d > 0 else 0
        details['成交额趋势(5d vs 10d)'] = f'{vol_trend:+.1f}%'
        if vol_trend > 10: score += 10; details['量趋势'] = '🟢 放量'
        elif vol_trend > 0: score += 3; details['量趋势'] = '🟢 温和放量'
        elif vol_trend > -10: score -= 3; details['量趋势'] = '🟡 缩量'
        else: score -= 10; alerts.append('🟠 成交额持续萎缩'); details['量趋势'] = '🟠 持续缩量'

    # ── 5. 指数技术位 ──
    try:
        from data.storage import load_meta
        idx_df = load_meta('csi800_index')
        if not idx_df.empty:
            idx_df['date'] = pd.to_datetime(idx_df['date'])
            idx_df = idx_df.set_index('date').sort_index()
            cl = pd.to_numeric(idx_df['close'], errors='coerce').dropna()
            if len(cl) >= 20:
                ma20 = float(cl.iloc[-20:].mean())
                cur_idx = float(cl.iloc[-1])
                details['指数'] = f'{cur_idx:.0f}'
                if cur_idx > ma20:
                    score += 5; details['vsMA20'] = '🟢 MA20上方'
                else:
                    score -= 5; details['vsMA20'] = '🔴 MA20下方'
    except: pass

    # ── Signal light ──
    if score >= 30: signal = '🟢 资金加速流入, 积极做多'
    elif score >= 10: signal = '🟢 资金温和流入, 正常参与'
    elif score >= -10: signal = '🟡 资金面中性, 谨慎参与'
    elif score >= -30: signal = '🟠 资金持续撤出, 注意风控'
    else: signal = '🔴 资金大幅撤离! 减仓/空仓'

    return score, signal, alerts, details

# ══════════════════════════════════
# Main
# ══════════════════════════════════
def run():
    """输出资金面完整报告"""
    score, signal, alerts, details = analyze()

    print(f"\n{'='*60}")
    print(f"  资金流向监控  {date.today()}")
    print(f"{'='*60}")
    print(f"\n  综合评分: {score:+d}/100  |  信号: {signal}")

    print(f"\n  ── 指标明细 ──")
    for k, v in details.items():
        print(f"  · {k}: {v}")

    if alerts:
        print(f"\n  ⚠️ 预警信号:")
        for a in alerts:
            print(f"    {a}")
    else:
        print(f"\n  ✅ 无预警信号")

    # 历史评分参考
    print(f"\n  ── 评分参考 ──")
    print(f"  >+30: 积极  |  +10~+30: 正常  |  -10~+10: 中性")
    print(f"  -30~-10: 警惕  |  <-30: 危险")
    print(f"{'='*60}\n")

    return score, signal, alerts, details

if __name__ == '__main__':
    run()
