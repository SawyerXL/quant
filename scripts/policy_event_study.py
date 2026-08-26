"""
政策层面事件研究：降准/降息/加息/M2 拐点 → 上证后续表现。

事件日来源：政策数值序列的变化点（准备金率"生效时间"、LPR/基准利率变化日、
M2同比方向拐点），全部自动标定，不依赖人工事件清单 —— 人工清单本机无可靠来源。

口径：事件日 D（政策已生效）→ 上证 D 起 1/5/20 日前瞻收益，对照无条件基准。
再加 MA200 分层：验证"政策效果依赖市场状态"的直觉。

用法: python scripts/policy_event_study.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily


def load_sh():
    d = load_daily("000001", "2007-01-01", "2026-08-26").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    cl = pd.to_numeric(d["close"], errors="coerce")
    ret = pd.Series(cl.pct_change() * 100, index=d.index)
    fwd = {n: (cl.shift(-n) / cl - 1) * 100 for n in (1, 5, 20)}
    return ret.dropna(), fwd


def to_trading(days, sh_index):
    """事件日落非交易日 → 映射到下一个交易日。"""
    out = []
    for d in days:
        pos = sh_index.searchsorted(d)
        out.append(sh_index[min(pos, len(sh_index) - 1)])
    return pd.DatetimeIndex(out)


def study(sh, fwd, events: pd.Series, name: str):
    """events: index=日期, 值=方向标签(如 '降准'/'升准'/'降息'/'加息')."""
    ev = events[events.index <= pd.Timestamp("2026-08-20")]
    print(f"\n【{name}】共 {len(ev)} 次")
    for label in sorted(set(ev)):
        days = to_trading(pd.DatetimeIndex(ev[ev == label].index), sh.index)
        base = {n: f"{fwd[n].mean():+.2f}%" for n in (1, 5, 20)}
        row = {n: fwd[n].reindex(days).mean() for n in (1, 5, 20)}
        wr = (fwd[20].reindex(days) > 0).mean() * 100
        print(f"  {label:<6}{len(days):>4}次  次日{row[1]:+.2f}%  5日{row[5]:+.2f}%  "
              f"20日{row[20]:+.2f}%  20日胜率{wr:.0f}%   [无条件: {base[1]}/{base[5]}/{base[20]}]")
        # MA200 分层(只看20日)
        m = sh.rolling(200).mean()
        above = pd.DatetimeIndex([d for d in days if sh[d] > m[d]])
        below = pd.DatetimeIndex([d for d in days if sh[d] <= m[d]])
        if len(above) >= 5:
            print(f"      ├ MA200上方 {len(above):>3}次: 20日 {fwd[20].reindex(above).mean():+.2f}% "
                  f"胜率{(fwd[20].reindex(above)>0).mean()*100:.0f}%")
        if len(below) >= 5:
            print(f"      └ MA200下方 {len(below):>3}次: 20日 {fwd[20].reindex(below).mean():+.2f}% "
                  f"胜率{(fwd[20].reindex(below)>0).mean()*100:.0f}%")


def cn_date(s):
    return pd.Timestamp(str(s).replace("年", "-").replace("月", "-").replace("日", ""))


def main():
    sh, fwd = load_sh()

    # 1. 存款准备金率调整
    rrr = ak.macro_china_reserve_requirement_ratio()
    ev = {}
    for _, r in rrr.iterrows():
        try:
            d = cn_date(r["生效时间"])
            # 方向用"调整前 vs 调整后"判断，别信幅度字符串里的加减号
            before = float(str(r["大型金融机构-调整前"]).replace("%", ""))
            after = float(str(r["大型金融机构-调整后"]).replace("%", ""))
            ev[d] = "降准" if after < before else "升准"
        except Exception:
            pass
    study(sh, fwd, pd.Series(ev), "存款准备金率调整(2007-2026)")

    # 2. LPR / 基准利率变化
    lpr = ak.macro_china_lpr()
    lpr["TRADE_DATE"] = pd.to_datetime(lpr["TRADE_DATE"])
    lpr = lpr.sort_values("TRADE_DATE")
    # RATE_2: 2019-08 LPR改革前为贷款基准利率; LPR1Y: 改革后
    ev2 = {}
    prev = None
    for _, r in lpr.iterrows():
        d = r["TRADE_DATE"]
        v = r["LPR1Y"] if d >= pd.Timestamp("2019-08-01") and pd.notna(r["LPR1Y"]) else r.get("RATE_2")
        if pd.isna(v):
            continue
        v = float(v)
        if prev is not None and abs(v - prev) > 1e-9:
            ev2[d] = "降息" if v < prev else "加息"
        prev = v
    study(sh, fwd, pd.Series(ev2), "LPR/基准利率调整(1991-2026)")

    # 3. M2 同比拐点(由降转升/由升转降), 公布滞后约10-15天, 用次月第一个交易日起算
    m2 = ak.macro_china_money_supply()
    m2 = m2.sort_values("月份")
    m2["yoy"] = pd.to_numeric(m2["货币和准货币(M2)-同比增长"], errors="coerce")
    m2 = m2.dropna(subset=["yoy"]).reset_index(drop=True)
    m2["chg"] = m2["yoy"].diff()
    ev3 = {}
    for _, r in m2.iterrows():
        d = pd.Timestamp(str(r["月份"]).replace("年", "-").replace("月份", "-01"))
        d = d + pd.DateOffset(months=1)   # 次月公布
        # 取次月第15个日历日后的第一个交易日近似为"市场得知日"
        d = d + pd.DateOffset(days=14)
        if r["chg"] > 0.1:
            ev3[d] = "M2回升"
        elif r["chg"] < -0.1:
            ev3[d] = "M2回落"
    study(sh, fwd, pd.Series(ev3), "M2同比拐点(2008-2026, 公布日近似次月15日)")


if __name__ == "__main__":
    main()
