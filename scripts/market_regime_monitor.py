"""
市场牛熊状态每日监控 — 多维度判断 + 分级预警
用法: python scripts/market_regime_monitor.py
      python scripts/market_regime_monitor.py --json  # JSON输出(供cron邮件)
      python scripts/market_regime_monitor.py --quiet # 仅输出预警(无预警则静默)

预警体系:
  🟡 NOTICE  — 单个指标边际恶化, 关注但不操作
  🟠 WARNING — 多个指标同步恶化, 应减仓/收紧止损
  🔴 DANGER  — 牛转熊概率>50%, 应大幅降低仓位
  ⚫ BEAR    — 熊市已确认, 应空仓或仅做防御

关键阈值(基于A股历史回测):
  1. 上证<MA200 持续5天 → 牛市结构受损
  2. 上证<MA200 持续20天 → 熊市确认
  3. 上证从52周高点回撤>15% → 技术性修正
  4. 上证从52周高点回撤>20% → 技术性熊市
  5. 融资余额连降>10天 + 指数破MA60 → 杠杆崩塌
  6. 成交量萎缩到60日均量的60%以下 → 流动性枯竭
  7. 3+主要指数同时跌破MA60 → 中期趋势转弱
  8. MA60下穿MA200(死叉) → 长期趋势转熊(滞后但可靠)
"""
import sys, os, json, argparse
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger

# ═══════════════════════════════════════
# Constants — 阈值定义
# ═══════════════════════════════════════

# 指数列表(代码, 名称, 类型)
WATCH_INDICES = [
    ('sh000001', '上证指数', 'broad'),
    ('sh000300', '沪深300', 'broad'),
    ('sh000905', '中证500', 'broad'),
    ('sh000852', '中证1000', 'small'),
    ('sz399006', '创业板指', 'growth'),
    ('sh000688', '科创50', 'tech'),
]

# 距离阈值
THRESHOLDS = {
    'drawdown_warning': -10.0,    # 高点回撤>10% → 警告
    'drawdown_correction': -15.0,  # 高点回撤>15% → 修正
    'drawdown_bear': -20.0,        # 高点回撤>20% → 技术性熊市
    'ma200_below_days_warn': 3,    # 连续在MA200下方天数→警告
    'ma200_below_days_danger': 5,  # →危险
    'ma200_below_days_bear': 20,   # →熊市确认
    'ma60_break_count_warn': 2,    # 几个指数同时破MA60→警告
    'ma60_break_count_danger': 4,  # →危险
    'volume_contract_warn': 0.75,  # 量vs60日均量<75%→缩量
    'volume_contract_danger': 0.60,# <60%→枯竭
    'margin_cons_days_warn': 5,    # 融资连降天数→警告
    'margin_cons_days_danger': 10, # →危险
    'margin_cons_days_panic': 15,  # →恐慌
    'margin_chg_warn': -3.0,       # 融资5日变化<-3%→警告
    'margin_chg_danger': -8.0,     # <-8%→危险
}

# ═══════════════════════════════════════
# Data Sources
# ═══════════════════════════════════════

def _get_index_data(code: str):
    """获取指数日线"""
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=code)
    if df is None or df.empty:
        return None
    return df

def _get_margin_data():
    """获取融资余额(SSE)"""
    try:
        import akshare as ak
        df = ak.stock_margin_sse(
            start_date=(date.today() - timedelta(days=60)).strftime('%Y%m%d'),
            end_date=date.today().strftime('%Y%m%d')
        )
        if df is None or df.empty:
            return None
        cols = df.columns.tolist()
        date_col = cols[0]
        bal_col = [c for c in cols if '融资余额' in str(c)]
        if not bal_col:
            bal_col = [cols[1]] if len(cols) > 1 else []
        if not bal_col:
            return None
        df = df.rename(columns={date_col: 'date', bal_col[0]: 'balance'})
        df['date'] = pd.to_datetime(df['date'])
        return df[df['balance'].notna()]
    except Exception as e:
        logger.debug(f"融资数据: {e}")
    return None

def _get_market_breadth():
    """获取涨跌家数(新浪)"""
    try:
        import requests
        total_up, total_down = 0, 0
        for code in ['s_sh000001', 's_sz399001', 's_sz399005', 's_sz399006']:
            r = requests.get(f'http://hq.sinajs.cn/list={code}',
                headers={'Referer': 'https://finance.sina.com.cn'}, timeout=5)
            d = r.text.split('"')[1].split(',')
            if len(d) >= 4:
                close = float(d[1]) if d[1] else 0
                prev_close = float(d[2]) if d[2] else close
                if close > prev_close: total_up += 1
                elif close < prev_close: total_down += 1
        return total_up, total_down
    except:
        return None, None

# ═══════════════════════════════════════
# Analysis Engines
# ═══════════════════════════════════════

def analyze_index_technical(code: str, name: str, idx_type: str):
    """分析单个指数的技术状态, 返回dict"""
    df = _get_index_data(code)
    if df is None:
        return None

    close = df['close'].values
    dates = df['date'].values
    latest = close[-1]
    latest_date = str(dates[-1])

    # MAs
    ma5 = np.mean(close[-5:])
    ma10 = np.mean(close[-10:])
    ma20 = np.mean(close[-20:])
    ma60 = np.mean(close[-60:]) if len(close) >= 60 else None
    ma120 = np.mean(close[-120:]) if len(close) >= 120 else None
    ma200 = np.mean(close[-200:]) if len(close) >= 200 else None

    # 52w high & drawdown
    n250 = min(250, len(close))
    high_52w = np.max(close[-n250:])
    dd_52w = (latest / high_52w - 1) * 100

    # 60d high drawdown
    high_60d = np.max(close[-60:]) if len(close) >= 60 else high_52w
    dd_60d = (latest / high_60d - 1) * 100

    # Consecutive days below MA200
    days_below_ma200 = 0
    if ma200 is not None:
        for i in range(len(close) - 1, -1, -1):
            if close[i] < ma200:
                days_below_ma200 += 1
            else:
                break

    # MA alignment
    if ma60 is not None and ma200 is not None:
        if ma20 > ma60 > ma200 and latest > ma20:
            trend = 'bullish'
        elif ma20 > ma60 and latest < ma20:
            trend = 'bullish_correction'
        elif latest < ma20 < ma60:
            trend = 'bearish'
        elif latest > ma20 and ma20 < ma60:
            trend = 'bounce'
        else:
            trend = 'mixed'
    else:
        trend = 'unknown'

    # Death cross / golden cross
    ma60_ma200 = None
    if ma60 is not None and ma200 is not None:
        ma60_ma200 = 'above' if ma60 > ma200 else 'below'

    # Returns
    ret_5d = (latest / close[-6] - 1) * 100 if len(close) > 5 else None
    ret_20d = (latest / close[-21] - 1) * 100 if len(close) > 20 else None
    ret_60d = (latest / close[-61] - 1) * 100 if len(close) > 60 else None

    return {
        'name': name, 'type': idx_type, 'code': code,
        'close': latest, 'date': latest_date,
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20,
        'ma60': ma60, 'ma200': ma200,
        'dd_52w': dd_52w, 'dd_60d': dd_60d,
        'days_below_ma200': days_below_ma200,
        'trend': trend, 'ma60_ma200': ma60_ma200,
        'ret_5d': ret_5d, 'ret_20d': ret_20d, 'ret_60d': ret_60d,
    }

def analyze_margin():
    """融资余额分析"""
    df = _get_margin_data()
    if df is None or df.empty:
        return None

    df = df.sort_values('date')
    balances = df['balance'].values
    dates = df['date'].values

    latest = float(balances[-1])
    bal_5d = float(balances[-5]) if len(balances) >= 5 else latest
    bal_10d = float(balances[-10]) if len(balances) >= 10 else latest

    chg_5d = (latest / bal_5d - 1) * 100 if bal_5d > 0 else 0
    chg_10d = (latest / bal_10d - 1) * 100 if bal_10d > 0 else 0

    # Consecutive decline days
    cons_down = 0
    for i in range(len(balances) - 1, max(0, len(balances) - 30), -1):
        if float(balances[i]) < float(balances[i - 1]):
            cons_down += 1
        else:
            break

    # Trend of declines (accelerating?)
    recent_changes = []
    for i in range(max(0, len(balances) - cons_down - 5), len(balances) - 1):
        if i > 0 and float(balances[i-1]) > 0:
            recent_changes.append((float(balances[i]) / float(balances[i-1]) - 1) * 100)

    accelerating = False
    if len(recent_changes) >= 3 and cons_down >= 5:
        # Check if recent declines are getting larger
        avg_first_half = np.mean(recent_changes[:len(recent_changes)//2])
        avg_second_half = np.mean(recent_changes[len(recent_changes)//2:])
        accelerating = avg_second_half < avg_first_half  # more negative

    return {
        'balance': latest, 'date': str(dates[-1])[:10],
        'chg_5d': chg_5d, 'chg_10d': chg_10d,
        'cons_down_days': cons_down, 'accelerating': accelerating,
    }

def analyze_volume():
    """量能分析(from 本地CSI800 index data)"""
    from data.storage import load_meta
    try:
        df = load_meta('csi800_index')
        if df.empty: return None
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()

        if 'amount' not in df.columns:
            return None

        amount = df['amount'].dropna()
        if len(amount) < 60:
            return None

        latest = float(amount.iloc[-1])
        ma5 = float(amount.iloc[-5:].mean())
        ma20 = float(amount.iloc[-20:].mean())
        ma60 = float(amount.iloc[-60:].mean())

        ratio_5_20 = ma5 / ma20 if ma20 > 0 else 1
        ratio_5_60 = ma5 / ma60 if ma60 > 0 else 1

        # Volume trend direction
        trend = 'expanding' if ratio_5_20 > 1.1 else ('contracting' if ratio_5_20 < 0.9 else 'stable')

        return {
            'latest': latest, 'ma5': ma5, 'ma20': ma20, 'ma60': ma60,
            'ratio_5_20': ratio_5_20, 'ratio_5_60': ratio_5_60,
            'trend': trend,
        }
    except:
        return None

# ═══════════════════════════════════════
# Alert Engine
# ═══════════════════════════════════════

def generate_alerts(indices_data: list, margin: dict, volume: dict) -> list:
    """综合判断, 生成分级预警列表"""
    alerts = []

    if not indices_data:
        return [{'level': 'error', 'msg': '指数数据获取失败'}]

    sh_data = next((i for i in indices_data if i['code'] == 'sh000001'), None)
    if sh_data is None:
        return [{'level': 'error', 'msg': '上证指数数据缺失'}]

    # ── Tier 1: 领先指标 ──

    # 1.1 融资余额连降
    if margin:
        cons = margin['cons_down_days']
        chg5 = margin['chg_5d']
        if cons >= THRESHOLDS['margin_cons_days_panic']:
            alerts.append({'level': 'danger', 'category': 'margin',
                'msg': f"融资余额连降{cons}天({chg5:+.1f}%) → 杠杆资金恐慌性撤退"})
        elif cons >= THRESHOLDS['margin_cons_days_danger']:
            alerts.append({'level': 'danger', 'category': 'margin',
                'msg': f"融资余额连降{cons}天({chg5:+.1f}%) → 杠杆资金持续撤退"})
        elif cons >= THRESHOLDS['margin_cons_days_warn']:
            if margin.get('accelerating'):
                alerts.append({'level': 'warning', 'category': 'margin',
                    'msg': f"融资余额连降{cons}天且加速({chg5:+.1f}%) → 注意杠杆踩踏"})
            else:
                alerts.append({'level': 'notice', 'category': 'margin',
                    'msg': f"融资余额连降{cons}天({chg5:+.1f}%) → 杠杆资金温和撤退"})

    # 1.2 成交量萎缩
    if volume:
        ratio_60 = volume['ratio_5_60']
        if ratio_60 < THRESHOLDS['volume_contract_danger']:
            alerts.append({'level': 'danger', 'category': 'volume',
                'msg': f"成交量仅60日均量的{ratio_60*100:.0f}% → 流动性枯竭警戒"})
        elif ratio_60 < THRESHOLDS['volume_contract_warn']:
            alerts.append({'level': 'warning', 'category': 'volume',
                'msg': f"成交量萎缩至60日均量的{ratio_60*100:.0f}% → 缩量阴跌风险"})

    # 1.3 涨跌比
    up_idx, down_idx = _get_market_breadth()
    if up_idx is not None and down_idx is not None:
        if up_idx == 0 and down_idx >= 3:
            alerts.append({'level': 'warning', 'category': 'breadth',
                'msg': f"主要指数全军覆没({up_idx}涨{dn_idx}跌) → 普跌格局"})

    # ── Tier 2: 同步指标 ──

    # 2.1 上证MA200
    if sh_data['ma200'] is not None:
        days = sh_data['days_below_ma200']
        if days >= THRESHOLDS['ma200_below_days_bear']:
            alerts.append({'level': 'bear', 'category': 'ma200',
                'msg': f"上证连续{days}天在MA200({sh_data['ma200']:.0f})下方 → ⚫ 熊市已确认"})
        elif days >= THRESHOLDS['ma200_below_days_danger']:
            alerts.append({'level': 'danger', 'category': 'ma200',
                'msg': f"上证连续{days}天在MA200({sh_data['ma200']:.0f})下方 → 牛市结构严重受损"})
        elif days >= THRESHOLDS['ma200_below_days_warn']:
            alerts.append({'level': 'warning', 'category': 'ma200',
                'msg': f"上证连续{days}天在MA200({sh_data['ma200']:.0f})下方 → 需要警惕"})

    # 2.2 回撤幅度
    dd = sh_data['dd_52w']
    if dd <= THRESHOLDS['drawdown_bear']:
        alerts.append({'level': 'bear', 'category': 'drawdown',
            'msg': f"上证从52周高点回撤{dd:+.1f}% → ⚫ 技术性熊市"})
    elif dd <= THRESHOLDS['drawdown_correction']:
        alerts.append({'level': 'danger', 'category': 'drawdown',
            'msg': f"上证从52周高点回撤{dd:+.1f}% → 进入技术性修正"})
    elif dd <= THRESHOLDS['drawdown_warning']:
        alerts.append({'level': 'warning', 'category': 'drawdown',
            'msg': f"上证从52周高点回撤{dd:+.1f}% → 回调加深, 关注"})

    # 2.3 多指数MA60破位
    below_ma60_count = sum(1 for i in indices_data
                           if i['ma60'] is not None and i['close'] < i['ma60'])
    if below_ma60_count >= THRESHOLDS['ma60_break_count_danger']:
        names = [i['name'] for i in indices_data if i['ma60'] and i['close'] < i['ma60']]
        alerts.append({'level': 'danger', 'category': 'ma60',
            'msg': f"{below_ma60_count}个主要指数跌破MA60: {', '.join(names)} → 中期趋势全面转弱"})
    elif below_ma60_count >= THRESHOLDS['ma60_break_count_warn']:
        names = [i['name'] for i in indices_data if i['ma60'] and i['close'] < i['ma60']]
        alerts.append({'level': 'warning', 'category': 'ma60',
            'msg': f"{below_ma60_count}个指数跌破MA60: {', '.join(names)} → 中期趋势边际转弱"})

    # 2.4 上证MA20 vs MA60 (即将死叉?)
    if sh_data['ma20'] is not None and sh_data['ma60'] is not None:
        gap = (sh_data['ma20'] / sh_data['ma60'] - 1) * 100
        if gap < 0:
            alerts.append({'level': 'danger', 'category': 'ma_cross',
                'msg': f"上证MA20({sh_data['ma20']:.0f})已下穿MA60({sh_data['ma60']:.0f}) → 中期空头排列"})
        elif gap < 1.0:
            alerts.append({'level': 'warning', 'category': 'ma_cross',
                'msg': f"上证MA20({sh_data['ma20']:.0f})逼近MA60({sh_data['ma60']:.0f})仅差{gap:.1f}% → 即将死叉"})

    # 2.5 MA60/MA200 death cross (滞后但可靠)
    if sh_data['ma60_ma200'] == 'below':
        # Check if this is new (crossed within last 5 days)
        if sh_data['days_below_ma200'] <= 5:
            alerts.append({'level': 'bear', 'category': 'death_cross',
                'msg': f"上证MA60下穿MA200(死叉) → ⚫ 长期趋势转熊信号"})

    # ── Tier 3: 综合判断 ──
    danger_count = sum(1 for a in alerts if a['level'] in ('danger', 'bear'))
    warning_count = sum(1 for a in alerts if a['level'] == 'warning')

    if danger_count >= 3:
        alerts.insert(0, {'level': 'bear', 'category': 'synthesis',
            'msg': f"⚫ 多项指标共振({danger_count}个危险信号+{warning_count}个警告) → 牛转熊概率>70%"})
    elif danger_count >= 2:
        alerts.insert(0, {'level': 'danger', 'category': 'synthesis',
            'msg': f"🔴 多项指标恶化({danger_count}个危险信号) → 牛转熊概率>50%, 建议仓位降至30%以下"})

    return alerts

# ═══════════════════════════════════════
# Report Formatter
# ═══════════════════════════════════════

def format_report(indices_data: list, margin: dict, volume: dict, alerts: list, quiet: bool = False):
    """格式化输出完整报告"""
    lines = []
    sep = '=' * 70

    # Header
    sh = next((i for i in indices_data if i['code'] == 'sh000001'), None)

    lines.append(sep)
    lines.append(f"  🐂🐻 市场牛熊状态监控  {date.today()}")
    lines.append(sep)

    # --- Overall verdict ---
    bear_alerts = [a for a in alerts if a['level'] == 'bear']
    danger_alerts = [a for a in alerts if a['level'] == 'danger']
    warning_alerts = [a for a in alerts if a['level'] == 'warning']
    notice_alerts = [a for a in alerts if a['level'] == 'notice']

    max_level = 'bear' if bear_alerts else ('danger' if danger_alerts else ('warning' if warning_alerts else ('notice' if notice_alerts else 'normal')))

    verdict_map = {
        'bear': ('⚫ 熊市确认', '建议: 空仓或仅保留防御性仓位, 等MA200重新站上'),
        'danger': ('🔴 牛转熊高风险', '建议: 仓位降至30%以下, 清掉所有破MA10持仓'),
        'warning': ('🟠 市场转弱', '建议: 收紧止损, 减仓至50%, 不新增仓位'),
        'notice': ('🟡 边际恶化', '建议: 关注变化, 暂不操作但保持警惕'),
        'normal': ('🟢 牛市健康', '建议: 正常持仓, 按策略纪律执行'),
    }

    verdict, suggestion = verdict_map[max_level]
    lines.append(f"\n  📌 综合判定: {verdict}")
    lines.append(f"  📋 {suggestion}")
    lines.append(f"  📊 信号统计: 熊市{len(bear_alerts)} | 危险{len(danger_alerts)} | 警告{len(warning_alerts)} | 关注{len(notice_alerts)}")

    # --- Index Summary ---
    lines.append(f"\n{'─' * 70}")
    lines.append("  📈 主要指数技术状态")
    lines.append(f"  {'指数':8s} {'收盘':>8s} {'vsMA20':>7s} {'vsMA60':>7s} {'vsMA200':>7s} {'52周回撤':>8s} {'趋势':12s}")
    lines.append(f"  {'─'*60}")

    for idx in indices_data:
        d20 = (idx['close'] / idx['ma20'] - 1) * 100 if idx['ma20'] else 0
        d60 = (idx['close'] / idx['ma60'] - 1) * 100 if idx['ma60'] else 0
        d200 = (idx['close'] / idx['ma200'] - 1) * 100 if idx['ma200'] else 0
        trend_emoji = {'bullish': '🟢多头', 'bullish_correction': '🟠多头回调',
                       'bearish': '🔴空头', 'bounce': '🟡反弹', 'mixed': '⚪震荡'}
        trend_str = trend_emoji.get(idx['trend'], idx['trend'])
        name = idx['name'][:6]
        lines.append(f"  {name:6s} {idx['close']:>8.0f} {d20:>+6.1f}% {d60:>+6.1f}% {d200:>+6.1f}% {idx['dd_52w']:>+7.1f}% {trend_str}")

    # --- Margin ---
    lines.append(f"\n{'─' * 70}")
    lines.append("  💰 资金面")
    if margin:
        cons = margin['cons_down_days']
        chg5 = margin['chg_5d']
        acc = '⚠️加速' if margin.get('accelerating') else ''
        lines.append(f"  融资余额: {margin['balance']/1e8:.0f}亿 | 5日{chg5:+.1f}% | 连降{cons}天 {acc}")
    if volume:
        lines.append(f"  成交量: {volume['ma5']/1e8:.0f}亿(5日均) | vs60日均: {volume['ratio_5_60']*100:.0f}% | 趋势: {volume['trend']}")

    # --- Key levels ---
    if sh:
        lines.append(f"\n{'─' * 70}")
        lines.append("  🎯 上证关键位")
        lines.append(f"  收盘: {sh['close']:.0f} | MA5={sh['ma5']:.0f} MA10={sh['ma10']:.0f} MA20={sh['ma20']:.0f} MA60={sh['ma60']:.0f} MA200={sh['ma200']:.0f}")
        lines.append(f"  支撑: 3900(心理) → {sh['ma200']:.0f}(MA200) → 3813(前低)")
        lines.append(f"  阻力: {sh['ma20']:.0f}(MA20) → {sh['ma60']:.0f}(MA60) → 4000(心理)")
        lines.append(f"  52周高: {sh['close']/(1+sh['dd_52w']/100):.0f}  回撤: {sh['dd_52w']:+.1f}%  连续破MA200: {sh['days_below_ma200']}天")

    # --- Alerts ---
    if alerts:
        lines.append(f"\n{'─' * 70}")
        lines.append("  🚨 预警信号")

        level_icons = {'bear': '⚫', 'danger': '🔴', 'warning': '🟠', 'notice': '🟡', 'error': '❌'}
        for a in alerts:
            icon = level_icons.get(a['level'], '•')
            cat = a.get('category', '')
            lines.append(f"  {icon} [{a['level'].upper():6s}] [{cat:12s}] {a['msg']}")
    else:
        lines.append(f"\n  ✅ 无预警信号, 市场状态健康")

    # --- Historical context ---
    lines.append(f"\n{'─' * 70}")
    lines.append("  📜 牛熊判断参考")
    lines.append("  ⚫ 熊市确认条件(满足任一条):")
    lines.append("    1. 上证<MA200持续20天以上")
    lines.append("    2. 上证从高点回撤>20%")
    lines.append("    3. MA60下穿MA200(死叉)")
    lines.append("    4. 融资连降15天+MA60破位")
    lines.append("  🔴 牛转熊高风险(满足任2条):")
    lines.append("    1. 上证<MA200持续5天以上")
    lines.append("    2. 上证从高点回撤>15%")
    lines.append("    3. 4+指数跌破MA60")
    lines.append("    4. 融资连降10天")
    lines.append("    5. 成交量<60日均量60%")
    lines.append(f"{'='*70}")

    return '\n'.join(lines)

# ═══════════════════════════════════════
# V反+超卖 → 暂缓卖出规则 (2026-07-15 新增)
# ═══════════════════════════════════════

def check_sell_override(stock_code: str, stock_name: str,
                        stock_price: float, stock_ma10: float,
                        stock_rsi: float, days_below_ma10: int,
                        market_v_reversal_pct: float = 0) -> dict:
    """
    检查是否应该暂缓MA10卖出。

    触发条件(全部满足):
      1. RSI < 30 (深度超卖, 反弹潜力大)
      2. 破MA10 ≥ 5天 (卖压已充分释放)
      3. 大盘日内V反 > 1.5% (情绪逆转)
      4. 股价在BOLL下轨附近 (技术支撑)

    返回: {'override': bool, 'reason': str, 'action': str}
    """
    override = False
    reasons = []
    conditions_met = 0

    # 条件1: RSI超卖
    if stock_rsi < 30:
        conditions_met += 1
        reasons.append(f'RSI={stock_rsi:.0f}深度超卖,反弹动能积蓄')

    # 条件2: 破MA10已充分
    if days_below_ma10 >= 5:
        conditions_met += 1
        reasons.append(f'破MA10已{days_below_ma10}天,卖压充分释放')

    # 条件3: 大盘V反
    if market_v_reversal_pct > 1.5:
        conditions_met += 1
        reasons.append(f'大盘V反{market_v_reversal_pct:+.1f}%,情绪逆转中')

    # 条件4: 距MA10幅度大,说明超跌
    ma10_distance = (stock_price / stock_ma10 - 1) * 100
    if ma10_distance < -8:
        conditions_met += 1
        reasons.append(f'距MA10={ma10_distance:+.1f}%,技术性超跌')

    if conditions_met >= 3:
        override = True
        action = (f'🟡 暂缓卖出1天: {stock_name}({stock_code}) 满足{conditions_met}/4个超卖+V反条件。'
                  f'观察明天能否借V反动能修复MA10。若明天仍站不回MA10,再执行清仓。')
    else:
        action = ''

    return {
        'override': override,
        'conditions_met': conditions_met,
        'reasons': reasons,
        'action': action,
    }

# ═══════════════════════════════════════
# Main Entry
# ═══════════════════════════════════════

def run_all(quiet: bool = False, json_out: bool = False):
    """运行完整分析, 返回 (verdict, alerts_json, report_text)"""
    # Gather data
    indices_data = []
    for code, name, typ in WATCH_INDICES:
        data = analyze_index_technical(code, name, typ)
        if data:
            indices_data.append(data)

    margin = analyze_margin()
    volume = analyze_volume()

    # Generate alerts
    alerts = generate_alerts(indices_data, margin, volume)

    # Determine max level
    levels = [a['level'] for a in alerts]
    max_level = 'bear' if 'bear' in levels else ('danger' if 'danger' in levels else (
        'warning' if 'warning' in levels else ('notice' if 'notice' in levels else 'normal')))

    # Build structured output
    sh = next((i for i in indices_data if i['code'] == 'sh000001'), None)
    result = {
        'date': str(date.today()),
        'verdict': max_level,
        'sh_close': sh['close'] if sh else None,
        'sh_dd_52w': sh['dd_52w'] if sh else None,
        'sh_days_below_ma200': sh['days_below_ma200'] if sh else None,
        'indices_below_ma60': sum(1 for i in indices_data if i['ma60'] and i['close'] < i['ma60']),
        'margin_cons_days': margin['cons_down_days'] if margin else None,
        'margin_chg_5d': margin['chg_5d'] if margin else None,
        'volume_ratio_60': volume['ratio_5_60'] if volume else None,
        'alerts': [{'level': a['level'], 'category': a['category'], 'msg': a['msg']} for a in alerts],
        'alert_count': {'bear': len([a for a in alerts if a['level']=='bear']),
                        'danger': len([a for a in alerts if a['level']=='danger']),
                        'warning': len([a for a in alerts if a['level']=='warning']),
                        'notice': len([a for a in alerts if a['level']=='notice'])},
    }

    # Output
    if json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif quiet:
        # Only output if there are warnings or worse
        if max_level in ('bear', 'danger', 'warning'):
            report = format_report(indices_data, margin, volume, alerts, quiet=True)
            print(report)
    else:
        report = format_report(indices_data, margin, volume, alerts)
        print(report)

    return max_level, result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='市场牛熊状态监控')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    parser.add_argument('--quiet', action='store_true', help='仅输出预警')
    args = parser.parse_args()

    try:
        run_all(quiet=args.quiet, json_out=args.json)
    except Exception as e:
        logger.error(f"监控异常: {e}")
        if args.json:
            print(json.dumps({'verdict': 'error', 'error': str(e)}, ensure_ascii=False))
        else:
            print(f"❌ 监控异常: {e}")
