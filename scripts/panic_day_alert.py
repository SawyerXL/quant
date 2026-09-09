"""
恐慌日加仓做T提醒 — 盘中/盘后检测恐慌信号 + 持仓候选筛选

用法:
  python scripts/panic_day_alert.py              # 检测今天是否恐慌日
  python scripts/panic_day_alert.py --intraday   # 盘中模式(14:30后检测)
  python scripts/panic_day_alert.py --date 2026-07-17  # 指定日期
  python scripts/panic_day_alert.py --send       # 发送邮件提醒

恐慌日定义:
  上证单日跌幅 >2.5%  且  振幅 >2.0%

加仓候选条件(全部满足):
  1. 已在持仓列表中
  2. 当日跟随大盘下跌(>2%)
  3. 仍在MA10上方(不要加仓已经破位的)
  4. 非跌停
  5. 在TOP60池内优先

回测结论:
  恐慌日买入 → 次日卖出: 胜率57%, 中位数+0.26%
  持有超过2天胜率下降, T+3中位数转负
"""

import sys, os, argparse, json
from pathlib import Path
from datetime import date, datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger

# ── 配置 ──
PANIC_DROP_THRESHOLD = -2.5     # 上证单日跌幅阈值(%)
PANIC_AMP_THRESHOLD = 2.0       # 振幅阈值(%)
CANDIDATE_MIN_DROP = -2.0       # 候选股当日最低跌幅(%)
MAX_HOLD_DAYS = 1               # 做T持有天数(回测最优)

# 用户持仓(与 my_holdings.csv 同步)
PORTFOLIO = {
    '600030': '中信证券', '601899': '紫金矿业', '603259': '药明康德',
    '603993': '洛阳钼业', '603392': '万泰生物', '001965': '招商公路',
    '002714': '牧原股份', '601138': '工业富联', '603156': '养元饮品',
    '002049': '紫光国微', '002559': '亚威股份', '603019': '中科曙光',
}


def get_index_data():
    """获取上证指数日线"""
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol='sh000001')
    df = df.sort_values('date')
    df['chg_pct'] = df['close'].pct_change() * 100
    df['amp'] = (df['high'] - df['low']) / df['open'] * 100
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma10'] = df['close'].rolling(10).mean()
    df['ma200'] = df['close'].rolling(200).mean()
    return df


def get_intraday_snapshot():
    """获取盘中实时数据（新浪接口）"""
    import urllib.request
    snap = {}
    # 上证指数
    try:
        url = 'https://hq.sinajs.cn/list=sh000001'
        req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode('gbk')
        parts = data.split('"')[1].split(',')
        snap['index'] = {
            'name': parts[0],
            'open': float(parts[1]),
            'prev_close': float(parts[2]),
            'price': float(parts[3]),
            'high': float(parts[4]),
            'low': float(parts[5]),
            'chg_pct': (float(parts[3]) - float(parts[2])) / float(parts[2]) * 100,
            'amp': (float(parts[4]) - float(parts[5])) / float(parts[1]) * 100,
        }
    except Exception as e:
        logger.warning(f'获取上证实时失败: {e}')
        return None

    # 持仓个股实时
    snap['stocks'] = {}
    for code in PORTFOLIO:
        try:
            market = 'sh' if code.startswith(('6','9')) else 'sz'
            url = f'https://hq.sinajs.cn/list={market}{code}'
            req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read().decode('gbk')
            parts = data.split('"')[1].split(',')
            if len(parts) > 3 and float(parts[3]) > 0:
                snap['stocks'][code] = {
                    'name': parts[0],
                    'open': float(parts[1]),
                    'prev_close': float(parts[2]),
                    'price': float(parts[3]),
                    'high': float(parts[4]),
                    'low': float(parts[5]),
                    'chg_pct': (float(parts[3]) - float(parts[2])) / float(parts[2]) * 100,
                }
        except:
            pass

    return snap


def check_intraday_panic(snap):
    """检查盘中的恐慌状态"""
    if snap is None or 'index' not in snap:
        return False, None

    idx = snap['index']
    chg = idx['chg_pct']
    amp = idx['amp']

    info = {
        'date': str(date.today()),
        'price': round(idx['price'], 0),
        'chg_pct': round(chg, 2),
        'amp': round(amp, 2),
        'low': round(idx['low'], 0),
        'intraday': True,
    }
    if chg < PANIC_DROP_THRESHOLD and amp > PANIC_AMP_THRESHOLD:
        return True, info
    return False, info


def find_intraday_candidates(snap):
    """从盘中快照筛选加仓候选"""
    candidates = []
    if snap is None or 'stocks' not in snap:
        return candidates

    for code, s in snap['stocks'].items():
        name = PORTFOLIO.get(code, s.get('name', ''))
        chg = s['chg_pct']
        # 盘中不判断MA10（需要日线数据），放宽条件
        if chg > CANDIDATE_MIN_DROP:
            continue
        if chg < -9.5:
            continue  # 接近跌停不加

        score = min(abs(chg), 8) * 10 + (20 if chg < -5 else 0)
        candidates.append({
            'code': code,
            'name': name,
            'price': round(s['price'], 2),
            'chg_pct': round(chg, 2),
            'score': score,
            'action': f'加仓{name}({code}) → 次日反弹T+1卖出',
            'risk': '次日若继续跌>3%止损, 不持有超过2天',
        })

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates


def is_panic_day(index_df, target_date=None):
    """判断是否为恐慌日"""
    if target_date is None:
        last = index_df.iloc[-1]
    else:
        from datetime import date as dt_date
        if isinstance(target_date, str):
            target_date = dt_date.fromisoformat(target_date)
        match = index_df[index_df['date'] == target_date]
        if len(match) == 0:
            # 尝试字符串匹配
            match = index_df[index_df['date'].astype(str) == str(target_date)]
        if len(match) == 0:
            return None, None
        last = match.iloc[-1]

    chg = last['chg_pct']
    amp = last['amp']

    if chg < PANIC_DROP_THRESHOLD and amp > PANIC_AMP_THRESHOLD:
        return True, {
            'date': str(last['date'])[:10],
            'close': float(last['close']),
            'chg_pct': round(float(chg), 2),
            'amp': round(float(amp), 2),
            'low': float(last['low']),
            'ma200': float(last.get('ma200', 0)),
        }
    return False, None


def get_stock_snapshot(code, target_date=None):
    """获取个股日线快照"""
    import akshare as ak
    try:
        df = ak.stock_zh_a_hist(symbol=code, period='daily', adjust='qfq')
        df = df.sort_values('日期')
        df['chg_pct'] = df['收盘'].pct_change() * 100
        df['MA10'] = df['收盘'].rolling(10).mean()

        if target_date:
            from datetime import date as dt_date
            if isinstance(target_date, str):
                target_date = dt_date.fromisoformat(target_date)
            match = df[df['日期'] == target_date]
            if len(match) == 0:
                match = df[df['日期'].astype(str) == str(target_date)]
            if len(match) == 0:
                return None
            row = match.iloc[-1]
        else:
            row = df.iloc[-1]

        return {
            'code': code,
            'close': float(row['收盘']),
            'chg_pct': round(float(row['chg_pct']), 2),
            'ma10': float(row.get('MA10', 0)),
            'above_ma10': row['收盘'] > row.get('MA10', 0),
            'limit_down': float(row['chg_pct']) < -9.5,
        }
    except Exception as e:
        logger.warning(f'获取{code}失败: {e}')
        return None


def find_candidates(index_info):
    """筛选加仓候选"""
    candidates = []
    target_date = index_info['date'] if index_info else None

    for code, name in PORTFOLIO.items():
        snap = get_stock_snapshot(code, target_date)
        if snap is None:
            continue

        # 筛选条件
        if snap['chg_pct'] > CANDIDATE_MIN_DROP:
            continue  # 跌幅不够
        if not snap['above_ma10']:
            continue  # MA10已破，不加仓（关键纪律）
        if snap['limit_down']:
            continue  # 跌停不加（无法买入/次日大概率低开）

        # 评分(越高越好): 跌幅深 + MA10上方 + 大盘共振
        score = 0
        score += min(abs(snap['chg_pct']), 8) * 10  # 跌幅分(最深8%)
        score += 20 if snap['above_ma10'] else 0
        score += 10 if snap['chg_pct'] < -5 else 0  # 深跌加分

        candidates.append({
            **snap,
            'name': name,
            'score': score,
            'action': f'加仓{name}({code}) → 明日反弹T+1卖出',
            'risk': '次日若继续跌>3%止损, 不持有超过2天',
        })

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates


def generate_alert(index_info, candidates):
    """生成提醒文本"""
    if not index_info:
        return None

    lines = []
    lines.append(f"🚨 恐慌日加仓提醒 — {index_info['date']}")
    lines.append(f"")
    lines.append(f"上证: {index_info['close']:.0f}  跌幅{index_info['chg_pct']:.1f}%  振幅{index_info['amp']:.1f}%")
    lines.append(f"判定: 🔴 恐慌日 → 加仓做T机会 (胜率57% 中位+0.26%)")
    lines.append(f"")

    if not candidates:
        lines.append("⚠️ 无符合条件的加仓候选。")
        lines.append("原因: 持仓股要么跌停/要么已破MA10/要么跌幅不够")
        lines.append("建议: 恐慌日不加仓破位票，不要摊平亏损")
    else:
        lines.append(f"📋 加仓候选 ({len(candidates)}只, 按优先级):")
        lines.append(f"")
        for i, c in enumerate(candidates, 1):
            tag = '🔥' if c['score'] > 100 else '✅' if c['score'] > 70 else '🟡'
            lines.append(f"  {i}. {tag} {c['name']}({c['code']})")
            lines.append(f"     今跌{c['chg_pct']:.1f}%  MA10: {'✅上方' if c['above_ma10'] else '❌已破'}  {'⚠️跌停!' if c['limit_down'] else ''}")
            lines.append(f"     操作: {c['action']}")
            lines.append(f"     风险: {c['risk']}")
            lines.append(f"")

    lines.append(f"───")
    lines.append(f"纪律: 恐慌日加仓 → T+1必须卖出 → 不持有超过2天")
    lines.append(f"回测: 2019-2026共452次, T+1胜率57%, 中位数+0.26%")
    lines.append(f"反例: 如果明天继续大跌>3% → 止损, 不扛")

    return '\n'.join(lines)


def send_alert(text, to_email=None):
    """发送邮件"""
    try:
        from monitoring.alerts import _send_email
        subject = f"🚨 恐慌日加仓提醒 {date.today()}"
        _send_email(subject, text, to=to_email)
        logger.info(f"恐慌日提醒已发送")
        return True
    except Exception as e:
        logger.error(f'邮件发送失败: {e}')
        return False


def generate_intraday_alert(index_info, candidates):
    """生成盘中提醒文本"""
    lines = []
    lines.append(f"🚨 盘中恐慌警报 — {index_info['date']}")
    lines.append(f"")
    lines.append(f"上证实时: {index_info['price']:.0f}  跌幅{index_info['chg_pct']:.1f}%  振幅{index_info['amp']:.1f}%")
    lines.append(f"判定: 🔴 盘中恐慌 → 收盘前加仓做T机会 (胜率57%)")
    lines.append(f"⏰ 剩余交易时间约30分钟，请尽快操作")
    lines.append(f"")

    if not candidates:
        lines.append("⚠️ 无符合条件的加仓候选")
        lines.append("(持仓股跌幅不够/接近跌停)")
    else:
        lines.append(f"📋 加仓候选 ({len(candidates)}只):")
        for i, c in enumerate(candidates, 1):
            tag = '🔥' if c['score'] > 100 else '✅' if c['score'] > 70 else '🟡'
            lines.append(f"  {i}. {tag} {c['name']}({c['code']}) 跌{c['chg_pct']:.1f}%  ¥{c['price']}")
            lines.append(f"     → 收盘前买入，明日反弹卖出")

    lines.append(f"\n───")
    lines.append(f"纪律: 收盘前买入 → T+1卖出 → 不持有超过2天")
    return '\n'.join(lines)


def run(target_date=None, send=False, intraday=False):
    """主逻辑"""
    if intraday:
        # 盘中模式：用新浪实时数据
        snap = get_intraday_snapshot()
        if snap is None:
            print("❌ 无法获取盘中数据（可能非交易时段）")
            return None
        is_panic, info = check_intraday_panic(snap)
        if not is_panic:
            print(f"✅ 盘中无恐慌 (现跌{info['chg_pct']:.1f}%)")
            return None
        candidates = find_intraday_candidates(snap)
        alert = generate_intraday_alert(info, candidates)
        print(alert)
        if send:
            try:
                from monitoring.alerts import _send_email
                _send_email(f"🚨 盘中恐慌! {info['date']}", alert)
                logger.info("盘中恐慌提醒已发送")
            except Exception as e:
                logger.error(f'发送失败: {e}')
        return {'info': info, 'candidates': candidates, 'alert': alert}

    # 日线模式（盘后回测/复盘用）
    df = get_index_data()
    is_panic, info = is_panic_day(df, target_date)

    if is_panic is None:
        print(f"❌ 未找到 {target_date} 的数据")
        return None

    if not is_panic:
        today = target_date or str(df['date'].iloc[-1])[:10]
        last = df.iloc[-1]
        print(f"✅ {today} 非恐慌日 (跌幅{last['chg_pct']:.1f}%) — 无需加仓做T")
        return None

    candidates = find_candidates(info)
    alert = generate_alert(info, candidates)
    print(alert)
    if send:
        send_alert(alert)

    return {'info': info, 'candidates': candidates, 'alert': alert}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='恐慌日加仓做T提醒')
    parser.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    parser.add_argument('--send', action='store_true', help='发送邮件')
    parser.add_argument('--intraday', action='store_true', help='盘中实时模式')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    args = parser.parse_args()

    if args.json:
        result = run(target_date=args.date, send=args.send, intraday=args.intraday)
        if result:
            print(json.dumps(result['info'], ensure_ascii=False))
    else:
        run(target_date=args.date, send=args.send, intraday=args.intraday)
