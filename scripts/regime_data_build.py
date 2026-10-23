"""中国股市牛熊研究：月度数据面板（2026-10-23 立项，Phase 0）。

只读/研究用，不喂信号链。数据源与口径：
- 价格：本地 SH000001 / CSI800 日线（2000/2005 起）→ 月末值、MA200 距离、
  前向 6/12 月收益、前向 6 月最大回撤。
- 估值：Legulegu 沪深300 PE（月度，2005-04 起）；ERP=1/PE−10Y 国债。
- 流动性/信用：SHIBOR 3M、10Y 国债、LPR、M1/M2 同比、社融增量。
- 供给：社融中的"非金融企业境内股票融资"（IPO+再融资代理）、沪深两融余额、
  新成立权益基金募集份额。
- 宏观月度序列进入预测时必须 lag 1 个月（M2/社融约次月 10-15 日发布）；
  `*_lag1` 列即为此口径。估值/利率/两融月末值当日可得。

用法:
  python scripts/regime_data_build.py            # 有缓存则用缓存
  python scripts/regime_data_build.py --refresh  # 重新拉取全部原始序列
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data_store" / "regime" / "raw"
OUT = ROOT / "data_store" / "regime" / "panel_monthly.parquet"
META = ROOT / "data_store" / "regime" / "panel_meta.json"
START, END = "2005-01-01", "2026-10-31"

SOURCES = {
    "pe_hs300": ("akshare.stock_index_pe_lg(沪深300)", "倍"),
    "money": ("akshare.macro_china_money_supply", "亿元 / %"),
    "shrzgm": ("akshare.macro_china_shrzgm", "亿元"),
    "shibor": ("akshare.macro_china_shibor_all", "%"),
    "bond": ("akshare.bond_zh_us_rate", "%"),
    "margin": ("akshare.macro_china_market_margin_sh+sz", "元"),
    "lpr": ("akshare.macro_china_lpr", "%"),
    "new_fund": ("akshare.fund_new_found_em", "亿份"),
}


def _fetch(name, fn, refresh=False):
    p = RAW / f"{name}.csv"
    if p.exists() and not refresh:
        return pd.read_csv(p)
    d = fn()
    RAW.mkdir(parents=True, exist_ok=True)
    d.to_csv(p, index=False)
    return d


def fetch_all(refresh=False):
    import akshare as ak
    raw = {}
    raw["pe_hs300"] = _fetch("pe_hs300",
                             lambda: ak.stock_index_pe_lg(symbol="沪深300"),
                             refresh)
    raw["money"] = _fetch("money", ak.macro_china_money_supply, refresh)
    raw["shrzgm"] = _fetch("shrzgm", ak.macro_china_shrzgm, refresh)
    raw["shibor"] = _fetch("shibor", ak.macro_china_shibor_all, refresh)
    raw["bond"] = _fetch("bond",
                         lambda: ak.bond_zh_us_rate(start_date="20050101"),
                         refresh)
    def _margin():
        sh = ak.macro_china_market_margin_sh()
        sh["date"] = pd.to_datetime(sh["日期"], errors="coerce")
        sh["融资融券余额"] = pd.to_numeric(sh["融资融券余额"], errors="coerce")
        try:
            sz = ak.macro_china_market_margin_sz()
            sz["date"] = pd.to_datetime(sz["日期"], errors="coerce")
            sz["融资融券余额"] = pd.to_numeric(sz["融资融券余额"], errors="coerce")
            s = (pd.concat([sh[["date", "融资融券余额"]],
                            sz[["date", "融资融券余额"]]])
                 .groupby("date", as_index=False)["融资融券余额"].sum())
            return s
        except Exception:
            return sh[["date", "融资融券余额"]].rename(
                columns={"融资融券余额": "融资融券余额"})
    raw["margin"] = _fetch("margin", _margin, refresh)
    raw["lpr"] = _fetch("lpr", ak.macro_china_lpr, refresh)
    raw["new_fund"] = _fetch("new_fund", ak.fund_new_found_em, refresh)
    return raw


def _month(s):
    return pd.to_datetime(s).dt.to_period("M")


def _monthly_last(df, date_col, value_cols):
    d = df.copy()
    d["_m"] = _month(d[date_col])
    d = d.dropna(subset=["_m"])
    out = d.groupby("_m")[value_cols].last()
    return out


def _index_monthly(code=None, meta=None):
    if meta:
        d = load_meta(meta).copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        close = pd.to_numeric(d["close"], errors="coerce").dropna()
    else:
        d = load_daily(code, "2000-01-01", "2026-12-31")
        if d.empty:
            raise ValueError(f"无本地日线: {code}")
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        close = pd.to_numeric(d["close"], errors="coerce").dropna()
    m = close.resample("ME").last()
    out = pd.DataFrame({"close": m})
    out["ret_1m"] = m.pct_change()
    ma200 = close.rolling(200, min_periods=120).mean()
    out["ma200_dist"] = (m / ma200.resample("ME").last() - 1) * 100
    out["ma200_slope_3m"] = ma200.resample("ME").last().pct_change(3) * 100
    out["fwd_6m"] = m.shift(-6) / m - 1
    out["fwd_12m"] = m.shift(-12) / m - 1
    # 前向 6 个月最大回撤（从月末收盘到未来 6 个月内最低收盘）
    dd6 = []
    for t in m.index:
        end = t + pd.DateOffset(months=6)
        w = close[(close.index > t) & (close.index <= end)]
        dd6.append(float((w.min() / m.loc[t] - 1)) if len(w) else np.nan)
    out["fwd_6m_mdd"] = dd6
    out.index = out.index.to_period("M")
    return out


def build_panel(raw):
    sh = _index_monthly(code="SH000001").add_prefix("sh_")
    csi = _index_monthly(meta="csi800_index").add_prefix("csi_")
    df = sh.join(csi, how="outer")

    # 估值
    pe = raw["pe_hs300"].copy()
    pe["_m"] = _month(pd.to_datetime(pe["日期"], errors="coerce"))
    pe = pe.dropna(subset=["_m"]).groupby("_m").last()
    df["pe_ttm"] = pd.to_numeric(pe["滚动市盈率"], errors="coerce")
    df["pe_static"] = pd.to_numeric(pe["静态市盈率"], errors="coerce")
    df["earnings_yield"] = 100.0 / df["pe_ttm"]
    df["pe_pct_10y"] = df["pe_ttm"].rolling(120, min_periods=60).apply(
        lambda x: float((x[-1] > x).mean()), raw=True)
    df["pe_pct_exp"] = df["pe_ttm"].expanding(min_periods=60).apply(
        lambda x: float((x[-1] > x).mean()), raw=True)

    # 货币
    mo = raw["money"].copy()
    mo["_m"] = pd.to_datetime(
        mo["月份"].astype(str).str.replace("月份", "", regex=False)
        .str.replace("年", "-", regex=False).str.replace("月", "", regex=False)
        .str.strip(), errors="coerce").dt.to_period("M")
    mo = mo.dropna(subset=["_m"]).groupby("_m").last()
    df["m1_yoy"] = pd.to_numeric(mo["货币(M1)-同比增长"], errors="coerce")
    df["m2_yoy"] = pd.to_numeric(mo["货币和准货币(M2)-同比增长"], errors="coerce")
    df["m1_m2_gap"] = df["m1_yoy"] - df["m2_yoy"]

    # 社融
    sf = raw["shrzgm"].copy()
    sf["_m"] = pd.to_datetime(sf["月份"].astype(str), format="%Y%m",
                              errors="coerce").dt.to_period("M")
    sf = sf.dropna(subset=["_m"]).groupby("_m").last()
    df["sf_total"] = pd.to_numeric(sf["社会融资规模增量"], errors="coerce")
    df["sf_equity"] = pd.to_numeric(sf.get("其中-非金融企业境内股票融资"),
                                    errors="coerce")
    # 社融源数据到 2026-04 后缺失；记录陈旧月数，预测只用 age<=2（防把
    # 五个月前的 12M 滚动和当成当月值）
    age, last = [], None
    for m in df.index:
        if pd.notna(df.at[m, "sf_equity"]):
            last = m
        age.append(np.nan if last is None else
                   (m.year - last.year) * 12 + (m.month - last.month))
    df["sf_age_months"] = age

    # 利率
    shb = raw["shibor"].copy()
    shb["_m"] = _month(pd.to_datetime(shb["日期"], errors="coerce"))
    shb = shb.dropna(subset=["_m"]).groupby("_m").last()
    df["shibor3m"] = pd.to_numeric(shb.get("3M-定价"), errors="coerce")
    bd = raw["bond"].copy()
    bd["_m"] = _month(pd.to_datetime(bd["日期"], errors="coerce"))
    bd = bd.dropna(subset=["_m"]).groupby("_m").last()
    df["cgb10y"] = pd.to_numeric(bd.get("中国国债收益率10年"), errors="coerce")
    df["erp"] = df["earnings_yield"] - df["cgb10y"]
    df["erp_pct_10y"] = df["erp"].rolling(120, min_periods=60).apply(
        lambda x: float((x[-1] > x).mean()), raw=True)

    lpr = raw["lpr"].copy()
    lpr["_m"] = _month(pd.to_datetime(lpr["TRADE_DATE"], errors="coerce"))
    lpr = lpr.dropna(subset=["_m"]).groupby("_m").last()
    df["lpr1y"] = pd.to_numeric(lpr.get("LPR1Y"), errors="coerce")

    # 两融
    mg = raw["margin"].copy()
    mg["_m"] = _month(pd.to_datetime(mg["date"], errors="coerce"))
    mg = mg.dropna(subset=["_m"]).groupby("_m").last()
    df["margin_bal"] = pd.to_numeric(mg.get("融资融券余额"), errors="coerce")
    df["margin_chg_3m"] = df["margin_bal"].pct_change(3) * 100

    # 新基金（权益类，按成立日期月度汇总）
    nf = raw["new_fund"].copy()
    nf["_m"] = _month(pd.to_datetime(nf.get("成立日期"), errors="coerce"))
    nf["募集份额"] = pd.to_numeric(nf.get("募集份额"), errors="coerce")
    t = nf["基金类型"].astype(str)
    mask = (t.str.contains("股票型|偏股|指数型-股票", na=False)
            & ~t.str.contains("偏债|债券", na=False))
    nfe = nf[mask].dropna(subset=["_m"])
    df["new_fund_equity"] = nfe.groupby("_m")["募集份额"].sum()

    # 12 月滚动供给/发行
    df["sf_equity_12m"] = df["sf_equity"].rolling(12, min_periods=6).sum()
    df.loc[df["sf_age_months"] > 1, "sf_equity_12m"] = np.nan
    df["new_fund_12m"] = df["new_fund_equity"].rolling(12, min_periods=6).sum()

    # 宏观发布滞后：预测时用 lag1
    for c in ["m1_yoy", "m2_yoy", "m1_m2_gap", "sf_total", "sf_equity",
              "sf_equity_12m", "lpr1y"]:
        df[c + "_lag1"] = df[c].shift(1)

    df = df.sort_index()
    df.index.name = "month"
    return df


def audit(df, raw):
    lines = ["# Regime 数据面板审计（2026-10-23）", "",
             f"- 面板行数: {len(df)}，区间 {df.index.min()} ~ {df.index.max()}",
             f"- 原始文件: `data_store/regime/raw/`；面板: `{OUT}`", ""]
    lines.append("| 序列 | 起始 | 结束 | 有效月数 | 说明 |")
    lines.append("|---|---|---|---|---|")
    desc = {
        "sh_close": "上证收盘", "csi_close": "CSI800 收盘",
        "pe_ttm": "沪深300 滚动PE(2005-04起)", "m1_yoy": "M1同比",
        "m2_yoy": "M2同比", "sf_total": "社融增量", "sf_equity": "股票融资(IPO+再融资代理)",
        "shibor3m": "SHIBOR 3M", "cgb10y": "10Y国债", "lpr1y": "LPR 1Y",
        "margin_bal": "两融余额(沪深合计)", "new_fund_equity": "新成立权益基金募集",
    }
    for c, d in desc.items():
        if c not in df.columns:
            lines.append(f"| {c} | - | - | 0 | {d}（缺失） |")
            continue
        s = df[c].dropna()
        lines.append(f"| {c} | {s.index.min() if len(s) else '-'} | "
                     f"{s.index.max() if len(s) else '-'} | {len(s)} | {d} |")
    lines += ["", "## 已知缺口/口径", "",
              "- 历史广度（涨跌家数）未纳入：本地全市场日线 2014+，2014 前缺口；",
              "- 解禁/减持规模未纳入：缺历史月度序列；",
              "- IPO 总量未单列：用社融“非金融企业境内股票融资”作代理（含再融资）；",
              "- 新基金数据按成立日期汇总，早期覆盖不全；",
              "- 宏观序列 `*_lag1` 为预测口径（发布滞后 1 个月）；",
              "- 2026-10 为部分月（as_of 2026-10-23）。"]
    lines += ["", "## 量纲/连续性快检", ""]
    rate_cols = {"pe_ttm", "m1_yoy", "m2_yoy", "shibor3m", "cgb10y", "lpr1y",
                 "margin_chg_3m", "erp"}
    for c in ["pe_ttm", "m1_yoy", "m2_yoy", "sf_total", "sf_equity",
              "shibor3m", "cgb10y", "margin_bal"]:
        if c not in df.columns:
            continue
        s = df[c].dropna()
        if len(s) < 3:
            continue
        if c in rate_cols:
            # 百分比/倍数序列：环比无意义，用百分点变化
            lines.append(f"- {c}: min={s.min():.2f}, max={s.max():.2f}, "
                         f"max|Δ|={s.diff().abs().max():.2f}pp")
        else:
            # 金额序列：允许负值/零值（社融增量可为负），只报极值+绝对变化
            lines.append(f"- {c}: min={s.min():.3g}, max={s.max():.3g}, "
                         f"max|Δ|={s.diff().abs().max():.3g}（金额，环比不适用）")
    lines += ["", "## 口径附注", "",
              "- 两融 2010-03 起为融资融券**试点起步**，余额从 659 万元级增长，"
              "早期 3 月变化率是基数效应，H3 研究从 2013+ 或对数变化开始；",
              "- 社融/股票融资源数据止于 2026-04；`sf_age_months` 记录陈旧月数，"
              "`sf_equity_12m` 在 age>1 时置 NaN，预测只用新鲜值；",
              "- m1/m2 为同比(%)，跨零时环比变化无意义；已在快检中改用百分点；",
              "- 新基金 2023+，不用于长周期检验，只作近期描述。"]
    p = ROOT / "logs" / "regime_data_audit_20261023.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:20]))
    print("->", p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    raw = fetch_all(refresh=args.refresh)
    df = build_panel(raw)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.reset_index().assign(month=lambda x: x["month"].astype(str)).to_parquet(
        OUT, index=False)
    META.write_text(json.dumps({
        "as_of": "2026-10-23", "start": START, "end": END,
        "rows": int(len(df)),
        "sources": {k: {"source": v[0], "unit": v[1]} for k, v in SOURCES.items()},
        "notes": ["宏观序列预测用 *_lag1", "2026-10 为部分月",
                  "广度/解禁/减持未纳入（缺口已在审计中声明）"],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"panel {df.shape} -> {OUT}")
    audit(df, raw)


if __name__ == "__main__":
    main()
