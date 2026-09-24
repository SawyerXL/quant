"""09-30 调仓预览 (2026-09-24): 模拟调仓日信号的三层过滤走查, 输出换血规模。
口径与 cb_signal 调仓日路径一致: 快照全排名 → 停牌(新浪实时)/日线N=10/
临期N=60 → 甲补足25只 → 每只 600000/25 → 与当前持仓差量(取整10)。
只读预览, 不下单、不写信号。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from loguru import logger

from data.storage import load_meta
from data.cb_select import select_double_low
from execution.trader import _round_cb_lot
from scripts.fetch_and_execute import classify_tier
from scripts.cb_signal import maturity_band

CB_CAPITAL = 600_000
TOP_N = 25
N_DAYS = 10
POS_FILE = Path("logs/qmt_positions_latest.json")


def main():
    snaps = load_meta("cb_snapshots")
    snaps["snap_date"] = pd.to_datetime(snaps["snap_date"])
    latest = snaps["snap_date"].max()
    snap = snaps[snaps["snap_date"] == latest]
    print(f"快照: {latest.date()} ({len(snap)} 条)")
    ranked = select_double_low(snap)   # 全排名(已含评级/规模/强赎/404过滤)

    # 新浪实时停牌 + 日线新鲜度 + 临期(与 cb_signal 同口径)
    import urllib.request
    syms = ",".join(("sh" if str(c).startswith("11") else "sz") + str(c)
                    for c in ranked[:80])
    try:
        req = urllib.request.Request("https://hq.sinajs.cn/list=" + syms,
                                     headers={"Referer": "https://finance.sina.com.cn"})
        txt = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
        suspended = set()
        for line in txt.splitlines():
            if "=" not in line:
                continue
            head, body = line.split("=", 1)
            code = head.rsplit("_", 1)[-1][2:]
            f = body.strip().strip('";').split(",")
            if len(f) >= 9:
                try:
                    prev, vol = float(f[2]), float(f[8])
                except ValueError:
                    continue
                if prev > 0 and vol == 0:
                    suspended.add(code)
    except Exception:
        suspended = set()
    try:
        ew = pd.read_parquet("data_store/meta/cb_expiry_watch.parquet")
        expiry = {}
        for _, r in ew.iterrows():
            c = str(r["代码"])
            if pd.notna(r["最后交易日"]):
                expiry[c] = pd.Timestamp(r["最后交易日"])
            elif pd.notna(r["到期日"]):
                expiry[c] = pd.Timestamp(r["到期日"]) - pd.Timedelta(days=10)
    except Exception:
        expiry = {}
    today = pd.Timestamp.now()
    pos = json.loads(POS_FILE.read_text(encoding="utf-8"))
    positions = {str(k).split(".")[0]: v.get("volume", 0)
                 for k, v in pos.get("positions", {}).items()}
    import akshare as ak
    selected, excluded, buffer = [], {}, set()
    for c in ranked[:80]:
        if len(selected) >= TOP_N:
            break
        if c in suspended:
            excluded[c] = "停牌"
            continue
        if c in expiry:
            days_left = (expiry[c] - today).days
            band = maturity_band(days_left, positions.get(c, 0) > 0)
            if band == "exclude":
                excluded[c] = f"临期({expiry[c].date()}, 剩{days_left}天)"
                continue
            if band == "hold_no_buy":
                buffer.add(c)   # 可持有不加仓
        try:
            sym = ("sh" if str(c).startswith(("11",)) else "sz") + str(c)
            d = ak.bond_zh_hs_cov_daily(symbol=sym)
            if d is None or d.empty:
                excluded[c] = "无日线"
                continue
            last = pd.Timestamp(d.sort_values("date").iloc[-1]["date"])
            px = float(d.sort_values("date").iloc[-1]["close"])
            time.sleep(0.6)
        except Exception:
            excluded[c] = "拉取失败"
            continue
        if len(pd.bdate_range(last, today)) - 1 > N_DAYS * 1.5:
            excluded[c] = f"日线停更({last.date()})"
            continue
        selected.append((c, px))
    print(f"调仓日预期选入: {len(selected)} 只, 排除 {len(excluded)} 只")
    print("排除:", excluded)

    budget = CB_CAPITAL / len(selected)
    targets = {}
    prices = {}
    for c, px in selected:
        if c in buffer:
            targets[c] = positions.get(c, 0)   # 缓冲带: 目标钳到当前持仓
        else:
            lots = int(budget / px / 10)
            targets[c] = lots * 10
        prices[c] = round(px, 2)

    sells, buys = [], []
    for c, tgt in targets.items():
        cur = positions.get(c, 0)
        if cur <= 0:
            continue
        if c not in targets:
            continue
    # 差量
    for c in sorted(set(positions) | set(targets)):
        cur = positions.get(c, 0)
        tgt = targets.get(c, 0)
        if c[:3] not in ("110", "111", "113", "118", "123", "127", "128"):
            continue
        if cur > 0 and tgt == 0:
            sells.append((c, cur, round(cur * (prices.get(c) or 100), 0)))
        elif tgt > 0:
            d = _round_cb_lot(tgt - cur)
            if d > 0:
                buys.append((c, cur, tgt, d, round(d * prices.get(c, 1), 0)))
    print(f"\n预期卖出 {len(sells)} 笔: {[(s[0], s[1]) for s in sells]}")
    print(f"预期买入 {len(buys)} 笔:")
    for b in buys:
        print(f"  {b[0]}: {b[1]}→{b[2]} (+{b[3]}, 约{b[4]:,.0f}元)")
    plan = {
        "sells": [{"code": s[0], "direction": "sell", "shares": s[1],
                   "est_amount": s[2]} for s in sells],
        "buys": [{"code": b[0], "direction": "buy", "shares": b[3],
                  "est_amount": b[4]} for b in buys],
    }
    total = sum(e["est_amount"] for e in plan["sells"] + plan["buys"])
    sig = {"buy": [b[0] for b in buys],
           "sell": [s[0] for s in sells], "interim_deployment": False}
    tier, reasons = classify_tier(plan, sig)
    tier_tag = {1: "档1免确认", 2: "档2清单确认", 3: "档3逐笔人工"}[tier]
    print(f"\n总交易额预估: {total:,.0f} 元 | 档位: {tier_tag} {reasons}")


if __name__ == "__main__":
    main()
