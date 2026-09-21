"""尾盘强势股隔夜溢价探针 (2026-09-17, 检验账本 N=41, 口径预锁见账本)。

测视频"八步法"的可测内核: T 日涨幅3~5%+量比>1+近20日涨停基因的
主板非ST票, T收盘买入→T+1开盘卖出的隔夜净收益(减13bp双边成本),
对照=同日全部主板非ST票同口径。换手/市值/分时/买点四步数据不可得
声明出局。盲区: 14:30可观察性以收盘近似(乐观); 停牌卖不出未建模(乐观)。
用法: python scripts/overnight_gap_probe.py > logs/overnight_gap_20260917.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_return_attribution import WINDOWS, load_blacklist
from smoke_t1_open import build_panels

START, END = WINDOWS["main"]
COMMISSION = 0.0013
LIMIT_UP = 9.7          # 主板涨停日涨幅阈值(容差)
RISE_LO, RISE_HI = 3.0, 5.0
VOL_RATIO = 1.0         # 量比>1
GENE_LOOKBACK = 20      # 涨停基因回看(不含当日)


def build_volume_panel(panel):
    """与 build_panels 同口径补 volume 面板(量比用)。"""
    load_start = (pd.Timestamp(START) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    meta = load_meta("stock_info_full")
    blacklist = load_blacklist()
    codes = [c for c in meta["code"].tolist() if c not in blacklist] \
        if not meta.empty else []
    vols = {}
    for code in codes:
        try:
            d = load_daily(code, load_start, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            v = pd.to_numeric(d.get("volume"), errors="coerce")
            if len(v) >= 200:
                vols[code] = v
        except Exception:
            pass
    vp = pd.DataFrame(vols).sort_index()
    vp = vp[vp.index.isin(panel.index)]
    return vp


def main():
    panel, open_panel, ap, cal = build_panels()
    vp = build_volume_panel(panel)
    meta = load_meta("stock_info_full")
    st = set(meta.loc[meta["is_st"].astype(str).str.lower() == "true",
                       "code"].tolist()) if not meta.empty else set()
    mainboard = [c for c in panel.columns
                 if (str(c).startswith(("60", "00")) and c not in st)]
    sub = panel[mainboard]
    opensub = open_panel[mainboard]
    vsub = vp[mainboard]

    close = sub.astype(float)
    op = opensub.astype(float)
    vol = vsub.astype(float)
    ret = close.pct_change()
    gene = ret.rolling(GENE_LOOKBACK).max() >= LIMIT_UP / 100
    gene = gene.shift(1)                     # 不含当日
    vol_ratio = vol / vol.rolling(5).mean().shift(1)

    # T日入选: 涨幅3~5% ∧ 量比>1 ∧ 涨停基因
    mask = ((ret * 100 >= RISE_LO) & (ret * 100 <= RISE_HI)
            & (vol_ratio > VOL_RATIO) & gene)
    # 隔夜收益: T+1开/T收-1 (T日入选行)
    gap = op.shift(-1) / close - 1
    sel_gap = gap[mask]
    ctl = gap.shift(1)                       # 对照与入选对齐到同日
    ctl_gap = ctl[mask]

    n_picks = int(mask.sum().sum())
    n_days = int((mask.sum(axis=1) > 0).sum())
    sel = sel_gap.stack().dropna()
    ctlv = ctl_gap.stack().dropna()

    sel_net = sel - COMMISSION
    ctl_net = ctlv - COMMISSION

    def stats(s, label):
        if s.empty:
            print(f"{label}: 无样本"); return
        ann = (1 + s).prod() ** (252 / max(len(s), 1)) - 1
        sharpe = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0
        print(f"{label}: n={len(s)} 均值{s.mean()*100:+.3f}% "
              f"中位{s.median()*100:+.3f}% 高开概率{(s+COMMISSION>0).mean()*100:.1f}% "
              f"年化(几何){ann*100:+.2f}% 日频夏普{sharpe:.2f}")

    print(f"主窗 {START}~{END} | 主板非ST {len(mainboard)} 只")
    print(f"执行天数 {n_days} | 票日样本 {n_picks} | 日均 {n_picks/max(n_days,1):.1f} 只")
    print(f"入选股票数(出现过): {int((mask.sum(axis=0)>0).sum())}")
    stats(sel_net, "入选组(减13bp)")
    stats(ctl_net, "对照同窗(减13bp)")
    print(f"高开概率差: 组−对照 = "
          f"{(sel+COMMISSION>0).mean()*100-(ctlv+COMMISSION>0).mean()*100:+.1f}pp")
    # 判读(预锁): 组净gap>0 且概率差>+5pp=立项 / 组≤0 或概率差≤5pp=判死
    m_sel, m_ctl = sel_net.mean(), ctl_net.mean()
    prob_diff = (sel + COMMISSION > 0).mean() - (ctlv + COMMISSION > 0).mean()
    if m_sel > 0 and prob_diff > 0.05:
        verdict = "内核有正溢价(组净gap>0 且概率差>+5pp) → 登记继续"
    elif m_sel <= 0 or prob_diff <= 0.05:
        verdict = "判死不立项(组净gap≤0 或概率差≤5pp)"
    else:
        verdict = "不可分辨"
    print(f"判读(预锁): {verdict}")


if __name__ == "__main__":
    main()
