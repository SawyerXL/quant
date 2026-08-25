"""
Mac 补测脚本 —— 测本机(东财封IP)拿不到、但 Mac 网络可得的 5 个因子。

自包含：上证指数和因子全部在线拉取，不依赖本地 data_store。
口径与 factor_predictive_table.py 一致：shift(1)，因子D→A股D+1，corr/t/极端5%分位。

用法(在 Mac 上):  python3 scripts/mac_factor_probe.py
依赖: pip install akshare pandas
"""
import sys
import numpy as np
import pandas as pd
import akshare as ak

START, END = "2012-01-01", "2026-08-25"


def tstat(x, y):
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(m) < 200:
        return np.nan, np.nan
    c = m["x"].corr(m["y"])
    return c, c * np.sqrt(len(m) - 2) / np.sqrt(1 - c * c)


def load_sh():
    d = ak.stock_zh_index_daily(symbol="sh000001")
    d["date"] = pd.to_datetime(d["date"])
    d = d[(d["date"] >= START) & (d["date"] <= END)].sort_values("date")
    d = d.set_index("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    return pd.Series(cl.pct_change() * 100, name="sh_ret").dropna()


def to_series(df, date_col, close_col, drop_last=False):
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    s = pd.Series(pd.to_numeric(df[close_col], errors="coerce").values,
                  index=df[date_col]).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if drop_last:
        s = s.iloc[:-1]   # 期货当日K线未走完
    return s


def run(sh, name, s, kind="pct"):
    if s is None or len(s) < 300:
        print(f"{name:<14}  拉取失败或样本不足")
        return
    f = s.pct_change() * 100 if kind == "pct" else s.diff()
    j = pd.DataFrame({"f": f.reindex(sh.index).shift(1), "sh": sh}).dropna()
    c, t = tstat(j["f"], j["sh"])
    hi = j["f"] >= j["f"].quantile(0.95)
    lo = j["f"] <= j["f"].quantile(0.05)
    print(f"{name:<14}{len(j):>6}{c:>8.3f}{t:>7.2f}  "
          f"高5%→{j['sh'][hi].mean():>+7.3f}%  低5%→{j['sh'][lo].mean():>+7.3f}%  "
          f"{'✅' if abs(t) > 3 else ('⚠️' if abs(t) > 2 else '❌')}")


def main():
    sh = load_sh()
    print(f"上证次日 无条件均值 {sh.mean():+.3f}%  样本 {len(sh)}天\n")
    print(f"{'因子':<14}{'样本':>6}{'corr':>8}{'t值':>7}  {'高5%→次日':>11}{'低5%→次日':>11}")

    # 1-3. 亚太三指数(东财, Mac网络可用)
    for name, sym in [("日经225", "日经225"), ("韩国KOSPI", "韩国KOSPI"), ("台湾加权", "台湾加权")]:
        try:
            d = ak.index_global_hist_em(symbol=sym)
            run(sh, name, to_series(d, "日期", "收盘"))
        except Exception as e:
            print(f"{name:<14}  失败 {type(e).__name__} {str(e)[:40]}")

    # 4. DXY 美元指数
    try:
        d = ak.index_global_hist_em(symbol="美元指数")
        run(sh, "美元指数DXY", to_series(d, "日期", "收盘"))
    except Exception as e:
        print(f"美元指数DXY   失败 {type(e).__name__} {str(e)[:40]}")

    # 5. USDCNH 离岸人民币
    try:
        d = ak.forex_hist_em(symbol="USDCNH")
        run(sh, "USDCNH", to_series(d, "日期", "最新价"))
    except Exception as e:
        print(f"USDCNH      失败 {type(e).__name__} {str(e)[:40]}")

    # 6. VIX —— 试多个候选，拿到哪个算哪个
    for sym in ["VIX恐慌指数", "恐慌指数VIX", "CBOE波动率"]:
        try:
            d = ak.index_global_hist_em(symbol=sym)
            run(sh, f"VIX({sym})", to_series(d, "日期", "收盘"))
            break
        except Exception:
            continue
    else:
        print("VIX          本版akshare无可用接口")

    print("\n把输出整段贴回 Claude 会话即可并表。")


if __name__ == "__main__":
    main()
