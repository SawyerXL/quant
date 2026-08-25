"""
因子预测力总表：什么能预测次日上证？

统一口径（与回测复验纪律一致）：
  因子值标 D，统一 shift(1) 挂到 A股 D+1 —— agent 已确认所有因子最晚收盘时点
  (美股/美债北京 D+1 凌晨4-5点) 都在 A股 D+1 开盘前，无未来函数。
  只报事前信息，不报同期相关。

因子变换：价格类→日收益率%，利率类→日变化bp，USDCNY→日变化%。
输出：线性corr(t) + 极端5%分位的次日均值（抓U型非线性）+ 胜率。

用法: python scripts/factor_predictive_table.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily


def load_sh():
    d = load_daily("000001", "2012-01-01", "2026-08-25").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    return pd.Series(cl.pct_change() * 100, name="sh_ret").dropna()


def series_from(df, col, kind="pct"):
    df = df.copy()
    date_col = "date" if "date" in df.columns else "日期"
    df["date"] = pd.to_datetime(df[date_col])
    s = df.set_index("date")[col].astype(float)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    # 期货最后一根是进行中K线(volume=0)，美股/美债当日可能未出 —— 都丢掉最后一根
    s = s.iloc[:-1]
    if kind == "pct":
        return s.pct_change() * 100
    return s.diff()  # bp/百分比点


def load_factors():
    fac = {}
    # 利率(变化bp)
    b = ak.bond_zh_us_rate()
    fac["美债10y(bp)"] = series_from(b, "美国国债收益率10年", "diff")
    fac["美债2y(bp)"] = series_from(b, "美国国债收益率2年", "diff")
    fac["中债10y(bp)"] = series_from(b, "中国国债收益率10年", "diff")
    # 商品(日收益%)
    fac["WTI原油"] = series_from(ak.futures_foreign_hist(symbol="CL"), "close")
    fac["布伦特"] = series_from(ak.futures_foreign_hist(symbol="OIL"), "close")
    fac["伦敦金"] = series_from(ak.futures_foreign_hist(symbol="XAU"), "close")
    fac["COMEX铜"] = series_from(ak.futures_foreign_hist(symbol="HG"), "close")
    # 美股指(日收益%)
    for k, sym in [("标普500", ".INX"), ("纳斯达克", ".IXIC"), ("SOX", ".SOX")]:
        try:
            fac[k] = series_from(ak.index_us_stock_sina(symbol=sym), "close")
        except Exception as e:
            print(f"  {k} 拉取失败: {e}")
    # 恒生
    fac["恒生"] = series_from(ak.stock_hk_index_daily_sina(symbol="HSI"), "close")
    # 人民币中间价(变化%)
    try:
        c = ak.currency_boc_safe()
        fac["USDCNY中间价"] = series_from(c, "美元", "pct")
    except Exception as e:
        print(f"  USDCNY 拉取失败: {e}")
    # 上证自身(对照): 前20日波动率 + 前20日涨幅 —— 已知有效, 作基准
    sh = load_daily("000001", "2012-01-01", "2026-08-25").sort_values("date")
    sh["date"] = pd.to_datetime(sh["date"])
    sh = sh.set_index("date")
    cl = pd.to_numeric(sh["close"], errors="coerce")
    ret = cl.pct_change() * 100
    fac["自身波动率20d"] = ret.rolling(20).std()
    fac["自身涨幅20d"] = (cl / cl.shift(20) - 1) * 100
    return fac


def tstat(x, y):
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(m) < 30:
        return np.nan, np.nan
    c = m["x"].corr(m["y"])
    t = c * np.sqrt(len(m) - 2) / np.sqrt(1 - c * c)
    return c, t


def main():
    sh = load_sh()
    print(f"上证次日收益样本: {len(sh)}天 {sh.index[0].date()}→{sh.index[-1].date()}, "
          f"无条件均值 {sh.mean():+.3f}%\n")
    fac = load_factors()

    print(f"{'因子':<14}{'样本':>6}{'corr':>8}{'t值':>7}  "
          f"{'因子最高5%→次日':>16}{'因子最低5%→次日':>16}  结论")
    results = []
    for name, s in fac.items():
        # 因子D → A股D+1：reindex到A股日期后shift(1)
        j = pd.DataFrame({"f": s.reindex(sh.index).shift(1), "sh": sh}).dropna()
        if len(j) < 200:
            print(f"{name:<14}{len(j):>6}   样本不足")
            continue
        c, t = tstat(j["f"], j["sh"])
        lo = j["f"] <= j["f"].quantile(0.05)
        hi = j["f"] >= j["f"].quantile(0.95)
        base = j["sh"].mean()
        hi_m, lo_m = j["sh"][hi].mean(), j["sh"][lo].mean()
        spread = hi_m - lo_m
        verdict = ("✅有预测力" if abs(t) > 3 and abs(c) > 0.05
                   else ("⚠️弱" if abs(t) > 2 else "❌无"))
        print(f"{name:<14}{len(j):>6}{c:>8.3f}{t:>7.2f}  "
              f"{hi_m:>+15.3f}%{lo_m:>+15.3f}%  {verdict} (差{spread:+.2f}pp)")
        results.append((name, len(j), c, t, hi_m, lo_m))
    print("\n无条件基准: 上证次日均值 %.3f%%" % base)

    # ── 日历效应 ──
    print("\n" + "=" * 70 + "\n日历效应(本地数据, 2012-2026)\n" + "=" * 70)
    dow = sh.groupby(sh.index.dayofweek).agg(["count", "mean"])
    names = ["周一", "周二", "周三", "周四", "周五"]
    print(f"  {'星期':<6}{'样本':>6}{'次日收益':>10}")
    for i, name in enumerate(names):
        if i in dow.index:
            print(f"  {name:<6}{int(dow.loc[i, 'count']):>6}{dow.loc[i, 'mean']:>+9.3f}%")
    # 月末效应: 每月最后2个交易日 vs 其余
    m_end = sh.groupby([sh.index.year, sh.index.month]).tail(2).index
    print(f"\n  月末2日: {sh[m_end].mean():+.3f}% (n={len(m_end)})  "
          f"vs 其余 {sh[~sh.index.isin(m_end)].mean():+.3f}%")
    # 长假前: 之后连续休市>=2天的交易日
    days = sorted(set(sh.index.date))
    hol_after = {}
    for i, dt in enumerate(days[:-2]):
        gap = (days[i + 1] - dt).days
        hol_after[dt] = gap >= 3  # 隔3天以上=至少连休2天
    pre = pd.Series(hol_after)
    pre_idx = pd.DatetimeIndex([pd.Timestamp(d) for d in pre[pre].index])
    print(f"  长假前(后隔≥2天休市): {sh[pre_idx].mean():+.3f}% (n={len(pre_idx)})  "
          f"vs 其余 {sh[~sh.index.isin(pre_idx)].mean():+.3f}%")


if __name__ == "__main__":
    main()
