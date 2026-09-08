"""
T4/T5/T6 诊断 (2026-09-08 overnight) — 三任务共享面板构建, 分别输出:
  --task t4: 退市/长期停牌标记股票的持仓日占比 (A0 vs A1 vs A3)
  --task t5: 拥挤度过滤判别(被剔除组 vs 未剔除组, 20/60日前向收益+t检验+beta/波动率)
  --task t6: 打新市值时间加权(股票持仓市值逐日重构, CB不计, 沪深分离, bear清仓20日恢复)
口径: 主窗口 2019-01-01~2026-08-28, 单路径(offset 0) — 诊断性任务, 不用10路径摊平
(注明口径; T4/T6 是占比/市值类统计, 对调仓日不敏感; T5 按调仓日聚类t检验)。
用法: python scripts/t456_diag.py --task t4|t5|t6
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from data.storage import load_daily, load_meta
from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, apply_filters
from backtest_return_attribution import (WINDOWS, BASE, A0_OV, load_blacklist,
                                         load_pit_memberships, load_entry_map,
                                         members_on)

START, END = WINDOWS["main"]
LOAD_START = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")


def build_panels():
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, LOAD_START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 200:
                prices[code] = cl
                if len(amt) >= 200:
                    amounts[code] = amt
        except Exception:
            pass
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(amounts).sort_index()


def common():
    panel, ap = build_panels()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    # 日历从 LOAD_START 起(v3 OOS 教训: 日历短于预加载起点会删预加载段)
    sh = load_daily("000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    # 面板∩交易日历: 剔除603012等带来的假期行(全市场NaN日)
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    return panel, ap, ic, cal, base


def run_arm(panel, ap, ic, base, ov, pit_map=None, a0=False, diag=True):
    cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE, **A0_OV}
                         if a0 else
                         {**DEFAULT_CONFIG.to_dict(), **BASE,
                          **({} if ov == "no_timing" else ov)})
    cfg.diag_holding_spans = diag
    cfg.diag_exposure = diag
    ic_arg = None if ov == "no_timing" else ic
    nav, info = run_backtest(panel, ap, base, cfg, ic_arg,
                             pit_members=pit_map if a0 else None)
    return nav, info, cfg


def task_t4():
    """退市/长期停牌标记股票的持仓日占比 (A0 vs A1 vs A3)"""
    panel, ap, ic, cal, base = common()
    use_pit = load_pit_memberships()
    entry_map = load_entry_map()
    pit_map = {d: sorted(members_on(d, use_pit, entry_map) or []) for d in base}

    # 停牌标记(9/7三级审计口径): 连续相同收盘≥5(库内停牌=前值填充语义,
    # 9/7实测39只, 最长65日)。数据洞(连续NaN≥5)单独统计作数据质量注记
    # (3331只/62%有≥5日断流——本地库数据洞广泛, 引擎MTM语义=断流日
    # 零贡献, 收益稀释~良性)。上市前NaN段不是停牌——只统计首个有效收盘后。
    win = panel[panel.index >= pd.Timestamp(START)]
    long_susp, nan_hole = set(), set()
    for code in win.columns:
        s = win[code]
        first_valid = s.first_valid_index()
        if first_valid is None:
            continue
        s2 = s.loc[first_valid:]
        if s2.isna().rolling(5).sum().max() >= 5:
            nan_hole.add(code)
            continue
        flat = (s2.diff().abs() < 1e-9).astype(int)
        if flat.rolling(5).sum().max() >= 5:
            long_susp.add(code)
    delist = {r["code"] for _, r in
              pd.read_csv("logs/t7_delist_candidates.csv").iterrows()
              if r["baostock_last"]}
    print(f"停牌标记股票(相同收盘≥5): {len(long_susp)}只, "
          f"数据洞标记(NaN≥5): {len(nan_hole)}只, 退市标记: {len(delist)}只")

    out = []
    for name, ov, a0 in [("A0", "no_timing", True),
                         ("A1", "no_timing", False),
                         ("A3", {}, False)]:
        nav, info, cfg = run_arm(panel, ap, ic, base, ov, pit_map, a0)
        spans = info["holding_spans"]
        tot_days = susp_days = delist_days = hole_days = 0
        for code, ed, xd, w, ep in spans:
            days = panel.loc[ed:xd, code] if code in panel.columns else pd.Series()
            n = int(days.notna().sum())
            tot_days += n
            if code in long_susp:
                susp_days += n
            if code in delist:
                delist_days += n
            if code in nan_hole:
                hole_days += n
        out.append((name, tot_days, susp_days, delist_days, hole_days))
        print(f"{name}: 总持仓日{tot_days}, 停牌标记股{susp_days} "
              f"({susp_days/tot_days*100 if tot_days else 0:.2f}%), "
              f"退市标记股{delist_days}, 数据洞股{hole_days} "
              f"({hole_days/tot_days*100 if tot_days else 0:.2f}%)")
    with open("logs/t4_suspension_share.log", "w", encoding="utf-8") as f:
        f.write(f"停牌标记股(相同收盘≥5): {len(long_susp)}只\n"
                f"数据洞标记股(NaN≥5): {len(nan_hole)}只\n"
                f"退市标记股: {len(delist)}只\n")
        for name, tot, susp, dl, hole in out:
            f.write(f"{name}: 总持仓日{tot} 停牌占比{susp/tot*100 if tot else 0:.2f}% "
                    f"退市占比{dl/tot*100 if tot else 0:.2f}% "
                    f"数据洞股占比{hole/tot*100 if tot else 0:.2f}%\n")
    print("落盘: logs/t4_suspension_share.log")


def task_t5():
    """拥挤度过滤判别: 被剔除(vol20>5) vs 未剔除, 前向20/60日收益"""
    panel, ap, ic, cal, base = common()
    pool2 = 60  # pool_size*2
    events = []
    for d in base:
        i = panel.index.get_loc(pd.Timestamp(d))
        amt_avg = ap.iloc[max(0, i - 20):i].mean().dropna()
        pool = amt_avg.nlargest(pool2).index.tolist()
        rejected, accepted = [], []
        for c in pool:
            if c not in panel.columns:
                continue
            cl = panel[c]
            cur = cl.iloc[i]
            if pd.isna(cur) or cur <= 0:
                continue
            hist = cl.iloc[:i + 1].dropna()
            if len(hist) < 60:
                continue
            rets = hist.pct_change()
            win = rets.iloc[-21:-1]  # T-1严格(与引擎vol20_use_today=False一致)
            vol20 = win.std() * 100
            (rejected if vol20 > DEFAULT_CONFIG.max_vol20 else accepted).append(c)
        for c, rej in [(c, True) for c in rejected] + [(c, False) for c in accepted]:
            px = panel[c]
            j0 = px.index.get_loc(pd.Timestamp(d))
            p0 = px.iloc[j0]
            r20 = float(px.iloc[min(j0 + 20, len(px) - 1)] / p0 - 1) if j0 + 20 < len(px) else np.nan
            r60 = float(px.iloc[min(j0 + 60, len(px) - 1)] / p0 - 1) if j0 + 60 < len(px) else np.nan
            events.append({"date": d, "code": c, "rejected": rej, "r20": r20, "r60": r60})
    ev = pd.DataFrame(events).dropna(subset=["r20", "r60"])
    # 按调仓日聚类的组间差 + t检验
    bydate = ev.pivot_table(index="date", columns="rejected", values=["r20", "r60"],
                            aggfunc="mean")
    diffs = pd.DataFrame({
        "d20": bydate["r20"][True] - bydate["r20"][False],
        "d60": bydate["r60"][True] - bydate["r60"][False],
    }).dropna()
    from scipy import stats
    t20, p20 = stats.ttest_1samp(diffs["d20"], 0)
    t60, p60 = stats.ttest_1samp(diffs["d60"], 0)
    ann20 = diffs["d20"].mean() * 252 / 20
    ann60 = diffs["d60"].mean() * 252 / 60
    # beta/波动率: 全窗口每票 vs 指数
    icw = ic[(ic.index >= pd.Timestamp(START)) & (ic.index <= pd.Timestamp(END))]
    icr = icw.pct_change().dropna()
    betas, vols = {}, {}
    win = panel[panel.index >= pd.Timestamp(START)]
    for c in set(ev["code"]):
        r = win[c].pct_change().dropna()
        if len(r) < 100:
            continue
        r = r.reindex(icr.index).dropna()
        if len(r) < 100:
            continue
        b = float(np.polyfit(icr.reindex(r.index), r, 1)[0])
        betas[c] = b
        vols[c] = float(r.std() * np.sqrt(252))
    for lab, rej in [("被剔除", True), ("未剔除", False)]:
        cs = set(ev[ev["rejected"] == rej]["code"]) & set(betas)
        print(f"{lab}组: 事件{len(ev[ev['rejected']==rej])}个, 平均beta="
              f"{np.mean([betas[c] for c in cs]):.2f}, 平均年化波动="
              f"{np.mean([vols[c] for c in cs])*100:.0f}%")
    print(f"前向20日: 被剔除组−未剔除组 均差{diffs['d20'].mean()*100:+.2f}% "
          f"(年化{ann20*100:+.1f}pp), t={t20:.2f} p={p20:.3f}")
    print(f"前向60日: 被剔除组−未剔除组 均差{diffs['d60'].mean()*100:+.2f}% "
          f"(年化{ann60*100:+.1f}pp), t={t60:.2f} p={p60:.3f}")
    verdict = ("真alpha(被剔除组显著跑输)" if (p20 < 0.05 or p60 < 0.05)
               or (ann20 < 0 and ann60 < 0 and abs(ann20) > 0.02 and abs(ann60) > 0.02)
               else "无显著差异→隐性降杠杆")
    print(f"判读(事前钉死): {verdict}")
    with open("logs/t5_filter_discrimination.log", "w", encoding="utf-8") as f:
        f.write(f"事件数: {len(ev)} (被剔除{len(ev[ev['rejected']])}/未剔除{len(ev[~ev['rejected']])})\n"
                f"前向20日组差: {diffs['d20'].mean()*100:+.2f}% 年化{ann20*100:+.1f}pp t={t20:.2f} p={p20:.3f}\n"
                f"前向60日组差: {diffs['d60'].mean()*100:+.2f}% 年化{ann60*100:+.1f}pp t={t60:.2f} p={p60:.3f}\n"
                f"判读: {verdict}\n")
    print("落盘: logs/t5_filter_discrimination.log")


def task_t6():
    """打新市值时间加权: A3 持仓市值逐日重构(沪深分离/CB不计/bear20日恢复)。
    口径(2026-09-08 二轮): 总市值水平用 exposure_ts(每日实际权重×nav×资金,
    精确), 沪深拆分用 holding_spans 重构的份额比(入场权重×价格路径);
    打新规则: 沪≥1万/深≥0.5万, T-2~T-21的20日滚动日均(近似20日均)。"""
    panel, ap, ic, cal, base = common()
    nav, info, cfg = run_arm(panel, ap, ic, base, {}, pit_map=None, a0=False)
    # 精确总市值: exposure_ts (date, actual_weight, target, cumul_nav)
    exp = [(pd.Timestamp(d), w, n) for d, w, _t, n in info["exposure_ts"]
           if d >= START]
    tot = pd.Series({d: w * n * cfg.initial_capital for d, w, n in exp})
    # 沪深份额比: spans 重构(水平不可靠, 比例可用)
    spans = info["holding_spans"]
    sh_raw = pd.Series(0.0, index=tot.index)
    sz_raw = pd.Series(0.0, index=tot.index)
    for code, ed, xd, w, ep in spans:
        if code not in panel.columns:
            continue
        seg = panel.loc[ed:xd, code].ffill().fillna(0.0)
        val = (w * (seg / ep) * cfg.initial_capital) if (ep and ep > 0) \
            else (w * cfg.initial_capital)
        tgt = sh_raw if str(code).startswith("6") else sz_raw
        tgt.loc[val.index] = tgt.loc[val.index] + val
    raw_tot = sh_raw + sz_raw
    sh_share = (sh_raw / raw_tot.replace(0, np.nan)).fillna(0.5)
    sh_val = tot * sh_share
    sz_val = tot - sh_val
    roll20 = tot.rolling(20).mean()
    sh_roll = sh_val.rolling(20).mean()
    sz_roll = sz_val.rolling(20).mean()
    # 打新资格: 20日滚动日均市值达标(沪1万/深0.5万); 两市合计口径=都达标
    sh_ok = (sh_roll >= 1e4).mean()
    sz_ok = (sz_roll >= 0.5e4).mean()
    print(f"A3 单组(50万名义)实际持仓市值时间加权: 总体日均{tot.mean()/1e4:.1f}万, "
          f"沪{sh_val.mean()/1e4:.1f}万 深{sz_val.mean()/1e4:.1f}万")
    print(f"市值>0交易日占比: {(tot > 0).mean()*100:.0f}% "
          f"(bear清仓段{(tot == 0).mean()*100:.0f}%)")
    print(f"打新资格(20日滚动均): 沪达标{sh_ok*100:.0f}% 深达标{sz_ok*100:.0f}% "
          f"日均市值中位{roll20.median()/1e4:.0f}万")
    print(f"vs 名义50万: 时间加权占比{tot.mean()/cfg.initial_capital*100:.0f}% "
          f"(两组合计vs名义100万: {tot.mean()*2/1e6*100:.0f}%)")
    tot.to_csv("logs/t6_daily_stock_mv.csv", header=["mv"], encoding="utf-8-sig")
    with open("logs/t6_daily_stock_mv_summary.log", "w", encoding="utf-8") as f:
        f.write(f"单组日均市值{tot.mean()/1e4:.1f}万 沪{sh_val.mean()/1e4:.1f}万 "
                f"深{sz_val.mean()/1e4:.1f}万\n")
        f.write(f"零市值日{(tot == 0).mean()*100:.0f}% 沪资格{sh_ok*100:.0f}% "
                f"深资格{sz_ok*100:.0f}%\n")
        f.write(f"两组合计时间加权市值占名义100万: {tot.mean()*2/1e6*100:.0f}%\n")
    print("落盘: logs/t6_daily_stock_mv.csv / logs/t6_daily_stock_mv_summary.log")


if __name__ == "__main__":
    import argparse
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--task", required=True, choices=["t4", "t5", "t6"])
    args = ap_.parse_args()
    {"t4": task_t4, "t5": task_t5, "t6": task_t6}[args.task]()
