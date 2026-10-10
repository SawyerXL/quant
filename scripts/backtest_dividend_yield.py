"""高分红持有年化测量 (2026-10-10, N=50 预登记, 判据见 logs/overnight_open_questions.md)。

为什么: 用户问"高分红的股票持有年化如何"——08-31 优先序里"分红质量"是
预算内正交信号方向, 本脚本出第一步测量数(十档单调性+红利TOP30 vs 基准),
不立项不裁决。
口径: PIT 中证800 月频重平衡; 股息率=近365天(除权日∈(t-365,t])每股现金
股息÷价(t); 等权; 成本 0.30%/边; return_basis=price(本地库价格口径, 与 A3
可比)。基准=同 harness 的 800 等权(全成员, 不筛分红)。
用法: python scripts/backtest_dividend_yield.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from data.storage import load_daily, load_meta  # noqa: E402

COST = 0.003
START, END = "2019-01-01", "2026-10-09"
AS_OF = "2026-10-10"


def load_panel():
    pit = load_meta("csi800_pit_weights")
    pit["trade_date"] = pd.to_datetime(pit["trade_date"])
    codes = sorted(pit["code"].astype(str).unique())
    prices = {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            if len(cl) >= 250:
                prices[code] = cl
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    print(f"价格面板 {panel.shape[0]}天×{panel.shape[1]}只")
    return panel, pit


def build_yield_series(panel):
    """每 (t, code) 的近365天除权现金股息(元/股), searchsorted 向量化。"""
    div = pd.read_parquet("data_store/meta/dividends_sina.parquet")
    div["code"] = div["code"].astype(str)
    div["ex"] = pd.to_datetime(div["除权除息日"], errors="coerce")
    div["per_share"] = pd.to_numeric(div["派息"], errors="coerce") / 10.0
    div = div.dropna(subset=["ex", "per_share"])
    div = div[(div["per_share"] > 0) & (div["per_share"] <= 100)]  # 值域 sanity
    dates = panel.index
    dt64 = dates.values.astype("datetime64[ns]")
    ys = pd.DataFrame(0.0, index=dates, columns=panel.columns)
    for code in panel.columns:
        sub = div[div["code"] == code]
        if sub.empty:
            continue
        ex = sub["ex"].values.astype("datetime64[ns]")
        ps = sub["per_share"].values
        order = np.argsort(ex)
        ex, ps = ex[order], ps[order]
        cum = np.cumsum(ps)
        for i, t in enumerate(dt64):
            lo, hi = t - np.timedelta64(365, "D"), t
            j_hi = np.searchsorted(ex, hi, side="right")
            j_lo = np.searchsorted(ex, lo, side="right")
            if j_hi > j_lo:
                ys.iloc[i, ys.columns.get_loc(code)] = cum[j_hi - 1] - (cum[j_lo - 1] if j_lo else 0)
    return ys


def metrics(rets):
    r = pd.Series(rets)
    if len(r) < 250:
        return None
    ann = (1 + r).prod() ** (252 / len(r)) - 1
    vol = r.std() * np.sqrt(252)
    sr = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    return ann, vol, sr, dd


def run_group(panel, mdates, sel_mask):
    """sel_mask: 每日可选成员布尔(已含宇宙/价格/分红过滤)。月频重平衡等权。"""
    sel_mask = sel_mask.fillna(False).astype(bool)  # 防御: 任何 NaN 污染在此截断
    rets, nav = [0.0], 1.0
    prev_w = None
    tos = []
    for i in range(1, len(panel.index)):
        t = panel.index[i]
        if t in mdates:
            valid = sel_mask.loc[t]
            valid = valid[valid]
            if len(valid) == 0:
                rets.append(0.0)
                nav *= 1.0
                prev_w = None
                continue
            w = pd.Series(1.0 / len(valid), index=valid.index)
            if prev_w is not None:
                to = (w - prev_w).abs().sum() / 2
                nav *= (1 - COST * to)
                tos.append(to)
            prev_w = w
        elif prev_w is not None and i >= 2:
            # 非重平衡日: 权重按前一日价格漂移(仅 i>=2 有前前日);
            # 停牌股价格 NaN → 按价格不变处理, 否则 NaN 毒化全月权重
            pr = panel.loc[panel.index[i - 1], prev_w.index]
            pp = panel.loc[panel.index[i - 2], prev_w.index]
            drift = (pr / pp).fillna(1.0)
            prev_w = (prev_w * drift)
            prev_w = prev_w / prev_w.sum()
        r = 0.0
        if prev_w is not None:
            ret1 = (panel.loc[t, prev_w.index] / panel.loc[panel.index[i - 1], prev_w.index]).fillna(1.0)
            r = ret1.values @ prev_w.values - 1
        rets.append(r)
        nav *= (1 + r)
    return pd.Series(rets, index=panel.index), nav, (np.mean(tos) if tos else 0)


def main():
    panel, pit = load_panel()
    ys = build_yield_series(panel)
    print(f"分红事件覆盖: {ys.gt(0).any(axis=1).sum()} 天有除息数据")

    # PIT 成员标志(每交易日沿用最近一次 trade_date 名单)
    pit_dates = sorted(pit["trade_date"].unique())
    flags = pd.DataFrame(False, index=panel.index, columns=panel.columns)
    for i, t in enumerate(panel.index):
        pd_i = [d for d in pit_dates if d <= t]
        if not pd_i:
            continue
        members = pit[pit["trade_date"] == pd_i[-1]]
        # 只标价格面板内存在的列, 否则 .loc 赋值会造 NaN 新列污染布尔掩码
        mvals = [c for c in members["code"].astype(str).values if c in panel.columns]
        flags.loc[t, mvals] = True
    print(f"PIT 成员月均 {flags.sum(axis=1).mean():.0f} 只")

    mdates = panel.index.to_series().groupby(panel.index.to_period("M")).first()
    mdates = set(panel.index[panel.index.isin(mdates.values)])

    # ── 基准: 800等权(全成员, 不筛分红) ──
    base_mask = flags & panel.notna()
    rets, nav, to = run_group(panel, mdates, base_mask)
    m = metrics(rets)
    if m:
        print(f"基准800等权  年化{m[0]*100:+.2f}% 波动{m[1]*100:.1f}% 夏普{m[2]:.2f} "
              f"回撤{m[3]*100:.1f}% 月换手{to*100:.0f}%")

    # ── 股息率十档(仅 PIT 成员中 yield>0 者分档) ──
    dec_masks = {k: pd.DataFrame(False, index=panel.index, columns=panel.columns) for k in range(10)}
    for t in mdates:
        valid = flags.loc[t] & panel.loc[t].notna() & (ys.loc[t] > 0)
        if valid.sum() < 100:
            continue
        y = ys.loc[t][valid]
        dec = pd.qcut(y, 10, labels=False, duplicates="drop")
        for k in range(10):
            codes = dec[dec == k].index
            if len(codes) >= 5:
                dec_masks[k].loc[t, codes] = True

    print("\n股息率十档(月频重平衡, 成本0.30%/边):")
    for k in sorted(dec_masks):
        sel = dec_masks[k] & panel.notna()
        rets, nav, to = run_group(panel, mdates, sel)
        m = metrics(rets)
        if not m:
            continue
        yv = [ys.loc[t, c] / panel.loc[t, c]
              for t in mdates if dec_masks[k].loc[t].any()
              for c in dec_masks[k].columns[dec_masks[k].loc[t]]]
        avg_yield = np.mean(yv) * 100 if yv else 0  # trailing-12m 股息率, 已是年化口径
        print(f"D{k+1:<2} 年化{m[0]*100:+.2f}% 波动{m[1]*100:.1f}% 夏普{m[2]:.2f} "
              f"回撤{m[3]*100:.1f}% 换手{to*100:.0f}%/月 股息率{avg_yield:.1f}%/年")

    # ── 红利TOP30(与 pool30 可比) ──
    top_mask = pd.DataFrame(False, index=panel.index, columns=panel.columns)
    for t in mdates:
        valid = flags.loc[t] & panel.loc[t].notna() & (ys.loc[t] > 0)
        if valid.sum() < 30:
            continue
        top = ys.loc[t][valid].sort_values(ascending=False).head(30).index
        top_mask.loc[t, top] = True
    sel = top_mask & panel.notna()
    rets, nav, to = run_group(panel, mdates, sel)
    m = metrics(rets)
    if m:
        yv = [ys.loc[t, c] / panel.loc[t, c]
              for t in mdates if top_mask.loc[t].any()
              for c in top_mask.columns[top_mask.loc[t]]]
        avg_yield = np.mean(yv) * 100 if yv else 0
        print(f"红利TOP30   年化{m[0]*100:+.2f}% 波动{m[1]*100:.1f}% 夏普{m[2]:.2f} "
              f"回撤{m[3]*100:.1f}% 换手{to*100:.0f}%/月 股息率{avg_yield:.1f}%/年")

    # 2019-2025 全窗(剔 2026 部分年)敏感检查: 关键组截断重算
    cut = pd.Timestamp("2025-12-31")
    print("\n── 2019-2025 敏感检查(剔 2026 部分年) ──")
    for tag, mk in [("基准800等权", base_mask), ("红利TOP30", sel)] + \
            [(f"D{k+1}", dec_masks[k] & panel.notna()) for k in sorted(dec_masks)]:
        sub = mk.fillna(False).astype(bool).loc[:cut]
        rets, nav, to = run_group(panel.loc[:cut], mdates, sub)
        m = metrics(rets)
        if m:
            print(f"{tag:<14} 年化{m[0]*100:+.2f}% 波动{m[1]*100:.1f}% 夏普{m[2]:.2f} 回撤{m[3]*100:.1f}%")

    print(f"\nas_of={AS_OF}, PIT800, 月频, 成本0.30%/边, return_basis=price(不含股息再投资)")


if __name__ == "__main__":
    main()
