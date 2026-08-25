"""
外盘领先信号 → A股持仓 的可执行性回测（通用版）。

用法: python scripts/backtest_overseas_signal.py [sox|copper|nbi|hsi]

方法论（来自 2026-08-24 铜信号回测的教训，别改）：
  1. 先实测日期对齐 —— 外盘bar标D，到底对应A股D还是D+1，用相关系数定，不靠推理
  2. 必须把次日收益拆成 跳空(昨收→今开) + 盘中(今开→今收)
     signal_alerts 的建议是"开盘减仓"，那就只有盘中段可规避，跳空是躲不掉的。
     用收→收衡量开盘操作会凭空造出成倍的假收益（铜那次算出93%年化，真实39%）
  3. 扣交易成本后再下结论
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily

TRADING_DAYS = 243
COST_STOCK = 0.20   # % 双边：印花税0.05 + 佣金0.05 + 滑点0.10
COST_ETF = 0.15     # % ETF免印花税

SIGNALS = {
    'sox':    {'kind': 'us_index', 'symbol': '.SOX', 'thresh': 3.0, 'name': 'SOX费半',
               'stocks': {'588000': '科创50ETF', '603019': '中科曙光', '002409': '雅克科技'}},
    'nbi':    {'kind': 'us_index', 'symbol': '.NBI', 'thresh': 2.0, 'name': 'NBI生科',
               'stocks': {'603259': '药明康德', '600276': '恒瑞医药'}},
    'copper': {'kind': 'futures', 'symbol': 'CAD', 'thresh': 2.0, 'name': '伦铜LME',
               'stocks': {'601899': '紫金矿业', '603993': '洛阳钼业'}},
    'gold':   {'kind': 'futures', 'symbol': 'GC', 'thresh': 2.0, 'name': '纽约金',
               'stocks': {'601899': '紫金矿业'}},
    'hsi':    {'kind': 'hk_index', 'symbol': 'HSI', 'thresh': 2.0, 'name': '恒生',
               'stocks': {'588000': '科创50ETF', '603019': '中科曙光'}},
}

ETF_SINA = {'588000': 'sh588000'}   # 本地日线库不含ETF，走新浪


def signal_ret(cfg) -> pd.Series:
    kind, sym = cfg['kind'], cfg['symbol']
    if kind == 'us_index':
        df = ak.index_us_stock_sina(symbol=sym)
    elif kind == 'futures':
        df = ak.futures_foreign_hist(symbol=sym)
    elif kind == 'hk_index':
        df = ak.stock_hk_index_daily_sina(symbol=sym)
    df = df.sort_values('date')
    df['date'] = pd.to_datetime(df['date'])
    s = df.set_index('date')['close'].astype(float)
    s = s[~s.index.duplicated(keep='last')]
    return (s.pct_change() * 100).dropna()


def stock_frame(code: str) -> pd.DataFrame:
    """返回 ret(收→收) / gap(昨收→今开) / intra(今开→今收)，单位%。"""
    if code in ETF_SINA:
        d = ak.fund_etf_hist_sina(symbol=ETF_SINA[code])
        d['date'] = pd.to_datetime(d['date'])
        d = d.set_index('date').sort_index()
    else:
        dfs = []
        for y in range(2019, 2027):
            x = load_daily(code, f'{y}-01-01', f'{y}-12-31')
            if not x.empty:
                dfs.append(x)
        d = pd.concat(dfs)
        d['date'] = pd.to_datetime(d['date'])
        d = d.set_index('date').sort_index()
    d = d[~d.index.duplicated(keep='last')][['open', 'close']].astype(float).dropna()
    f = pd.DataFrame({
        'ret': d['close'].pct_change() * 100,
        'gap': (d['open'] / d['close'].shift(1) - 1) * 100,
        'intra': (d['close'] / d['open'] - 1) * 100,
    }).dropna()
    return f[np.isfinite(f).all(axis=1)]


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 else np.nan


def perf(pct: pd.Series) -> dict:
    r = pct.dropna() / 100
    eq = (1 + r).cumprod()
    yrs = len(r) / TRADING_DAYS
    return {'cagr': (eq.iloc[-1] ** (1 / yrs) - 1) * 100,
            'sharpe': r.mean() / r.std(ddof=1) * np.sqrt(TRADING_DAYS),
            'mdd': (eq / eq.cummax() - 1).min() * 100}


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else 'sox'
    cfg = SIGNALS[key]
    sig = signal_ret(cfg)
    th = cfg['thresh']
    print(f"信号 {cfg['name']}({cfg['symbol']})  {len(sig)}根  "
          f"{sig.index[0].date()}→{sig.index[-1].date()}  阈值±{th}%\n")

    for code, name in cfg['stocks'].items():
        f = stock_frame(code)
        ret, gap, intra = f['ret'], f['gap'], f['intra']
        cost = COST_ETF if code in ETF_SINA else COST_STOCK
        print("=" * 80)
        print(f"{name} {code}   {len(ret)}天 {ret.index[0].date()}→{ret.index[-1].date()}  "
              f"双边成本{cost}%")
        bad = int((ret.abs() > 11).sum())
        if bad:
            print(f"  ⚠ 单日涨跌>11%的bar {bad}根（疑似脏数据/复权跳变）")

        c = sig.reindex(ret.index)
        print(f"\n【日期对齐】{cfg['name']}当日 vs A股收益相关系数")
        same, nxt = c.corr(ret), c.corr(ret.shift(-1))
        print(f"  同日(D) {same:+.4f}   次日(D+1) {nxt:+.4f}   → "
              f"{'外盘领先，用D+1' if abs(nxt) > abs(same) else 'A股已同步，D已含信息'}")

        print(f"\n【事件研究】{cfg['name']} >= ±{th}% → 次日收益拆解")
        print(f"  无条件基准: 收→收 {ret.mean():+.3f}% = 跳空 {gap.mean():+.3f}% + 盘中 {intra.mean():+.3f}%")
        print(f"  {'方向':<9}{'次数':>5}{'收→收':>9}{'跳空':>9}{'盘中':>9}{'盘中t值':>9}{'可规避':>8}")
        for dname, mask in ((f'涨>+{th}%', c >= th), (f'跌<-{th}%', c <= -th)):
            m = mask.shift(1).fillna(False).astype(bool).reindex(ret.index, fill_value=False)
            cc, gg, ii = ret[m], gap[m], intra[m]
            if len(cc) < 5:
                print(f"  {dname:<9}{len(cc):>5}  样本不足")
                continue
            share = ii.mean() / cc.mean() * 100 if cc.mean() != 0 else np.nan
            print(f"  {dname:<9}{len(cc):>5}{cc.mean():>8.3f}%{gg.mean():>8.3f}%"
                  f"{ii.mean():>8.3f}%{tstat(ii):>9.2f}{share:>7.0f}%")

        print(f"\n【可执行规则】{cfg['name']}跌<-{th}% → 次日开盘减仓（跳空照吃）")
        print(f"  {'方案':<16}{'年化':>9}{'夏普':>8}{'最大回撤':>10}{'触发':>6}{'扣费后年化':>12}")
        bh = perf(ret)
        print(f"  {'买入持有':<16}{bh['cagr']:>8.2f}%{bh['sharpe']:>8.2f}{bh['mdd']:>9.2f}%"
              f"{0:>6}{bh['cagr']:>11.2f}%")
        flat = (c <= -th).shift(1).fillna(False).astype(bool).reindex(ret.index, fill_value=False)
        for label, w in (('开盘减半', 0.5), ('开盘清仓', 1.0)):
            # 空仓比例w：跳空段全额承受，盘中段只留(1-w)
            strat = pd.Series(np.where(flat, gap + (1 - w) * intra, ret), index=ret.index)
            net = strat - flat.astype(float) * cost * w
            p, pn = perf(strat), perf(net)
            print(f"  {label:<16}{p['cagr']:>8.2f}%{p['sharpe']:>8.2f}{p['mdd']:>9.2f}%"
                  f"{int(flat.sum()):>6}{pn['cagr']:>11.2f}%")
        print()


if __name__ == '__main__':
    main()
