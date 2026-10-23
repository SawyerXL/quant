"""高风险卫星纸面 — CB-only（2026-10-21，用户"核心不动+高风险卫星"框架）。

口径（只读+纸面，不下单、不碰实盘账户）：
- 卫星 = 与核心 CB 腿同一策略（同一 25 只、同一月度 snapdelay 信号），
  1x notional（不杠杆；用户允许 ≤3x，但 1x 已把卫星风险预算用在 CB 上）。
- 规模 = 核心名义(160 万)的 10%/20%/30%（16/32/48 万），三档并行纸面。
- 卫星 NAV = S × (nav_cb / nav_cb_start)；核心 = logs/qmt_nav_history.parquet
  的 nav_combined（100 万股票 + 60 万 CB，qmt_nav_track 每日 15:35 重建）。
- 合并 NAV = (160万×nav_combined + S×nav_cb/nav_cb_start)/(160万+S)。
  **假设卫星资金是额外资金**（不是从核心划走）；若从核心划走，合并口径不同。
- 卫星成本/滑点沿用核心 CB 腿的 NAV（同一策略），实际独立账户会有小额
  lot 取整差异；前向纸面是近似，非承诺。
- 首次运行初始化 start_date/nav_cb_start；之后每日追加。≥60 个交易日后
  按"卫星 DD ≤ 卫星资本、合并 DD ≤ 15%、夏普"评估；不达预期不实盘。

用法: python scripts/satellite_cb_paper.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

NAV = Path("logs/qmt_nav_history.parquet")
STATE = Path("logs/satellite_cb_paper_state.json")
OUT = Path("logs/satellite_cb_paper.csv")
CORE = 1_600_000.0
# 2026-10-23: 100万实盘口径的卫星账本(40万股票+60万CB核心 + 5万CB-only卫星)
CORE_100W = 1_000_000.0
SAT_100W = 50_000.0
SIZES = {"sat05": 0.05, "sat10": 0.10, "sat20": 0.20, "sat30": 0.30,
         "sat40": 0.40, "sat50": 0.50, "sat75": 0.75}


def _metrics(ret: pd.Series):
    ret = ret.dropna()
    if len(ret) < 2:
        return {"n": int(len(ret))}
    nav = (1 + ret).cumprod()
    vol = ret.std()
    return {
        "n": int(len(ret)),
        "cum_pct": round(float(nav.iloc[-1] - 1) * 100, 2),
        "dd_pct": round(float((nav / nav.cummax() - 1).min()) * 100, 2),
        "sharpe": round(float(ret.mean() / vol * (252 ** 0.5)), 3) if vol > 0 else None,
    }


def main():
    if not NAV.exists():
        print("缺 NAV parquet, 跳过")
        return 1
    df = pd.read_parquet(NAV)
    if df.empty:
        print("NAV 为空, 跳过")
        return 1
    latest = df.iloc[-1]
    date = str(latest["date"])[:10]
    nav_cb = float(latest["nav_cb"])
    nav_comb = float(latest["nav_combined"])

    if not STATE.exists():
        STATE.write_text(json.dumps({
            "start_date": date,
            "nav_cb_start": nav_cb,
            "nav_combined_start": nav_comb,
            "core_notional": CORE,
            "sizes": SIZES,
            "note": "卫星资金=额外资金; 1x CB; 只读纸面, 不下单",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"卫星纸面初始化: {date} nav_cb={nav_cb:.6f} "
              f"nav_combined={nav_comb:.6f} (三档 10/20/30%)")
        return 0

    st = json.loads(STATE.read_text(encoding="utf-8"))
    mult = nav_cb / float(st["nav_cb_start"])
    row = {"date": date, "nav_cb": round(nav_cb, 6),
           "nav_combined": round(nav_comb, 6),
           "sat_multiple": round(mult, 6)}
    for name, pct in st["sizes"].items():
        S = CORE * float(pct)
        sat_nav = S * mult
        comb = (CORE * nav_comb + sat_nav) / (CORE + S)
        row[f"sat_nav_{name}"] = round(sat_nav, 0)
        row[f"combined_nav_{name}"] = round(comb, 6)
        row[f"sat_ret_{name}"] = round(mult - 1, 6)
    # 100万核心 + 5万卫星(用户 2026-10-23 指定): 核心=100万, 卫星=5万 CB-only 1x
    row["sat_nav_100w5w"] = round(SAT_100W * mult, 0)
    row["combined_nav_100w5w"] = round(
        (CORE_100W * nav_comb + SAT_100W * mult) / (CORE_100W + SAT_100W), 6)
    row["sat_ret_100w5w"] = round(mult - 1, 6)

    hist = pd.read_csv(OUT) if OUT.exists() else pd.DataFrame()
    if not hist.empty and date in set(hist["date"].astype(str)):
        hist = hist[hist["date"].astype(str) != date]
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist = hist.sort_values("date").reset_index(drop=True)
    hist.to_csv(OUT, index=False)

    print(f"卫星纸面 {date}: CB {mult:.4f}x | "
          f"start {st['start_date']}")
    sm = hist["sat_multiple"].astype(float)
    sat_dd = float((sm / sm.cummax() - 1).min() * 100)
    for name in st["sizes"]:
        navs = hist[f"combined_nav_{name}"].astype(float)
        r = navs.pct_change().dropna()
        print(f"  {name}: 合并NAV {row[f'combined_nav_{name}']:.6f} "
              f"{_metrics(r)}")
        print(f"    卫星: {mult:.4f}x 累计{100*(mult-1):+.2f}% "
              f"dd={sat_dd:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
