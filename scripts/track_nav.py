"""
每日净值追踪 — 记录持仓市值+现金, 计算绩效指标

用法:
  python scripts/track_nav.py              # 记录今日净值
  python scripts/track_nav.py --report     # 生成绩效报告
"""

import sys, os, json, argparse
from pathlib import Path
from datetime import date, datetime
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

NAV_FILE = Path(__file__).parent.parent / 'data' / 'nav_history.json'

# 个人账户初始
INITIAL_CAPITAL = 207_000


def get_today_portfolio():
    """获取当前持仓市值（从 my_holdings.csv 实时读取）"""
    import urllib.request, csv

    csv_path = Path(__file__).parent.parent / 'config' / 'my_holdings.csv'
    total_market = 0
    n_positions = 0
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('monitor', 'True') != 'True':
                continue
            try:
                shares = int(float(row['shares']))
            except (ValueError, TypeError):
                continue
            if shares <= 0:
                continue
            code = str(row['code']).zfill(6)
            n_positions += 1
            try:
                # 三板固定价
                if code == '400286':
                    total_market += shares * 0.35
                    continue
                # 可转债
                if code.startswith(('11','12')) or code.startswith('71'):
                    mkt = 'sh' if code.startswith(('11','71')) else 'sz'
                    url = f'https://hq.sinajs.cn/list={mkt}{code}'
                    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
                    resp = urllib.request.urlopen(req, timeout=5)
                    data = resp.read().decode('gbk')
                    parts = data.split('"')[1].split(',')
                    price = float(parts[3])
                    total_market += shares * price
                    continue
                mkt = 'sh' if code.startswith(('5','6','9')) else 'sz'
                url = f'https://hq.sinajs.cn/list={mkt}{code}'
                req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
                resp = urllib.request.urlopen(req, timeout=5)
                data = resp.read().decode('gbk')
                parts = data.split('"')[1].split(',')
                price = float(parts[3])
                total_market += shares * price
            except Exception:
                pass

    # 现金从nav_history上一条记录继承，首次默认0
    prev_cash = 0
    if NAV_FILE.exists():
        try:
            hist = json.loads(NAV_FILE.read_text())
            if hist:
                prev_cash = hist[-1].get('cash', 0)
        except Exception:
            pass

    return {
        'date': str(date.today()),
        'market_value': round(total_market, 2),
        'cash': prev_cash,  # 继承上日现金
        'total_equity': round(total_market + prev_cash, 2),
        'n_positions': n_positions,
    }


def load_nav_history():
    if NAV_FILE.exists():
        return json.loads(NAV_FILE.read_text())
    return []


def save_nav(record):
    history = load_nav_history()
    # 检查是否已记录今天
    today = str(date.today())
    history = [r for r in history if r.get('date') != today]
    history.append(record)
    history.sort(key=lambda x: x['date'])
    NAV_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history


def calc_metrics(history):
    """计算绩效指标"""
    if len(history) < 2:
        return None

    df = pd.DataFrame(history)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')

    equity = df['total_equity'].values
    dates = df['date'].values

    # 累计收益
    total_return = (equity[-1] / INITIAL_CAPITAL - 1) * 100

    # 日收益率
    daily_rets = np.diff(equity) / equity[:-1]

    # 年化收益
    days = (dates[-1] - dates[0]).astype('timedelta64[D]').astype(int)
    if days > 0:
        annual_return = ((1 + total_return/100) ** (365/days) - 1) * 100
    else:
        annual_return = 0

    # 夏普 (假设无风险利率2%)
    if len(daily_rets) > 1:
        rf_daily = 0.02 / 252
        excess = daily_rets - rf_daily
        sharpe = np.sqrt(252) * excess.mean() / excess.std() if excess.std() > 0 else 0
    else:
        sharpe = 0

    # 最大回撤
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100
    max_dd = dd.min()

    # 胜率 (日)
    win_rate = (daily_rets > 0).sum() / len(daily_rets) * 100 if len(daily_rets) > 0 else 0

    # 盈亏比
    avg_win = daily_rets[daily_rets > 0].mean() if (daily_rets > 0).any() else 0
    avg_loss = abs(daily_rets[daily_rets < 0].mean()) if (daily_rets < 0).any() else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    return {
        'total_return': round(total_return, 2),
        'annual_return': round(annual_return, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'days': days,
        'trading_days': len(history),
        'start_date': str(dates[0])[:10],
        'end_date': str(dates[-1])[:10],
        'final_equity': round(equity[-1], 0),
        'initial_capital': INITIAL_CAPITAL,
    }


def run(record=False, report=False):
    if record:
        snap = get_today_portfolio()
        history = save_nav(snap)
        print(f'✅ 净值已记录: {snap["date"]} 总权益 ¥{snap["total_equity"]:,.0f}')
        print(f'   市值 ¥{snap["market_value"]:,.0f} + 现金 ¥{snap["cash"]:,.0f}')
        return snap

    if report:
        history = load_nav_history()
        if len(history) < 2:
            print('❌ 数据不足，至少需要2个交易日')
            return

        m = calc_metrics(history)
        if m is None:
            print('❌ 无法计算')
            return

        print(f'{"="*50}')
        print(f'  绩效报告  {m["start_date"]} → {m["end_date"]}')
        print(f'{"="*50}')
        print(f'  交易日: {m["trading_days"]}天 ({m["days"]}日历天)')
        print(f'')
        print(f'  初始资金:  ¥{m["initial_capital"]:,.0f}')
        print(f'  最终权益:  ¥{m["final_equity"]:,.0f}')
        print(f'  累计收益:  {m["total_return"]:+.2f}%')
        print(f'  年化收益:  {m["annual_return"]:+.2f}%')
        print(f'  夏普比率:  {m["sharpe"]:.2f}')
        print(f'  最大回撤:  {m["max_drawdown"]:+.2f}%')
        print(f'  日胜率:    {m["win_rate"]:.1f}%')
        print(f'  盈亏比:    {m["profit_factor"]:.2f}')

        # 最近每日
        df = pd.DataFrame(history)
        print(f'\n{"="*50}')
        print(f'  每日净值')
        print(f'{"="*50}')
        for _, r in df.tail(10).iterrows():
            chg = ''
            print(f'  {r["date"]}  ¥{r["total_equity"]:>10,.0f}  {chg}')

        return m


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--record', action='store_true', help='记录今日净值')
    parser.add_argument('--report', action='store_true', help='生成绩效报告')
    args = parser.parse_args()

    if not args.record and not args.report:
        args.report = True  # 默认显示报告

    run(record=args.record, report=args.report)
