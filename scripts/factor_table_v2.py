"""
因子表 v2：缺数据的因子想办法补数据再测（2026-08-25）

补数据方案：
  融资余额   → 上交所官网 stock_margin_sse（非东财端口，避IP封锁）
  亚太联动   → 日经/韩国/台湾 index_global_hist_em（东财，失败则放弃）
  央行流动性 → Shibor 7天 作为"银行间流动性"代理（公开市场操作无现成接口）
  期权情绪   → 上交所50ETF期权 QVIX index_option_50etf_qvix
  涨停情绪   → 用本地全市场日线自算"涨停家数"（东财涨停池被封，本地数据兜底）

口径与 factor_predictive_table.py 完全一致：shift(1)，因子D→A股D+1。
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


def tstat(x, y):
    m = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(m) < 30:
        return np.nan, np.nan
    c = m["x"].corr(m["y"])
    return c, c * np.sqrt(len(m) - 2) / np.sqrt(1 - c * c)


def run_factor(sh, name, s, note=""):
    j = pd.DataFrame({"f": s.reindex(sh.index).shift(1), "sh": sh}).dropna()
    if len(j) < 200:
        print(f"{name:<16}{len(j):>6}   样本不足 ({note})")
        return
    c, t = tstat(j["f"], j["sh"])
    hi = j["f"] >= j["f"].quantile(0.95)
    lo = j["f"] <= j["f"].quantile(0.05)
    verdict = ("✅" if abs(t) > 3 else ("⚠️" if abs(t) > 2 else "❌"))
    print(f"{name:<16}{len(j):>6}{c:>8.3f}{t:>7.2f}  "
          f"最高5%→{j['sh'][hi].mean():>+7.3f}%  最低5%→{j['sh'][lo].mean():>+7.3f}%  "
          f"{verdict}  {note}")


def main():
    sh = load_sh()
    print(f"上证次日收益 无条件均值 {sh.mean():+.3f}%  样本{len(sh)}天")
    print(f"{'因子':<16}{'样本':>6}{'corr':>8}{'t值':>7}  {'最高5%→次日':>12}{'最低5%→次日':>12}\n")

    # 1. 融资余额(上交所官网)
    try:
        m = ak.stock_margin_sse(start_date="20120101", end_date="20260825")
        col = "融资余额" if "融资余额" in m.columns else [c for c in m.columns if "融资" in c][0]
        s = pd.Series(pd.to_numeric(m[col], errors="coerce").values,
                      index=pd.to_datetime(m["信用交易日期"])).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        run_factor(sh, "融资余额(变化%)", s.pct_change() * 100, "上交所官网")
    except Exception as e:
        print(f"融资余额: 拉取失败 {type(e).__name__} {str(e)[:70]}")

    # 2. 亚太联动(东财, 被封则放弃)
    for label, sym in [("日经225", "日经225"), ("韩国KOSPI", "韩国KOSPI指数"), ("台湾加权", "台湾加权指数")]:
        try:
            df = ak.index_global_hist_em(symbol=sym)
            if df is None or df.empty:
                print(f"{label}: 返回空")
                continue
            df["日期"] = pd.to_datetime(df["日期"])
            s = df.set_index("日期")["收盘"].astype(float).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            run_factor(sh, label, s.pct_change() * 100, "东财index_global")
        except Exception as e:
            print(f"{label}: 拉取失败 {type(e).__name__} {str(e)[:60]}")

    # 3. Shibor 7天(流动性代理)
    try:
        r = ak.rate_interbank(market="上海银行间同业拆放利率", symbol="Shibor人民币", indicator="7天")
        r["日期"] = pd.to_datetime(r["日期"])
        s = r.set_index("日期")["利率"].astype(float).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        run_factor(sh, "Shibor7d(bp)", s.diff(), "央行流动性代理")
    except Exception as e:
        print(f"Shibor: 拉取失败 {type(e).__name__} {str(e)[:60]}")

    # 4. 50ETF期权QVIX(情绪)
    try:
        q = ak.index_option_50etf_qvix()
        q["日期"] = pd.to_datetime(q["日期"])
        s = q.set_index("日期")["收盘"].astype(float).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        run_factor(sh, "50ETF QVIX(变化)", s.diff(), "期权情绪")
    except Exception as e:
        print(f"QVIX: 拉取失败 {type(e).__name__} {str(e)[:60]}")

    # 5. 涨停家数(本地全市场自算, 2019+)
    try:
        print("  计算涨停家数(本地全市场, 需读5000+文件)...")
        import glob
        cnt = {}
        for f in glob.glob("data_store/daily/20*/*.parquet"):
            try:
                d = pd.read_parquet(f, columns=["date", "close"])
                d["date"] = pd.to_datetime(d["date"]).astype(str).str[:10]
                d["close"] = pd.to_numeric(d["close"], errors="coerce")
                d = d[d["date"] >= "2019-01-01"].sort_values("date")
                if len(d) < 2:
                    continue
                d["pct"] = d["close"].pct_change() * 100
                # 涨停: 主板9.8%+/科创创业19.5%+ (简化, 不含ST)
                zt = d[(d["pct"] >= 9.8) | (d["pct"] >= 19.5)]
                for day, grp in zt.groupby("date"):
                    cnt[day] = cnt.get(day, 0) + len(grp)
            except Exception:
                pass
        s = pd.Series(cnt).sort_index()
        s.index = pd.to_datetime(s.index)
        run_factor(sh, "涨停家数(变化)", s.diff(), "本地自算,2019+")
    except Exception as e:
        print(f"涨停家数: 失败 {type(e).__name__} {str(e)[:60]}")


if __name__ == "__main__":
    main()
