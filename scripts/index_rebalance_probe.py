"""指数调仓效应测量 (2026-09-21, 检验账本 N=42, 口径预锁见账本)。

机制: 中证800 半年调样 → 指数基金被迫买卖 → 调入票相对调出票的
价格效应。事件=PIT 表相邻时点成分差; 度量=同一事件调入−调出的
配对差, 窗口 T-20→T-1 / T→T+20 / T-20→T+20; 收益=收盘价累计
(无成本, 测量性质)。判读(锁死): 总窗口均值>0 且块bootstrap
σ≥0.35 → 候选; 否则判死。block=21/B=2000/seed 与主回测登记同。
用法: python scripts/index_rebalance_probe.py > logs/index_rebalance_20260921.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily
from main_backtest_analysis import circ_block_boot

BLOCK, B, SEED = 21, 2000, 20260918
PRE, POST = 20, 20


def main():
    pit = pd.read_parquet("data_store/meta/csi800_pit_weights.parquet")
    pit["trade_date"] = pd.to_datetime(pit["trade_date"])
    dates = sorted(pit["trade_date"].unique())
    # 事件: 相邻时点的成分差(仅半年调样月会有大变化)
    events = []
    for d0, d1 in zip(dates, dates[1:]):
        s0 = set(pit[pit["trade_date"] == d0]["code"])
        s1 = set(pit[pit["trade_date"] == d1]["code"])
        added = sorted(s1 - s0)
        removed = sorted(s0 - s1)
        if added and removed:
            events.append((d1, added, removed))
    print(f"调样事件: {len(events)} 个")
    for d, a, r in events:
        print(f"  {d.date()}: 调入{len(a)} 调出{len(r)}")

    # 日历(用上证)
    sh = load_daily("SH000001", "2018-06-01", "2026-08-28")
    cal = sorted(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist())
    cal_idx = {d: i for i, d in enumerate(cal)}

    rows = []
    for d_eff, added, removed in events:
        dstr = d_eff.strftime("%Y-%m-%d")
        if dstr not in cal_idx:
            continue
        i0 = cal_idx[dstr]
        for tag, codes in (("in", added), ("out", removed)):
            for c in codes:
                df = load_daily(c, "2018-01-01", "2026-08-28")
                if df.empty:
                    continue
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
                cl = pd.to_numeric(df["close"], errors="coerce").dropna()
                # 对齐到调样日: 找 ≤ 生效日的最近交易日
                if not cl.index.isin(pd.to_datetime(cal)).any():
                    continue
                # 用日历定位窗口
                if i0 - PRE < 0 or i0 + POST >= len(cal):
                    continue
                pre_d = cal[i0 - PRE]
                post_d = cal[i0 + POST]
                eff_d = cal[i0]
                # 用 ≤d 最近bar(停牌对齐)
                def last_le(s, d):
                    x = s[s.index <= pd.Timestamp(d)]
                    return float(x.iloc[-1]) if len(x) else None
                p_pre = last_le(cl, pre_d)
                p_eff = last_le(cl, eff_d)
                p_post = last_le(cl, post_d)
                if not all(v is not None for v in (p_pre, p_eff, p_post)):
                    continue
                rows.append({"date": dstr, "tag": tag,
                             "code": c,
                             "pre": p_eff / p_pre - 1,
                             "post": p_post / p_eff - 1,
                             "total": p_post / p_pre - 1})

    df = pd.DataFrame(rows)
    print(f"有效样本: {len(df)} 行 (调入 {len(df[df.tag=='in'])} / "
          f"调出 {len(df[df.tag=='out'])})")
    # 配对差: 按事件日, 调入均值 − 调出均值
    g_in = df[df.tag == "in"].groupby("date")["total"].mean()
    g_out = df[df.tag == "out"].groupby("date")["total"].mean()
    pair = (g_in - g_out).dropna()
    print(f"配对事件数: {len(pair)}")
    mean_eff = float(pair.mean())
    boots = [np.mean(x) for x in circ_block_boot(pair.values)]
    se = float(np.std(boots))
    sigma = mean_eff / se if se > 0 else 0
    print(f"总窗口(±20日) 调入−调出: 均值 {mean_eff*100:+.2f}% "
          f"± {se*100:.2f}%(SE_boot) → σ={sigma:+.2f}")
    for win, col in (("预期(T-20→T-1)", "pre"), ("持续(T→T+20)", "post")):
        gi = df[df.tag == "in"].groupby("date")[col].mean()
        go = df[df.tag == "out"].groupby("date")[col].mean()
        p = (gi - go).dropna()
        b = [np.mean(x) for x in circ_block_boot(p.values)]
        print(f"{win}: 均值 {p.mean()*100:+.2f}% ± {np.std(b)*100:.2f}%")
    verdict = ("候选资格(调仓效应存在, 可评估事件策略)"
               if mean_eff > 0 and abs(sigma) >= 0.35
               else "判死不立项")
    print(f"判读(锁死): {verdict}")


if __name__ == "__main__":
    main()
