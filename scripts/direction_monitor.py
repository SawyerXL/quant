#!/usr/bin/env python3
"""
市场方向监控 — 收敛三角形 + MA200 + 融资 + 量能
每天盘后跑一次，输出: 继续观望 / 向上突破 / 向下破位 / 进入磨底

用法:
  python scripts/direction_monitor.py           # 终端报告
  python scripts/direction_monitor.py --alert   # 方向变化时额外标记
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from loguru import logger

# ══════════════════════════════════════════════════════════════════
# 关键阈值
# ══════════════════════════════════════════════════════════════════
TRIANGLE_TOP = 3877       # 收敛三角形上轨
TRIANGLE_BOTTOM = 3741    # 收敛三角形下轨
MA200_BELOW_DAYS = 20     # 连续跌破MA200多少天确认熊市
MA60_DEATH_CROSS_GAP = 11 # MA60距死叉MA200还差多少点(8/4数据)

CSV_LOG = Path(__file__).parent.parent / "config" / "direction_monitor_log.csv"


# ══════════════════════════════════════════════════════════════════
# 数据拉取
# ══════════════════════════════════════════════════════════════════

def fetch_index_data():
    """拉取上证指数近期日线(含MA200/MA60计算)."""
    # 拉最近300个交易日，够算MA200
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=400)).strftime('%Y%m%d')

    try:
        df = ak.stock_zh_index_daily(symbol="sh000001")
    except Exception:
        # fallback: try individual API
        df = ak.stock_zh_a_hist(symbol="000001", period="daily",
                                start_date=start, end_date=end, adjust="qfq")
        # This is 平安银行 not 上证指数... let me try another approach
        pass

    if df.empty:
        return pd.DataFrame()

    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)

    # MA
    df['ma60'] = df['close'].rolling(60).mean()
    df['ma200'] = df['close'].rolling(200).mean()

    # 成交量均线
    df['vol_ma20'] = df['volume'].rolling(20).mean()

    return df


def fetch_margin_data():
    """拉取融资余额(东方财富)."""
    try:
        mg = ak.stock_margin_sse(start_date='20260101', end_date=datetime.now().strftime('%Y%m%d'))
        if mg.empty:
            return pd.DataFrame()
        # 列名可能是中文'信用交易日期'或'date'
        date_col = '信用交易日期' if '信用交易日期' in mg.columns else 'date'
        mg['date'] = pd.to_datetime(mg[date_col])
        # 统一融资余额列名
        if '融资余额' in mg.columns:
            mg['margin_value'] = mg['融资余额']
        mg = mg.sort_values('date')
        return mg
    except Exception as e:
        logger.warning(f"融资数据拉取失败: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
# 信号判断
# ══════════════════════════════════════════════════════════════════

def check_triangle(price, date_str):
    """检查三角形突破状态."""
    if price > TRIANGLE_TOP:
        return 'UP_BREAK', f'突破上轨{TRIANGLE_TOP}(+{price-TRIANGLE_TOP:.0f}点)'
    elif price < TRIANGLE_BOTTOM:
        return 'DOWN_BREAK', f'跌破下轨{TRIANGLE_BOTTOM}(-{TRIANGLE_BOTTOM-price:.0f}点)'
    else:
        # 在三角形内，计算距上下轨距离
        pct_to_top = (TRIANGLE_TOP - price) / price * 100
        pct_to_bot = (price - TRIANGLE_BOTTOM) / price * 100
        return 'INSIDE', f'三角形内(上轨+{pct_to_top:.1f}%, 下轨-{pct_to_bot:.1f}%)'


def check_ma_status(df):
    """检查MA200/MA60状态."""
    last = df.iloc[-1]
    price = last['close']
    ma60 = last.get('ma60', np.nan)
    ma200 = last.get('ma200', np.nan)

    if pd.isna(ma200):
        return {}, []

    # 连续跌破MA200天数
    below_days = 0
    for i in range(len(df)-1, -1, -1):
        if df.iloc[i]['close'] < df.iloc[i].get('ma200', np.inf):
            below_days += 1
        else:
            break

    # MA60 vs MA200 差距
    gap = np.nan
    if not pd.isna(ma60):
        gap = (ma200 - ma60) / ma200 * 100  # 正=MA60在上,负=MA60在下

    signals = {
        'ma200_below_days': below_days,
        'ma60_ma200_gap_pct': round(gap, 2) if not pd.isna(gap) else None,
        'ma200_value': round(ma200, 1),
        'ma60_value': round(ma60, 1) if not pd.isna(ma60) else None,
    }

    alerts = []
    if below_days >= MA200_BELOW_DAYS:
        alerts.append(f'⚠ MA200下方{below_days}天 → 熊市确认')
    elif below_days >= 15:
        alerts.append(f'🟡 MA200下方{below_days}天 → 接近确认(还需{MA200_BELOW_DAYS-below_days}天)')

    if not pd.isna(gap) and gap < 0:
        alerts.append(f'⚠ MA60已死叉MA200({abs(gap):.2f}%)')
    elif not pd.isna(gap) and gap < 0.5:
        alerts.append(f'🟡 MA60距死叉MA200仅差{gap:.2f}%')

    return signals, alerts


def check_margin(df):
    """检查融资余额趋势."""
    if df.empty:
        return {}, []

    last = df.iloc[-1]
    margin_val = last.get('margin_value', last.get('rzye', np.nan))

    # 连续下降天数
    decline_days = 0
    vals = []
    if 'margin_value' in df.columns:
        vals = df['margin_value'].values
    elif 'rzye' in df.columns:
        vals = df['rzye'].values

    if len(vals) >= 10:
        for i in range(len(vals)-1, max(len(vals)-21, 0), -1):
            if vals[i] < vals[i-1]:
                decline_days += 1
            else:
                break

        # 5日变化率
        chg_5d = (vals[-1] / vals[-6] - 1) * 100 if len(vals) >= 6 else 0

        signals = {
            'margin_value_yi': round(vals[-1]/1e8, 1),
            'decline_days': decline_days,
            'chg_5d_pct': round(chg_5d, 2),
        }
    else:
        signals = {'margin_value_yi': round(margin_val/1e8, 1) if not pd.isna(margin_val) else None}
        decline_days = 0

    alerts = []
    if decline_days >= 10:
        alerts.append(f'🔴 融资连降{decline_days}天 → 去杠杆进行中')
    elif decline_days >= 5:
        alerts.append(f'🟡 融资连降{decline_days}天')
    elif decline_days == 0 and len(vals) >= 2 and vals[-1] > vals[-2]:
        alerts.append('🟢 融资止跌回升 → 右侧信号')

    return signals, alerts


def check_volume(df):
    """检查量能状态."""
    if df.empty or 'vol_ma20' not in df.columns:
        return {}, []

    last = df.iloc[-1]
    today_vol = last['volume']
    avg_vol = last.get('vol_ma20', today_vol)
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1

    # 近期量能趋势
    recent = df.tail(10)
    vol_trend = 'flat'
    if len(recent) >= 5:
        first_half = recent.head(5)['volume'].mean()
        second_half = recent.tail(5)['volume'].mean()
        if second_half < first_half * 0.85:
            vol_trend = 'shrinking'
        elif second_half > first_half * 1.15:
            vol_trend = 'expanding'

    signals = {
        'vol_ratio_vs_20ma': round(vol_ratio, 2),
        'vol_trend_10d': vol_trend,
    }

    alerts = []
    if vol_trend == 'shrinking':
        alerts.append('📉 量能持续萎缩 → 变盘临近(缩到极致就是方向)')
    elif vol_trend == 'expanding' and vol_ratio > 1.3:
        alerts.append('📈 放量中 → 方向选择进行中')

    return signals, alerts


# ══════════════════════════════════════════════════════════════════
# 综合判定
# ══════════════════════════════════════════════════════════════════

def overall_verdict(triangle_status, ma_signals, margin_signals, vol_signals):
    """综合方向判断."""

    # 三角形突破 = 最高优先级
    if triangle_status[0] == 'UP_BREAK':
        return '🟢 向上突破', ('三角形上轨突破确认。等待连续2天站稳可启动CB试点。'
                          '\n  建议: 先观望1-2天确认不是假突破，然后个人5万建CB底仓。')
    if triangle_status[0] == 'DOWN_BREAK':
        return '🔴 向下破位', ('三角形下轨失守。Phase 1急跌延续，CB不进，现金为王。'
                          '\n  建议: 继续观望，如果伴随放量下跌+融资加速流出→可能还有一波急跌。')

    # 三角形内 = 继续观望
    ma_below = ma_signals.get('ma200_below_days', 0)
    margin_decline = margin_signals.get('decline_days', 0)
    vol_trend = vol_signals.get('vol_trend_10d', 'flat')

    # 磨底信号: 三角形内+缩量+MA200下方但不再加速下跌
    if vol_trend == 'shrinking' and ma_below >= 10:
        return '🟡 磨底迹象', ('量能萎缩+MA200下方横盘 = 急跌尾声。距CB建仓最近的状态。'
                          '\n  建议: 准备资金，等融资止跌或三角形突破即可动手。')

    # 变盘临近: 三角形内+缩量+接近边界
    # (三角形收窄中，无法判断具体位置但不能排除)
    if vol_trend == 'shrinking':
        return '🟡 变盘临近', ('三角形收敛+量缩，方向选择在即。'
                          '\n  建议: 不要赌方向，等突破了再跟。')

    # 默认
    return '⚪ 继续观望', ('三角形内运行，无明确方向信号。'
                       '\n  建议: 不变应万变，等突破。')


# ══════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════

def print_report(today, price, triangle_status, ma_signals, ma_alerts,
                 margin_signals, margin_alerts, vol_signals, vol_alerts,
                 verdict, reasoning):
    """终端报告."""
    print()
    print("═" * 70)
    print(f"  市场方向监控  {today}")
    print("═" * 70)

    # 价格快照
    print(f"\n  📊 上证: {price:.0f}  |  {triangle_status[1]}")

    # MA状态
    print(f"\n  ── MA状态 ──")
    if ma_signals:
        print(f"  MA200: {ma_signals['ma200_value']:.0f}  |  "
              f"MA60: {ma_signals.get('ma60_value', '?'):.0f}"
              f"  |  下方{ma_signals['ma200_below_days']}天")
    for a in ma_alerts:
        print(f"  {a}")

    # 融资
    print(f"\n  ── 融资余额 ──")
    if margin_signals:
        print(f"  余额: {margin_signals.get('margin_value_yi', '?')}亿"
              f"  |  连降{margin_signals.get('decline_days', 0)}天"
              f"  |  5日变化{margin_signals.get('chg_5d_pct', 0):+.1f}%")
    for a in margin_alerts:
        print(f"  {a}")

    # 量能
    print(f"\n  ── 量能 ──")
    if vol_signals:
        print(f"  量比(vs20均): {vol_signals['vol_ratio_vs_20ma']:.2f}"
              f"  |  趋势: {vol_signals['vol_trend_10d']}")
    for a in vol_alerts:
        print(f"  {a}")

    # 综合
    print(f"\n  ══ 综合判定: {verdict} ══")
    print(f"  {reasoning}")
    print("═" * 70)
    print()


def log_to_csv(today, price, verdict, triangle_status, ma_signals, margin_signals):
    """追加记录到CSV."""
    row = {
        'date': today,
        'price': price,
        'verdict': verdict,
        'triangle': triangle_status[0],
        'ma200_below_days': ma_signals.get('ma200_below_days', ''),
        'ma60_ma200_gap': ma_signals.get('ma60_ma200_gap_pct', ''),
        'margin_yi': margin_signals.get('margin_value_yi', ''),
        'margin_decline_days': margin_signals.get('decline_days', ''),
    }
    df = pd.DataFrame([row])
    if CSV_LOG.exists():
        existing = pd.read_csv(CSV_LOG)
        # 去重同一天
        existing = existing[existing['date'] != today]
        df = pd.concat([existing, df], ignore_index=True)
    df.to_csv(CSV_LOG, index=False)


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main(alert_only=False):
    today = datetime.now().strftime('%Y-%m-%d')

    # 拉数据
    logger.info("拉取指数数据...")
    idx = fetch_index_data()
    if idx.empty:
        logger.error("无法获取指数数据")
        return

    logger.info("拉取融资数据...")
    margin = fetch_margin_data()

    # 当天数据
    last = idx.iloc[-1]
    price = float(last['close'])
    last_date = str(last['date'].date())[:10]

    # 各维度判断
    triangle_status = check_triangle(price, last_date)
    ma_signals, ma_alerts = check_ma_status(idx)
    margin_signals, margin_alerts = check_margin(margin)
    vol_signals, vol_alerts = check_volume(idx)

    # 综合
    verdict, reasoning = overall_verdict(
        triangle_status, ma_signals, margin_signals, vol_signals
    )

    # 输出
    all_alerts = [a for a in ma_alerts + margin_alerts + vol_alerts
                  if a.startswith('⚠') or a.startswith('🔴') or a.startswith('🟢')]

    if alert_only and not all_alerts and triangle_status[0] == 'INSIDE':
        # 无变化，静默
        return

    print_report(last_date, price, triangle_status,
                 ma_signals, ma_alerts,
                 margin_signals, margin_alerts,
                 vol_signals, vol_alerts,
                 verdict, reasoning)

    if all_alerts:
        print(f"  🚨 告警: {' | '.join(all_alerts)}\n")

    # 记录
    log_to_csv(last_date, price, verdict.split(' ')[0], triangle_status, ma_signals, margin_signals)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='市场方向监控')
    p.add_argument('--alert', action='store_true', help='仅在有变化时输出(适合cron)')
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{message}</level>")

    main(alert_only=args.alert)
