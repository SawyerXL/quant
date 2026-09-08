"""
S0a 过滤器触发率+双池判别 (2026-09-08 晚间, 用户§0再分解) —
检验解释(a): 800等权池里"可剔除的坏票"是否太少。
测量(主窗口, 单路径offset 0, 双周调仓日):
  1. pit800池: vol20>5被剔除数/占比 (T8新臂口径, 引擎apply_filters同款逻辑)
  2. top60池(现网候选): 同款剔除率, 复现T5的1398/5581事件
  3. 双池各自的判别检验: 被剔除组 vs 未剔除组 前向20/60日收益, 按调仓日
     聚类的t检验 + 效应量(年化pp)
  4. pit800被剔除股票 ∩ top60池 的重叠率(800池的剔除是否集中在热门角落)
判读(事前钉死, 用户给定):
  剔除率 <3% 且/或 800池判别不显著 → 解释(a)成立: 过滤器不是失效,
  是800池没东西可剔, 价值与"有坏票的热池"绑定。
用法: python scripts/s0_filter_trigger.py > logs/s0_filter_trigger.log 2>&1
输出: logs/s0_filter_trigger.csv, logs/s0_discrimination_800.log
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
from backtest_config import DEFAULT_CONFIG
from backtest_engine import make_rebal_dates
from backtest_return_attribution import (WINDOWS, load_blacklist,
                                         load_pit_memberships, load_entry_map,
                                         members_on)
from scipy import stats

START, END = WINDOWS["main"]
LOAD_START = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
MAX_VOL20 = DEFAULT_CONFIG.max_vol20  # 5.0


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
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    sh = load_daily("000001", LOAD_START, END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    panel = panel[panel.index.isin(pd.to_datetime(cal))]
    ap = ap[ap.index.isin(pd.to_datetime(cal))]
    return panel, ap, cal


def vol20_of(panel, c, i):
    """T-1严格口径, 与引擎vol20_use_today=False一致"""
    cl = panel[c]
    hist = cl.iloc[:i + 1].dropna()
    if len(hist) < 60:
        return None
    rets = hist.pct_change()
    return float(rets.iloc[-21:-1].std() * 100)


def classify(panel, ap, i, codes):
    """返回 (rejected_vol20, accepted, skipped_hist, skipped_price)"""
    rej, acc, skip_h, skip_p = [], [], [], []
    for c in codes:
        if c not in panel.columns:
            continue
        cl = panel[c]
        cur = cl.iloc[i]
        if pd.isna(cur) or cur <= 0:
            skip_p.append(c)
            continue
        v = vol20_of(panel, c, i)
        if v is None:
            skip_h.append(c)
            continue
        (rej if v > MAX_VOL20 else acc).append(c)
    return rej, acc, skip_h, skip_p


def forward_returns(panel, c, j0):
    px = panel[c]
    n = len(px)
    p0 = px.iloc[j0]
    r20 = float(px.iloc[min(j0 + 20, n - 1)] / p0 - 1) if j0 + 20 < n else np.nan
    r60 = float(px.iloc[min(j0 + 60, n - 1)] / p0 - 1) if j0 + 60 < n else np.nan
    return r20, r60


def discriminate(panel, events, label):
    ev = pd.DataFrame(events).dropna(subset=["r20", "r60"])
    if ev.empty:
        print(f"[{label}] 无有效事件")
        return
    bydate = ev.pivot_table(index="date", columns="rejected",
                            values=["r20", "r60"], aggfunc="mean")
    diffs = pd.DataFrame({
        "d20": bydate["r20"].get(True, 0) - bydate["r20"].get(False, 0),
        "d60": bydate["r60"].get(True, 0) - bydate["r60"].get(False, 0),
    }).dropna()
    t20, p20 = stats.ttest_1samp(diffs["d20"], 0)
    t60, p60 = stats.ttest_1samp(diffs["d60"], 0)
    n_rej = len(ev[ev["rejected"]]); n_acc = len(ev[~ev["rejected"]])
    print(f"[{label}] 事件: 被剔除{n_rej} / 未剔除{n_acc}, 有效调仓日{len(diffs)}")
    print(f"[{label}] 前向20日组差 {diffs['d20'].mean()*100:+.2f}% "
          f"(年化{diffs['d20'].mean()*252/20*100:+.1f}pp) t={t20:.2f} p={p20:.3f}")
    print(f"[{label}] 前向60日组差 {diffs['d60'].mean()*100:+.2f}% "
          f"(年化{diffs['d60'].mean()*252/60*100:+.1f}pp) t={t60:.2f} p={p60:.3f}")
    return diffs


def main():
    panel, ap, cal = build_panels()
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    pit = load_pit_memberships()
    entry_map = load_entry_map()
    use_pit = pit

    rows = []
    ev800, ev60 = [], []
    total_overlap_rej = 0
    for d in base:
        i = panel.index.get_loc(pd.Timestamp(d))
        dstr = str(pd.Timestamp(d).date())

        # 现网候选池: 成交额top60 (pool_size*2)
        amt_avg = ap.iloc[max(0, i - 20):i].mean().dropna()
        pool60 = amt_avg.nlargest(60).index.tolist()
        rej60, acc60, sh60, sp60 = classify(panel, ap, i, pool60)

        # T8新臂池: PIT800成员 ∩ 面板
        members = [c for c in (members_on(d, use_pit, entry_map) or [])
                   if c in panel.columns]
        rej800, acc800, sh800, sp800 = classify(panel, ap, i, members)

        overlap = len(set(rej800) & set(pool60))
        total_overlap_rej += overlap
        r800 = len(rej800) / max(1, len(rej800) + len(acc800))
        r60 = len(rej60) / max(1, len(rej60) + len(acc60))
        rows.append({"date": dstr, "pool800_n": len(members),
                     "pool800_rej": len(rej800), "pool800_rej_rate": r800,
                     "pool60_n": len(pool60), "pool60_rej": len(rej60),
                     "pool60_rej_rate": r60, "overlap_800rej_in60": overlap})

        for c in rej800 + acc800:
            r20, r60f = forward_returns(panel, c, i)
            ev800.append({"date": dstr, "code": c, "rejected": c in set(rej800),
                          "r20": r20, "r60": r60f})
        for c in rej60 + acc60:
            r20, r60f = forward_returns(panel, c, i)
            ev60.append({"date": dstr, "code": c, "rejected": c in set(rej60),
                         "r20": r20, "r60": r60f})

    df = pd.DataFrame(rows)
    df.to_csv("logs/s0_filter_trigger.csv", index=False, encoding="utf-8-sig")
    print(f"=== 触发率汇总 (主窗口 {START}~{END}, 调仓日{len(base)}个) ===")
    print(f"pit800池: 平均成员{df['pool800_n'].mean():.0f}只, "
          f"vol20>{MAX_VOL20}被剔除率 {df['pool800_rej_rate'].mean()*100:.2f}% "
          f"(日均{df['pool800_rej'].mean():.1f}只), "
          f"剔除票与top60池日均重叠 {total_overlap_rej/len(base):.1f}只")
    print(f"top60池: 平均{df['pool60_n'].mean():.0f}只, 剔除率 "
          f"{df['pool60_rej_rate'].mean()*100:.2f}% "
          f"(日均{df['pool60_rej'].mean():.1f}只)")
    print("\n=== 判别检验(被剔除组 vs 未剔除组 前向收益) ===")
    print("--- 现网top60池(复现T5) ---")
    discriminate(panel, ev60, "top60")
    print("--- pit800池 ---")
    discriminate(panel, ev800, "pit800")
    print("\n落盘: logs/s0_filter_trigger.csv")


if __name__ == "__main__":
    main()
