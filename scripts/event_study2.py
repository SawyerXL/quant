"""
补验两个"没验证过"的：地产政策事件→A股、美联储利率决议→A股。

A. 地产政策: 事件日期经网络搜索确认(2015-330/2020三道红线/2022保交楼三连/
   2024-517/2026-0828房贷新政), 测上证+万科+保利的D+1/5/20, 拆gap/intraday。
B. 美联储: 用联邦基金利率序列(ak.macro_bank_usa_interest_rate, 有效段1982-2025)
   变化点自动标定加息/降息决议, 测上证1/5/20日, 按MA200分层。
用法: python scripts/event_study2.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import akshare as ak
from data.storage import load_daily

PROPERTY = [
    ("330新政", "2015-03-30", 1),
    ("三道红线座谈会", "2020-08-20", -1),
    ("政治局保交楼", "2022-07-28", 1),
    ("专项借款保交楼", "2022-08-19", 1),
    ("金融十六条", "2022-11-11", 1),
    ("保函置换", "2022-11-14", 1),
    ("517新政", "2024-05-17", 1),
    ("房贷40年+现房销售", "2026-08-28", 1),
]


def a_frame(code):
    d = load_daily(code, "2014-01-01", "2026-08-28").sort_values("date")
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    d["open"] = pd.to_numeric(d["open"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    f = pd.DataFrame({
        "close": d["close"],
        "ret": d["close"].pct_change() * 100,
        "gap": (d["open"] / d["close"].shift(1) - 1) * 100,
        "intra": (d["close"] / d["open"] - 1) * 100,
    }).dropna()
    return f


def fwd_ret(d, n):
    return (d["close"].shift(-n) / d["close"] - 1) * 100


def study_property():
    print("=" * 74)
    print("A. 地产政策事件 → 上证/万科/保利 (D=事件后首个交易日)")
    print("=" * 74)
    for code, name in [("000001", "上证"), ("000002", "万科A"), ("600048", "保利发展")]:
        f = a_frame(code)
        print(f"\n--- {name} {code} (对照: 收{f['ret'].mean():+.2f}% 跳空{f['gap'].mean():+.2f}% 盘中{f['intra'].mean():+.2f}%)")
        # 个股早期数据有洞(万科2015段缺失, 事件会被错映到最近bar), 只测2020+事件
        events = PROPERTY if code == "000001" else [e for e in PROPERTY if e[1] >= "2020-01-01"]
        rows = []
        for ev, day, sign in events:
            i = f.index.searchsorted(pd.Timestamp(day))
            if i < len(f) - 21:
                D = f.index[i]
                rows.append({
                    "ev": ev, "sign": sign, "gap": f["gap"].loc[D],
                    "intra": f["intra"].loc[D],
                    "f5": (f["close"].shift(-5).loc[D] / f["close"].loc[D] - 1) * 100,
                    "f20": (f["close"].shift(-20).loc[D] / f["close"].loc[D] - 1) * 100,
                })
        r = pd.DataFrame(rows)
        for sign, lab in [(1, "利多"), (-1, "收紧")]:
            s = r[r["sign"] == sign]
            if len(s) == 0:
                continue
            print(f"  {lab}政策 {len(s)}次: 跳空{s['gap'].mean():+.2f}% 盘中{s['intra'].mean():+.2f}% "
                  f"5日{s['f5'].mean():+.2f}% 20日{s['f20'].mean():+.2f}% "
                  f"(20日胜率{(s['f20']>0).mean()*100:.0f}%)")
        print("  明细:", [(e, f"{g:+.1f}/{it:+.1f}") for e, g, it in zip(r["ev"], r["gap"], r["intra"])])


def study_fed():
    print("\n" + "=" * 74)
    print("B. 美联储利率决议 → 上证 (决议从联邦基金利率序列变化点自动标定)")
    print("=" * 74)
    fed = ak.macro_bank_usa_interest_rate()
    fed = fed.dropna(subset=["今值"])
    fed["日期"] = pd.to_datetime(fed["日期"])
    fed = fed.sort_values("日期")
    fed["chg"] = fed["今值"].diff()

    sh = load_daily("000001", "2014-01-01", "2026-08-28").sort_values("date")
    sh["date"] = pd.to_datetime(sh["date"])
    sh = sh.set_index("date")
    cl = pd.to_numeric(sh["close"], errors="coerce")
    ma200 = cl.rolling(200).mean()
    print(f"利率序列有效段: {str(fed['日期'].min())[:10]} → {str(fed['日期'].max())[:10]}, "
          f"加息{int((fed['chg']>0).sum())}次 降息{int((fed['chg']<0).sum())}次\n")
    print(f"{'方向':<6}{'次数':>5}{'次日':>8}{'5日':>8}{'20日':>9}{'20日胜率':>9}  MA200上方/下方20日")
    for sign, lab in [(1, "加息"), (-1, "降息")]:
        ev = fed[fed["chg"] * sign > 0]["日期"]
        # 决议在美东14:00(北京次日凌晨2点), A股次日已知 → 映射到次日A股交易日
        vals = {"d1": [], "d5": [], "d20": [], "above": [], "below": []}
        for dt in ev:
            i = sh.index.searchsorted(dt)
            if i < len(sh) - 21:
                D = sh.index[i]
                c = cl.loc[D]
                m = ma200.loc[D] if pd.notna(ma200.loc[D]) else None
                vals["d1"].append((cl.shift(-1).loc[D] / c - 1) * 100)
                vals["d5"].append((cl.shift(-5).loc[D] / c - 1) * 100)
                vals["d20"].append((cl.shift(-20).loc[D] / c - 1) * 100)
                if m and c > m:
                    vals["above"].append((cl.shift(-20).loc[D] / c - 1) * 100)
                elif m:
                    vals["below"].append((cl.shift(-20).loc[D] / c - 1) * 100)
        n = len(vals["d1"])
        if n == 0:
            continue
        a = f"{np.mean(vals['above']):+.2f}%" if vals["above"] else "n/a"
        b = f"{np.mean(vals['below']):+.2f}%" if vals["below"] else "n/a"
        print(f"{lab:<6}{n:>5}{np.mean(vals['d1']):>+7.2f}%{np.mean(vals['d5']):>+7.2f}%"
              f"{np.mean(vals['d20']):>+8.2f}%{(pd.Series(vals['d20'])>0).mean()*100:>8.0f}%  {a}/{b}")


if __name__ == "__main__":
    study_property()
    study_fed()
