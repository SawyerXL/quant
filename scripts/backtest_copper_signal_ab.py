"""
伦铜(LME/CAD) vs 美铜(COMEX/HG) 作为紫金矿业/洛阳钼业领先信号的 A/B 回测。

背景：signal_alerts 里 copper 信号一直用 symbol='HG'(COMEX美铜) 却标名"伦铜"。
问题：要不要真换成伦铜标的？本脚本给 A/B 数据。

先做日期对齐实测再回测 —— 历史上 CSI500 N=6 的 +39.6% 就是日期错位造出来的。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily

THRESH = 2.0             # 现网配置的触发阈值，回测不动它
ROUND_TRIP_COST = 0.20   # % 一卖一买：印花税0.05 + 双边佣金0.05 + 滑点0.10
STOCKS = {'601899': '紫金矿业', '603993': '洛阳钼业'}
Y0, Y1 = 2019, 2026


def copper_ret(symbol: str) -> pd.Series:
    df = ak.futures_foreign_hist(symbol=symbol).sort_values('date')
    df['date'] = pd.to_datetime(df['date'])
    s = df.set_index('date')['close'].astype(float)
    s = s[~s.index.duplicated(keep='last')]
    return (s.pct_change() * 100).dropna()


def stock_ohlc(code: str) -> pd.DataFrame:
    dfs = []
    for y in range(Y0, Y1 + 1):
        d = load_daily(code, f'{y}-01-01', f'{y}-12-31')
        if not d.empty:
            dfs.append(d)
    d = pd.concat(dfs)
    d['date'] = pd.to_datetime(d['date'])
    d = d.set_index('date').sort_index()
    d = d[~d.index.duplicated(keep='last')]
    return d[['open', 'close']].astype(float).dropna()


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 else np.nan


def perf(daily_pct: pd.Series) -> dict:
    """daily_pct 是百分数序列。"""
    r = daily_pct.dropna() / 100
    if len(r) < 30:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / 243
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sharpe = r.mean() / r.std(ddof=1) * np.sqrt(243) if r.std(ddof=1) > 0 else np.nan
    mdd = (eq / eq.cummax() - 1).min()
    return {'年化': cagr * 100, '夏普': sharpe, '最大回撤': mdd * 100}


def main():
    hg = copper_ret('HG')    # COMEX 美铜（现网在用）
    cad = copper_ret('CAD')  # LME 伦铜（候选）
    print(f"美铜HG  {len(hg)}根 {hg.index[0].date()}→{hg.index[-1].date()}")
    print(f"伦铜CAD {len(cad)}根 {cad.index[0].date()}→{cad.index[-1].date()}")
    print(f"两者日收益相关系数 {hg.reindex(cad.index).corr(cad):.4f}\n")

    for code, name in STOCKS.items():
        ohlc = stock_ohlc(code)
        px = ohlc['close']
        f = pd.DataFrame({
            'ret':   px.pct_change() * 100,                  # 收→收
            'gap':  (ohlc['open'] / px.shift(1) - 1) * 100,  # 昨收→今开（持仓者躲不掉）
            'intra': (px / ohlc['open'] - 1) * 100,          # 今开→今收（开盘减仓能躲）
        }).dropna()
        f = f[str(Y0):]
        ret, gap, intra = f['ret'], f['gap'], f['intra']
        print("=" * 78)
        print(f"{name} {code}   A股样本 {len(ret)}天 {ret.index[0].date()}→{ret.index[-1].date()}")
        dirty = int((ret.abs() > 11).sum())
        if dirty:
            print(f"  ⚠ 单日涨跌超11%的异常bar: {dirty}根（A股涨跌停±10%，疑似脏数据/复权跳变）")

        # ── 1. 日期对齐实测：铜D 到底对应 A股D 还是 A股D+1 ──
        print("\n【日期对齐】铜当日涨跌 vs A股收益相关系数")
        print(f"{'信号':<10}{'同日(D)':>12}{'次日(D+1)':>12}  → 结论")
        for label, cop in (('美铜HG', hg), ('伦铜CAD', cad)):
            c = cop.reindex(ret.index)
            same = c.corr(ret)
            nxt = c.corr(ret.shift(-1))
            verdict = "铜领先→用D+1" if abs(nxt) > abs(same) else "已同步→D已含信息"
            print(f"{label:<10}{same:>12.4f}{nxt:>12.4f}  → {verdict}")

        # ── 2. 事件研究：拆解成"跳空(躲不掉)"和"盘中(开盘减仓能躲)" ──
        print(f"\n【事件研究】铜日变动 >= ±{THRESH}% → 下一个A股交易日，拆解收益来源")
        print(f"无条件基准: 收→收 {ret.mean():+.3f}% = 跳空 {gap.mean():+.3f}% + 盘中 {intra.mean():+.3f}%")
        print(f"{'信号':<10}{'方向':<8}{'次数':>5}{'收→收':>9}{'跳空':>9}{'盘中':>9}{'盘中t值':>9}{'可规避占比':>11}")
        for label, cop in (('美铜HG', hg), ('伦铜CAD', cad)):
            c = cop.reindex(ret.index)
            for dname, mask in (('涨>+2%', c >= THRESH), ('跌<-2%', c <= -THRESH)):
                m = mask.shift(1).fillna(False).astype(bool)   # 信号在D，收益看D+1
                cc, gg, ii = ret[m].dropna(), gap[m].dropna(), intra[m].dropna()
                if len(cc) < 5:
                    continue
                share = ii.mean() / cc.mean() * 100 if cc.mean() != 0 else np.nan
                print(f"{label:<10}{dname:<8}{len(cc):>5}{cc.mean():>8.3f}%{gg.mean():>8.3f}%"
                      f"{ii.mean():>8.3f}%{tstat(ii):>9.2f}{share:>10.0f}%")

        # ── 3. 可执行规则 A/B：铜跌>2% → 次日"开盘"减仓（跳空躲不掉） ──
        print(f"\n【可执行规则】铜跌<-{THRESH}% → 次日开盘清仓(只规避盘中段，跳空照吃)")
        print(f"{'方案':<14}{'年化':>9}{'夏普':>8}{'最大回撤':>10}{'空仓天数':>9}{'扣费后年化':>12}")
        bh = perf(ret)
        print(f"{'买入持有':<14}{bh['年化']:>8.2f}%{bh['夏普']:>8.2f}{bh['最大回撤']:>9.2f}%"
              f"{0:>9}{bh['年化']:>11.2f}%")
        for label, cop in (('美铜HG(现网)', hg), ('伦铜CAD(候选)', cad)):
            c = cop.reindex(ret.index)
            flat = (c <= -THRESH).shift(1).fillna(False).astype(bool)
            flat = flat.reindex(ret.index, fill_value=False)
            # 空仓日只拿到跳空段（开盘才卖出），非空仓日拿完整收→收
            strat = pd.Series(np.where(flat, gap.reindex(ret.index), ret), index=ret.index)
            p = perf(strat)
            # 每个空仓日=一次卖出+一次买回：印花税0.05+双边佣金0.05+滑点0.10
            net = perf(strat - flat.astype(float) * ROUND_TRIP_COST)
            print(f"{label:<14}{p['年化']:>8.2f}%{p['夏普']:>8.2f}{p['最大回撤']:>9.2f}%"
                  f"{int(flat.sum()):>9}{net['年化']:>11.2f}%")
        print()


if __name__ == '__main__':
    main()
