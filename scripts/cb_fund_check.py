"""10-09 换血资金充足性检查 (2026-10-01 建, run-book"只算不改"步骤工具化)。

当日实时价(akshare, 与信号同源)重算 24 笔总额: 卖出所得 + 现有现金 ≥ 买入支出?
只输出检查结果, 不改清单不改张数。缺口 >5% → 停止判据④(分批+按 dblow 倒序
砍尾部买入笔)。现价获取失败 → 回退本地日线并标注。
用法: python scripts/cb_fund_check.py [--plan logs/trade_plan_20260930_cb.json]
"""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

GAP_STOP = 0.05  # 停止判据④: 缺口>5%


def _live_price(code):
    """akshare 当日最新价(与 cb_signal 定价同源); 失败回退本地日线, 返回 (px, src)。"""
    try:
        import akshare as ak
        sym = ("sh" if str(code).startswith("11") else "sz") + str(code)
        d = ak.bond_zh_hs_cov_daily(symbol=sym)
        if d is not None and len(d) > 0:
            d = d.sort_values("date")
            return float(d.iloc[-1]["close"]), "akshare"
    except Exception:
        pass
    try:
        local = Path("data_store/convertible_bonds/daily") / f"{code}.parquet"
        ld = pd.read_parquet(local)
        dc = "date" if "date" in ld.columns else ld.columns[0]
        return float(ld.sort_values(dc).iloc[-1]["close"]), "本地T-1"
    except Exception:
        return None, "无价"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="logs/trade_plan_20260930_cb.json")
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    sell_amt = buy_amt = 0.0
    print(f"资金检查(只算不改) | 实时价重算:")
    for s in plan["sells"]:
        px, src = _live_price(s.get("code"))
        if px:
            sell_amt += s.get("shares", 0) * px
            print(f"  卖 {s['code']} {s['shares']}张 @ {px:.2f}({src})")
        else:
            print(f"  卖 {s['code']}: 无价, 用计划价 {s.get('est_amount',0):,.0f}")
            sell_amt += s.get("est_amount", 0)
    for b in plan["buys"]:
        px, src = _live_price(b.get("code"))
        if px:
            buy_amt += b.get("shares", 0) * px
        else:
            buy_amt += b.get("est_amount", 0)
    # 现有现金: 从最新持仓快照的 cash 字段(仿真户总现金含全部 notional, 按需核对)
    cash = 0.0
    try:
        import glob as _g
        snaps = sorted(_g.glob("logs/*positions_2026*.json"))
        if snaps:
            d = json.loads(Path(snaps[-1]).read_text(encoding="utf-8"))
            cash = float(d.get("account", {}).get("cash", 0))
    except Exception:
        pass
    gap = buy_amt - sell_amt - cash
    tag = "缺口" if gap > 0 else "盈余"
    print(f"\n卖出所得 {sell_amt:,.0f} + 现有现金 {cash:,.0f} "
          f"vs 买入支出 {buy_amt:,.0f} → {tag} {abs(gap):,.0f} ({gap/max(buy_amt,1)*100:+.1f}%)")
    print("注: 仿真户 cash 为种子资金(4888万污染值), 本检查在仿真下恒过——"
          "真实账户上线后此检查才具约束力; 净买入需求(卖出所得 vs 买入支出)"
          f"= {buy_amt - sell_amt:,.0f} 元, 该数在真盘是硬约束。")
    if gap > buy_amt * GAP_STOP:
        print("⚠ 停止判据④触发: 缺口>5% → 分批执行+按 dblow 倒序砍尾部买入笔(整笔砍, 不缩张数)")
    else:
        print("✓ 资金充足(缺口在 5% 内), 按序执行")


if __name__ == "__main__":
    main()
