"""
策略归因分析 v2 — Carhart 四因子 + 压力测试 + 条件Beta

因子构造：
  市场(MKT)  = CSI 800 月收益 − Rf
  规模(SMB)  = CSI 1000 − CSI 300 月收益差（小盘 − 大盘）
  价值(HML)  = 申万价值 − 中证成长 月收益差（价值 − 成长）
  动量(MOM)  = 宇宙截面 WML，12m-1m 跳月动量

输出三组结果：
  A. 四因子模型（核心）
  B. 剔除2020-2021压力测试
  C. 牛熊分段条件Beta
"""
import os, sys
import pandas as pd
import numpy as np
import statsmodels.api as sm
import akshare as ak
from pathlib import Path
from loguru import logger

BASE     = Path(__file__).parent.parent
DAILY    = BASE / "data_store/daily"
META     = BASE / "data_store/meta"
NAV_PATH = BASE / "logs/backtest_a4_nav_hist_universe.csv"

START      = "2019-01-01"
END        = "2024-12-31"
DATA_START = "2017-12-01"
RF_ANNUAL  = 0.025


# ── 辅助：取指数月度收益 ──────────────────────────────────────
def index_monthly(symbol: str, name: str) -> pd.Series:
    df = ak.stock_zh_index_daily(symbol=symbol)
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["close"].resample("ME").last().pct_change().dropna()
    logger.info(f"  {name}({symbol}): {len(s)}个月，{s.index[0].date()} → {s.index[-1].date()}")
    return s.rename(name)


# ── 1. 加载策略月度超额收益 ────────────────────────────────────
def load_strategy() -> pd.Series:
    nav = pd.read_csv(NAV_PATH, index_col=0, parse_dates=True)["nav_hist_universe"]
    ret = nav.resample("ME").last().pct_change().dropna() - RF_ANNUAL / 12
    return ret[(ret.index >= START) & (ret.index <= END)].rename("strat_excess")


# ── 2. 加载四因子 ─────────────────────────────────────────────
def load_factors(mom_series: pd.Series) -> pd.DataFrame:
    logger.info("下载指数数据...")
    csi800 = pd.read_parquet(META / "csi800_index.parquet")
    csi800["date"] = pd.to_datetime(csi800["date"])
    mkt = (csi800.set_index("date")["close"]
           .resample("ME").last().pct_change().dropna() - RF_ANNUAL / 12)
    mkt = mkt[(mkt.index >= START) & (mkt.index <= END)].rename("MKT")

    csi300  = index_monthly("sh000300", "CSI300")
    csi1000 = index_monthly("sh000852", "CSI1000")
    val     = index_monthly("sz399371", "SW_Value")
    growth  = index_monthly("sz399370", "CSI_Growth")

    smb = (csi1000 - csi300).rename("SMB")
    hml = (val - growth).rename("HML")

    return pd.DataFrame({"MKT": mkt, "SMB": smb, "HML": hml, "MOM": mom_series})


# ── 3. 构建截面动量因子 WML ───────────────────────────────────
def build_mom(codes: list[str]) -> pd.Series:
    logger.info(f"加载 {len(codes)} 只股票日线数据（{DATA_START} → {END}）...")
    chunks = []
    for yr in range(int(DATA_START[:4]), int(END[:4]) + 1):
        yr_dir = DAILY / str(yr)
        if not yr_dir.exists():
            continue
        loaded = 0
        for code in codes:
            fp = yr_dir / f"{code}.parquet"
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp, columns=["date", "pct_chg", "code"])
                df["date"] = pd.to_datetime(df["date"])
                df = df[(df["date"] >= DATA_START) & (df["date"] <= END)]
                if not df.empty:
                    chunks.append(df)
                    loaded += 1
            except Exception:
                pass
        logger.info(f"  {yr}: {loaded} 只")

    panel = pd.concat(chunks, ignore_index=True)
    panel["ret"] = panel["pct_chg"] / 100.0
    panel["ym"]  = panel["date"].dt.to_period("M")
    monthly = (panel.groupby(["code", "ym"])["ret"]
               .apply(lambda x: (1 + x).prod() - 1).reset_index())
    monthly["date"] = monthly["ym"].dt.to_timestamp("M")
    wide = monthly.pivot(index="date", columns="code", values="ret").sort_index()
    logger.info(f"月度收益面板：{wide.shape[0]}月 × {wide.shape[1]}只")

    mom_vals = {}
    for i, t in enumerate(wide.index):
        if i < 13:
            continue
        sig_window = wide.iloc[i - 13 : i - 1]
        if len(sig_window) < 10:
            continue
        cumret   = (1 + sig_window).prod() - 1
        curr_ret = wide.iloc[i]
        valid    = cumret.dropna().index.intersection(curr_ret.dropna().index)
        if len(valid) < 50:
            continue
        sig = cumret[valid]
        ret = curr_ret[valid]
        w = ret[sig >= sig.quantile(0.80)]
        l = ret[sig <= sig.quantile(0.20)]
        if len(w) >= 5 and len(l) >= 5:
            mom_vals[t] = w.mean() - l.mean()

    mom = pd.Series(mom_vals, name="MOM").sort_index()
    logger.info(f"MOM因子：{len(mom)}个月")
    return mom


# ── 4. OLS + Newey-West ───────────────────────────────────────
def run_ols(y: pd.Series, X_df: pd.DataFrame, label: str = "") -> dict:
    X = sm.add_constant(X_df)
    ols = sm.OLS(y, X).fit()
    hac = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 4})

    alpha_m = ols.params["const"]
    alpha_a = (1 + alpha_m) ** 12 - 1
    t_hac   = hac.tvalues["const"]
    p_hac   = hac.pvalues["const"]
    beta_mkt = ols.params.get("MKT", np.nan)
    r2      = ols.rsquared
    n       = int(ols.nobs)

    result = {
        "label":       label,
        "n_months":    n,
        "alpha_annual": alpha_a,
        "t_alpha_hac": t_hac,
        "p_alpha_hac": p_hac,
        "beta_mkt":    beta_mkt,
        "r2":          r2,
        "params":      ols.params.to_dict(),
        "tvalues":     ols.tvalues.to_dict(),
    }
    return result


# ── 5. 打印结果表 ─────────────────────────────────────────────
def print_result(res: dict):
    a  = res["alpha_annual"]
    t  = res["t_alpha_hac"]
    p  = res["p_alpha_hac"]
    bm = res["beta_mkt"]
    r2 = res["r2"]
    n  = res["n_months"]

    sig_a  = "✅" if a >= 0.08 else "❌"
    sig_t  = "✅" if t >= 2.0  else ("⚠️" if t >= 1.5 else "❌")
    sig_bm = "📊"
    sig_r2 = "📊"

    logger.info(f"  样本：{n}个月")
    logger.info(f"  {sig_a}  年化Alpha（扣因子后） {a:+.1%}   [阈值>8%]")
    logger.info(f"  {sig_t}  Alpha t值 (HAC)       {t:+.2f}   [阈值>2，p={p:.3f}]")
    logger.info(f"  {sig_bm}  市场Beta              {bm:.3f}")
    logger.info(f"  {sig_r2}  R²                   {r2:.3f}")

    # 各因子载荷
    for k in ["SMB", "HML", "MOM"]:
        if k in res["params"]:
            logger.info(f"      β_{k} = {res['params'][k]:.3f}  (t={res['tvalues'][k]:.2f})")


# ── 主函数 ────────────────────────────────────────────────────
def main():
    logger.info("=" * 68)
    logger.info("Carhart 四因子归因 + 压力测试 + 条件Beta")
    logger.info("=" * 68)

    # 宇宙股票
    universe_df = pd.read_parquet(META / "universe_history.parquet")
    codes = sorted({c for _, row in universe_df.iterrows()
                    if row.get("codes") for c in row["codes"].split(",")})
    logger.info(f"宇宙：{len(codes)} 只股票")

    # 构建因子
    mom     = build_mom(codes)
    factors = load_factors(mom)
    strat   = load_strategy()

    # 对齐
    df = pd.concat([strat, factors], axis=1).dropna()
    df = df[(df.index >= START) & (df.index <= END)]
    logger.info(f"对齐后样本：{len(df)}个月，{df.index[0].strftime('%Y-%m')} → {df.index[-1].strftime('%Y-%m')}")

    # ─── A. 四因子全样本 ─────────────────────────────────────
    logger.info("\n" + "─" * 68)
    logger.info("A. Carhart 四因子（全样本 2019-2024）")
    logger.info("─" * 68)
    res_4f = run_ols(df["strat_excess"], df[["MKT", "SMB", "HML", "MOM"]], "四因子全样本")
    print_result(res_4f)

    # 对比：两因子（仅市场+动量）
    logger.info("\n  [参照] 两因子（MKT+MOM）：")
    res_2f = run_ols(df["strat_excess"], df[["MKT", "MOM"]], "两因子全样本")
    logger.info(f"    Alpha={res_2f['alpha_annual']:+.1%}  t={res_2f['t_alpha_hac']:.2f}  "
                f"Beta={res_2f['beta_mkt']:.3f}  R²={res_2f['r2']:.3f}")

    alpha_delta = res_2f["alpha_annual"] - res_4f["alpha_annual"]
    r2_delta    = res_4f["r2"] - res_2f["r2"]
    logger.info(f"    → SMB/HML净影响：Alpha下调 {alpha_delta:+.1%}，R² 上升 {r2_delta:+.3f}")

    # ─── B. 压力测试：剔除2020-2021 ──────────────────────────
    logger.info("\n" + "─" * 68)
    logger.info("B. 压力测试：剔除2020-2021（43个月 → 47个月→实际剩余）")
    logger.info("─" * 68)
    df_stress = df[~df.index.year.isin([2020, 2021])]
    logger.info(f"  剩余样本：{len(df_stress)}个月")
    if len(df_stress) >= 20:
        res_stress = run_ols(df_stress["strat_excess"],
                             df_stress[["MKT", "SMB", "HML", "MOM"]],
                             "剔除牛市")
        print_result(res_stress)
    else:
        logger.warning("样本不足20个月，跳过")

    # ─── C. 牛熊条件Beta ─────────────────────────────────────
    logger.info("\n" + "─" * 68)
    logger.info("C. 牛熊分段条件Beta")
    logger.info("─" * 68)

    bull_years = [2019, 2020, 2021, 2024]   # CSI 800 正收益年份
    bear_years = [2022, 2023]

    for label, years in [("牛市年份", bull_years), ("熊市年份", bear_years)]:
        sub = df[df.index.year.isin(years)]
        if len(sub) < 10:
            logger.info(f"  {label}: 样本不足，跳过")
            continue
        X = sm.add_constant(sub[["MKT"]])
        m = sm.OLS(sub["strat_excess"], X).fit()
        beta = m.params["MKT"]
        alpha_m = m.params["const"]
        alpha_a = (1 + alpha_m) ** 12 - 1
        r2 = m.rsquared
        logger.info(f"  {label} ({sorted(years)}，{len(sub)}个月):")
        logger.info(f"    Beta={beta:.3f}  年化Alpha={alpha_a:+.1%}  R²={r2:.3f}")

    # 整体条件Beta（按月市场涨跌分）
    df_bull_m = df[df["MKT"] >= 0]
    df_bear_m = df[df["MKT"] < 0]
    for label, sub in [("市场上涨月", df_bull_m), ("市场下跌月", df_bear_m)]:
        if len(sub) < 10:
            continue
        X = sm.add_constant(sub[["MKT"]])
        m = sm.OLS(sub["strat_excess"], X).fit()
        logger.info(f"  {label} ({len(sub)}个月): Beta={m.params['MKT']:.3f}  R²={m.rsquared:.3f}")

    # ─── 最终裁决 ─────────────────────────────────────────────
    logger.info("\n" + "═" * 68)
    logger.info("最终裁决")
    logger.info("═" * 68)

    a4f  = res_4f["alpha_annual"]
    t4f  = res_4f["t_alpha_hac"]
    astr = res_stress["alpha_annual"] if len(df_stress) >= 20 else None
    tstr = res_stress["t_alpha_hac"]  if len(df_stress) >= 20 else None

    verdict_parts = []

    if a4f >= 0.08 and t4f >= 2.0:
        verdict_parts.append(f"✅ 四因子Alpha={a4f:.1%} 显著（t={t4f:.2f}），剥离SMB/HML后选股工艺仍存在")
    elif a4f >= 0.05 and t4f >= 1.5:
        verdict_parts.append(f"⚠️ 四因子Alpha={a4f:.1%}，t={t4f:.2f}——达标但边缘，需更长历史验证")
    else:
        verdict_parts.append(f"❌ 四因子Alpha={a4f:.1%}，t={t4f:.2f}——不显著，超额主要来自风格暴露")

    if astr is not None:
        if astr >= 0.08 and tstr >= 1.5:
            verdict_parts.append(f"✅ 剔除牛市后Alpha={astr:.1%}（t={tstr:.2f}），策略扛打")
        elif astr >= 0.05:
            verdict_parts.append(f"⚠️ 剔除牛市后Alpha={astr:.1%}（t={tstr:.2f}），有所衰减但可接受")
        else:
            verdict_parts.append(f"❌ 剔除牛市后Alpha={astr:.1%}，依赖牛市明显")

    for v in verdict_parts:
        logger.info(f"  {v}")

    logger.info("═" * 68)


if __name__ == "__main__":
    sys.path.insert(0, str(BASE))
    main()
