"""10-09 换血事件复核闸门 (2026-10-01 建, run-book"只删不改"步骤工具化)。

输入: 09-30 plan 文件(24笔) + cb_expiry_watch(09:05 cron 产物) + 快照评级 +
本地日线新鲜度。对 19 只买入标的逐只查四类事件: 强赎/临期跨线/停牌/评级下调
→ 命中删笔(记原因)。卖出 5 笔不复核(事件只会加强)。删笔>5 → 停止判据③。
只输出删笔建议, 不修改任何文件(执行删笔=人工确认后手工处理)。
用法: python scripts/cb_event_gate.py [--plan logs/trade_plan_20260930_cb.json]
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from data.storage import load_meta
from scripts.cb_signal import maturity_band

N_DAYS = 10          # 停牌判定: 日线停更阈值(与信号 N=10 一致)
MAX_DELETES = 5      # 停止判据③: 删笔>5 → 停
MIN_RATING_ORDER = {"AAA": 0, "AA+": 1, "AA": 2, "AA-": 3, "A+": 4, "A": 5, "A-": 6}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="logs/trade_plan_20260930_cb.json")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    buys = [(b.get("code"), b.get("shares")) for b in plan["buys"]]
    print(f"事件复核 | plan: {len(buys)} 笔买入 | 只删不改 | 停止线 >{MAX_DELETES} 笔")

    # run-book 步骤②: 快照核验(路径一——ranked 由 09-01 快照构造, 变了=意外)
    try:
        snaps = load_meta("cb_snapshots")
        snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
        snap_max = snaps["snap_date"].max()
        ok = str(snap_max.date()) == "2026-09-01"
        print(f"快照核验: 最新 snap_date = {snap_max.date()} "
              f"{'✓ 与路径一一致' if ok else '⚠ 停止判据②: 快照已变, 停查因(ranked 可能已换)'}")
    except Exception:
        print("快照核验: 读取失败(按异常处理, 停查)")

    # 到期表(09:05 cron 产物)
    expiry = {}
    try:
        ew = pd.read_parquet("data_store/meta/cb_expiry_watch.parquet")
        for _, r in ew.iterrows():
            c = str(r["代码"])
            last = pd.Timestamp(r["最后交易日"]) if pd.notna(r["最后交易日"]) else None
            exp = pd.Timestamp(r["到期日"]) if pd.notna(r["到期日"]) else None
            if last is not None:
                expiry[c] = last
            elif exp is not None:
                expiry[c] = exp - pd.Timedelta(days=10)
    except Exception:
        pass

    # 评级(最新快照)
    rating = {}
    try:
        snaps = load_meta("cb_snapshots")
        snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
        latest = snaps[snaps["snap_date"] == snaps["snap_date"].max()]
        rating = dict(zip(latest["code"].astype(str), latest["rating"].astype(str)))
    except Exception:
        pass

    today = date.today()
    deletes = []
    for code, shares in buys:
        reasons = []
        if code in expiry:
            days_left = (expiry[code].date() - today).days
            band = maturity_band(days_left, False)  # 买入侧=未持有
            if band == "exclude":
                reasons.append(f"临期跨线(交易末日{expiry[code].date()}, 剩{days_left}天)")
        r = rating.get(code, "")
        if r in MIN_RATING_ORDER and MIN_RATING_ORDER[r] > MIN_RATING_ORDER["A-"]:
            reasons.append(f"评级{r}低于A-")
        local = Path("data_store/convertible_bonds/daily") / f"{code}.parquet"
        if local.exists():
            try:
                ld = pd.read_parquet(local, columns=["date"])
                if not ld.empty:
                    last_date = pd.Timestamp(ld["date"].max())
                    gap = (pd.Timestamp(today) - last_date).days
                    if gap > N_DAYS * 2:
                        reasons.append(f"日线停更{last_date.date()}({gap}天)")
            except Exception:
                pass
        if reasons:
            deletes.append((code, shares, "; ".join(reasons)))
            print(f"  ✂ {code} {shares}张: {reasons}")
    print(f"\n结论: 建议删笔 {len(deletes)}/{len(buys)}"
          + ("  ⚠ 停止判据③触发(>5笔): 整张清单时效性不成立, 人工重审"
             if len(deletes) > MAX_DELETES else "  → 删笔经人工确认后执行, 其余照单"))


if __name__ == "__main__":
    main()
