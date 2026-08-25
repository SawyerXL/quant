"""
做T信号结算 —— 把 t_signal_log.csv 的"信号"变成可算胜率的"结果"。

为什么需要：验证期(2026-08-24起一个月)的达标线是「胜率≥90%、单次≥+0.15%」，
而信号日志只记了 date/time/code/direction/action，没有成交与收益。
不结算的话，一个月后是200条信号和0条可算胜率的数据。

判定方式：新浪5分钟线，只看信号时间之后的bar，按顺序判两腿：
  正T: 先挂卖leg1(high≥leg1成交) → 再挂买leg2(low≤leg2接回)
  反T: 先挂买leg1(low≤leg1成交)  → 再挂卖leg2(high≥leg2卖出)
leg2 当日未成交 → 按铁律第4条"次日开盘必了结"，用次日开盘价强制平仓。
leg1 都没成交 → 这单根本没发生，不计入胜率（但单独统计，用来看信号质量）。

成本：个股0.20%(印花税0.05+双边佣金0.05+滑点0.10)，ETF免印花税按0.15%。
用法: python scripts/settle_t_signals.py [--rebuild]
"""
import sys
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from datetime import date
from loguru import logger

SIGNAL_LOG = Path("config/t_signal_log.csv")
SETTLE_LOG = Path("config/t_settle_log.csv")
COST_STOCK, COST_ETF = 0.20, 0.15
POSITION_FRAC = 1 / 3          # 铁律1: 只用1/3仓位


def _sina_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _cost(code: str) -> float:
    return COST_ETF if code.startswith(("5", "1")) and not code.startswith("6") else COST_STOCK


def _legs(row):
    """优先用结构化列；老记录从 action 文本里抠价格。"""
    l1, l2 = row.get("leg1_price"), row.get("leg2_price")
    if pd.notna(l1) and pd.notna(l2):
        return float(l1), float(l2)
    # 必须锚定"挂买/挂卖"再取数，否则 "买1/3" 里的 1 会被当成第二腿价格
    nums = re.findall(r"挂[买卖]\s*(\d+\.?\d*)", str(row.get("action", "")))
    return (float(nums[0]), float(nums[1])) if len(nums) >= 2 else (None, None)


_MIN_CACHE = {}


def _minutes(code: str) -> pd.DataFrame:
    """新浪5分钟线。稳态下每天只结算几只不会限流；批量重算时会被拒，交给日线兜底。"""
    if code in _MIN_CACHE:
        return _MIN_CACHE[code]
    import time
    import akshare as ak
    df = pd.DataFrame()
    for attempt in range(3):
        try:
            raw = ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="5", adjust="")
            raw["day"] = pd.to_datetime(raw["day"])
            for c in ("open", "high", "low", "close"):
                raw[c] = pd.to_numeric(raw[c], errors="coerce")
            df = raw.dropna(subset=["high", "low"]).sort_values("day")
            break
        except Exception as e:
            if attempt == 2:
                logger.debug(f"  {code} 分钟线不可用({e})，转日线近似")
            time.sleep(2 * (attempt + 1))
    _MIN_CACHE[code] = df
    return df


def _daily(code: str) -> pd.DataFrame:
    """兜底：本地日线。只能判'价格够到没'，判不了两腿先后顺序。"""
    from data.storage import load_daily
    try:
        d = load_daily(code, "2026-06-01", str(date.today()))
        if d.empty:
            return pd.DataFrame()
        d = d.copy()
        d["d"] = pd.to_datetime(d["date"]).astype(str).str[:10]
        for c in ("open", "high", "low", "close"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return d.dropna(subset=["high", "low"]).sort_values("d")
    except Exception:
        return pd.DataFrame()


def _settle_daily(row, out, l1, l2, code, d, direction) -> dict:
    """日线近似：够到即算成交，但无法证实 leg2 发生在 leg1 之后 —— 标注出来别当精确值用。"""
    dd = _daily(code)
    if dd.empty:
        out["status"] = "无可用行情"
        return out
    today_row = dd[dd["d"] == d]
    if today_row.empty:
        out["status"] = "日线未覆盖该日"
        return out
    bar = today_row.iloc[0]
    out["method"] = "日线近似"

    filled1 = bar["high"] >= l1 if direction == "正T" else bar["low"] <= l1
    if not filled1:
        out["leg1_filled"] = "否"
        out["status"] = "首腿未成交(信号未兑现)"
        return out
    out["leg1_filled"] = "是"

    filled2 = bar["low"] <= l2 if direction == "正T" else bar["high"] >= l2
    if filled2:
        out["leg2_filled"], exit_px, kind = "是", l2, "当日了结"
        out["seq_verified"] = "否(日线判不了先后)"
    else:
        nxt = dd[dd["d"] > d]
        if nxt.empty:
            out["leg2_filled"] = "否"
            out["status"] = "待次日了结(次日数据未出)"
            return out
        out["leg2_filled"], exit_px, kind = "否", float(nxt.iloc[0]["open"]), "次日开盘强平"
    return _finish(out, direction, l1, exit_px, kind, code)


def _finish(out, direction, l1, exit_px, kind, code) -> dict:
    if direction == "正T":
        sell_px, buy_px = l1, exit_px
    else:
        buy_px, sell_px = l1, exit_px
    net = (sell_px - buy_px) / buy_px * 100 - _cost(code)
    out.update({"exit_price": round(exit_px, 3), "exit_kind": kind,
                "pnl_pct": round(net, 4),
                "pnl_on_position": round(net * POSITION_FRAC, 4),
                "status": "已结算"})
    return out


def settle_one(row) -> dict:
    code, d, t = str(row["code"]).zfill(6), str(row["date"]), str(row["time"])
    direction = row["direction"]
    l1, l2 = _legs(row)
    out = {"date": d, "time": t, "code": code, "name": row.get("name", ""),
           "direction": direction, "leg1_price": l1, "leg2_price": l2,
           "leg1_filled": "", "leg1_time": "", "leg2_filled": "", "leg2_time": "",
           "exit_price": "", "exit_kind": "", "pnl_pct": "", "pnl_on_position": "",
           "method": "分钟精确", "seq_verified": "是", "status": ""}
    if l1 is None:
        out["status"] = "价格缺失"
        return out

    m = _minutes(code)
    day = m[m["day"].astype(str).str[:10] == d] if not m.empty else pd.DataFrame()
    if day.empty:
        return _settle_daily(row, out, l1, l2, code, d, direction)

    after = day[day["day"].astype(str).str[11:19] >= t]
    if after.empty:
        out["status"] = "信号在收盘后"
        return out

    # ── leg1 ──
    hit1 = after[after["high"] >= l1] if direction == "正T" else after[after["low"] <= l1]
    if hit1.empty:
        out["leg1_filled"] = "否"
        out["status"] = "首腿未成交(信号未兑现)"
        return out
    t1 = hit1.iloc[0]["day"]
    out["leg1_filled"], out["leg1_time"] = "是", str(t1)[11:19]

    # ── leg2：只在 leg1 成交之后找 ──
    rest = day[day["day"] > t1]
    hit2 = rest[rest["low"] <= l2] if direction == "正T" else rest[rest["high"] >= l2]

    if not hit2.empty:
        out["leg2_filled"], out["leg2_time"] = "是", str(hit2.iloc[0]["day"])[11:19]
        exit_px, kind = l2, "当日了结"
    else:
        # 铁律4: 次日开盘必了结
        nxt = m[m["day"].astype(str).str[:10] > d]
        if nxt.empty:
            out["leg2_filled"] = "否"
            out["status"] = "待次日了结(次日数据未出)"
            return out
        exit_px, kind = float(nxt.iloc[0]["open"]), "次日开盘强平"
        out["leg2_filled"], out["leg2_time"] = "否", str(nxt.iloc[0]["day"])[:19]

    return _finish(out, direction, l1, exit_px, kind, code)


def main():
    if not SIGNAL_LOG.exists():
        logger.error("无信号日志")
        return
    sig = pd.read_csv(SIGNAL_LOG, dtype={"code": str})
    rebuild = "--rebuild" in sys.argv

    done = set()
    old = pd.DataFrame()
    if SETTLE_LOG.exists() and not rebuild:
        old = pd.read_csv(SETTLE_LOG, dtype={"code": str})
        # 只跳过终态；"待次日了结"下次要重算
        fin = old[old["status"] != "待次日了结(次日数据未出)"]
        done = set(zip(fin["date"], fin["code"], fin["direction"]))

    rows = []
    for _, r in sig.iterrows():
        key = (str(r["date"]), str(r["code"]).zfill(6), r["direction"])
        if key in done:
            continue
        rows.append(settle_one(r))
    if not rows and old.empty:
        logger.info("无待结算信号")
        return

    new = pd.DataFrame(rows)
    allr = pd.concat([old[old["status"] == "待次日了结(次日数据未出)"].iloc[0:0], old, new],
                     ignore_index=True) if not old.empty else new
    allr = allr.drop_duplicates(subset=["date", "code", "direction"], keep="last")
    allr = allr.sort_values(["date", "time"])
    allr.to_csv(SETTLE_LOG, index=False)

    # ── 验证期战报 ──
    s = allr[allr["status"] == "已结算"].copy()
    s["pnl_pct"] = pd.to_numeric(s["pnl_pct"], errors="coerce")
    logger.info(f"结算完成: 共{len(allr)}条信号 → 已结算{len(s)}条")
    print("\n" + "=" * 70)
    print("做T验证期战报 (基准: 胜率≥90%, 单次≥+0.15%)")
    print("=" * 70)
    if s.empty:
        print("  暂无已结算记录")
    else:
        win = (s["pnl_pct"] > 0).mean() * 100
        avg = s["pnl_pct"].mean()
        print(f"  已结算 {len(s)} 笔   胜率 {win:.1f}%  {'✅' if win >= 90 else '❌ 低于90%'}")
        print(f"                     单次均值 {avg:+.3f}%  {'✅' if avg >= 0.15 else '❌ 低于+0.15%'}")
        print(f"  按1/3仓位折算对总仓位贡献: 单次 {avg*POSITION_FRAC:+.3f}%  累计 {s['pnl_on_position'].astype(float).sum():+.3f}%")
        print(f"  当日了结 {(s['exit_kind']=='当日了结').sum()} 笔 | "
              f"次日强平 {(s['exit_kind']=='次日开盘强平').sum()} 笔")
        by = s.groupby("direction")["pnl_pct"].agg(["count", "mean", lambda x: (x > 0).mean() * 100])
        by.columns = ["笔数", "均值%", "胜率%"]
        print("\n" + by.round(3).to_string())
    nf = allr[allr["status"].str.startswith("首腿未成交")]
    if len(nf):
        print(f"\n  首腿未成交(信号发了但价格没够到) {len(nf)}条 —— 不计入胜率，但说明信号偏激进")
    pend = allr[allr["status"].str.startswith("待次日")]
    if len(pend):
        print(f"  待次日了结 {len(pend)}条")
    print()


if __name__ == "__main__":
    main()
