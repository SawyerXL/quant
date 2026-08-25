"""因子表 v2b：v2 失败项的补救轮。"""
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


def tstat(x, y):
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(m) < 30:
        return np.nan, np.nan
    c = m["x"].corr(m["y"])
    return c, c * np.sqrt(len(m) - 2) / np.sqrt(1 - c * c)


def run_factor(sh, name, s, note=""):
    j = pd.DataFrame({"f": s.reindex(sh.index).shift(1), "sh": sh}).dropna()
    if len(j) < 200:
        print(f"{name:<18}{len(j):>6}   样本不足 {note}")
        return
    c, t = tstat(j["f"], j["sh"])
    hi = j["f"] >= j["f"].quantile(0.95)
    lo = j["f"] <= j["f"].quantile(0.05)
    print(f"{name:<18}{len(j):>6}{c:>8.3f}{t:>7.2f}  "
          f"高5%→{j['sh'][hi].mean():>+7.3f}%  低5%→{j['sh'][lo].mean():>+7.3f}%  "
          f"{'✅' if abs(t)>3 else ('⚠️' if abs(t)>2 else '❌')}  {note}")


def event_test(sh, signal_series, name, horizons=(1, 5, 20)):
    """事件口径: 信号日(D) → D+1起forward收益。与框架'融资止跌'一致。"""
    fwd = {n: (sh.shift(-n)).reindex(sh.index) for n in horizons}
    m = signal_series.reindex(sh.index).fillna(False).astype(bool)
    base = sh.mean()
    print(f"\n  【事件】{name}  触发{m.sum()}次")
    print(f"  {'':<20}{'次数':>6}" + "".join(f"{f'{n}日后':>10}" for n in horizons))
    print(f"  {'信号触发':<20}{m.sum():>6}" + "".join(f"{fwd[n][m].mean():>+9.3f}%" for n in horizons))
    ctrl = ~m
    print(f"  {'无条件对照':<20}{ctrl.sum():>6}" + "".join(f"{fwd[n][ctrl].mean():>+9.3f}%" for n in horizons))


def main():
    sh = load_sh()
    print(f"上证次日 无条件 {sh.mean():+.3f}%  n={len(sh)}")
    print(f"{'因子':<18}{'样本':>6}{'corr':>8}{'t值':>7}  {'高5%→次日':>11}{'低5%→次日':>11}\n")

    # 1. Shibor 正确参数名
    try:
        r = ak.rate_interbank(market="上海银行同业拆借市场", symbol="Shibor人民币", indicator="1周")
        s = pd.Series(pd.to_numeric(r["利率"], errors="coerce").values,
                      index=pd.to_datetime(r["日期"])).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        run_factor(sh, "Shibor1周(bp)", s.diff(), "流动性代理")
    except Exception as e:
        print(f"Shibor: 失败 {type(e).__name__} {str(e)[:60]}")

    # 2. QVIX 正确列名
    try:
        q = ak.index_option_50etf_qvix()
        s = pd.Series(pd.to_numeric(q["close"], errors="coerce").values,
                      index=pd.to_datetime(q["date"])).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        run_factor(sh, "QVIX(变化)", s.diff(), "期权情绪")
        # 水平值也测(高恐慌状态 → 次日?)
        run_factor(sh, "QVIX(水平)", s, "期权情绪")
    except Exception as e:
        print(f"QVIX: 失败 {type(e).__name__} {str(e)[:60]}")

    # 3. 亚太 via investing 源
    for country, idx in [("日本", "日经225"), ("韩国", "KOSPI"), ("中国台湾", "台湾加权指数")]:
        try:
            d = ak.index_investing_global(country=country, index_name=idx)
            s = pd.Series(pd.to_numeric(d["收盘"], errors="coerce").values,
                          index=pd.to_datetime(d["日期"])).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            run_factor(sh, idx, s.pct_change() * 100, "investing源")
        except Exception as e:
            print(f"{idx}: 失败 {type(e).__name__} {str(e)[:50]}")

    # 4. 融资余额 事件口径: 连降≥10天后首次回升(框架原定义)
    try:
        m = ak.stock_margin_sse(start_date="20120101", end_date="20260825")
        s = pd.Series(pd.to_numeric(m["融资余额"], errors="coerce").values,
                      index=pd.to_datetime(m["信用交易日期"])).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        down = s.diff() < 0
        cons = down.groupby((~down).cumsum()).cumsum()
        turn = (cons.shift(1) >= 10) & (s.diff() > 0)   # 昨日连降≥10天 且 今日回升
        event_test(sh, turn, "融资连降≥10天后首次回升")
    except Exception as e:
        print(f"融资事件: 失败 {type(e).__name__} {str(e)[:60]}")

    # 5. 对照: 涨停家数(v2已测✅)与自身波动率的关系说明
    print("\n  注: v2 里涨停家数变化 t=3.55 两侧都是正(波动聚集形态), 与自身波动率信号同族。")


if __name__ == "__main__":
    main()
