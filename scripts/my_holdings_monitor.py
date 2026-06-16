"""
个人持仓监控 — 只管卖出/持有，绝不推荐买入。

约束：
  本工具不生成买入信号，不推荐新标的。
  输出为决策建议，执行由人工在券商完成。
  monitor=False 的持仓只显示不告警。

用法: python scripts/my_holdings_monitor.py
Cron: 0 16 * * 1-5
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import date, datetime
from loguru import logger
from data.storage import load_daily

HOLDINGS_FILE = Path("config/my_holdings.csv")
LOG_FILE = Path("logs/holdings_monitor.log")
MA_WINDOW = 10; EXIT_DAYS = 3
ABSOLUTE_STOP = -0.12; TRAILING_STOP = -0.18
TAKE_FULL = 0.25; TAKE_HALF = 0.15
WARN_PCT = 0.03   # 接近线3%预警

# ── 核心分析 ───────────────────────────────────────────
def analyze(code: str, name: str, cost_price: float, shares: int,
            buy_date: str, is_etf: bool = False) -> dict:
    """
    返回持仓诊断字典。
    本工具不生成买入信号——returns dict with 'action' in [sell, reduce, warn, hold, locked]
    """
    today = date.today().strftime("%Y-%m-%d")
    df = load_daily(code, (date.today() - pd.Timedelta(days=90)).strftime("%Y-%m-%d"), today)
    if df.empty or len(df) < 10:
        return {"code": code, "name": name, "action": "nodata", "cur": None,
                "pnl_pct": None, "reason": "数据不足"}

    df = df.sort_values("date"); closes = df["close"].values
    cur = closes[-1]; ma10 = closes[-MA_WINDOW:].mean()
    pnl = (cur / cost_price - 1) if cost_price > 0 else 0
    high_since_buy = max(closes)  # 持有期内最高价

    # MA10 状态
    below = 0
    for c in reversed(closes):
        if c < ma10: below += 1
        else: break

    # 追踪止损：从持有期最高点回撤
    trail_dd = (cur / high_since_buy - 1) if high_since_buy > 0 else 0

    # T+1 锁定
    locked = False
    if buy_date:
        try:
            bd = datetime.strptime(buy_date, "%Y-%m-%d").date()
            if (date.today() - bd).days < 1:
                locked = True
        except Exception: pass

    result = {"code": code, "name": name, "cur": round(cur, 2),
              "ma10": round(ma10, 2), "pnl_pct": round(pnl * 100, 1),
              "below_ma": below, "trail_dd": round(trail_dd * 100, 1),
              "high": round(high_since_buy, 2), "cost": cost_price,
              "shares": shares, "mktval": round(cur * shares, 0), "etf": is_etf}

    if locked:
        result["action"] = "locked"; result["reason"] = "T+1锁定，今日不可卖"
        return result

    if below >= EXIT_DAYS:
        result["action"] = "sell"; result["reason"] = f"MA10连续{below}日跌破({cur:.2f}<{ma10:.2f})"
    elif pnl <= ABSOLUTE_STOP:
        result["action"] = "sell"; result["reason"] = f"绝对止损{pnl:.1%}(<-12%)"
    elif trail_dd <= TRAILING_STOP:
        result["action"] = "sell"; result["reason"] = f"追踪止损{trail_dd:.1%}(<-18%，从高{high_since_buy:.2f})"
    elif pnl >= TAKE_FULL and not is_etf:
        result["action"] = "sell"; result["reason"] = f"止盈{pnl:.1%}(>+25%)"
    elif pnl >= TAKE_HALF and not is_etf:
        # 检查MA10拐头
        if len(closes) >= 12:
            ma_prev = closes[-12:-2].mean()
            if ma10 < ma_prev:
                result["action"] = "reduce"; result["reason"] = f"止盈减半{pnl:.1%}(>+15%)+MA10拐头"
            else:
                result["action"] = "hold"; result["reason"] = "浮盈保持"
        else:
            result["action"] = "hold"; result["reason"] = "浮盈保持"
    elif pnl <= ABSOLUTE_STOP + WARN_PCT:
        result["action"] = "warn"; result["reason"] = f"接近绝对止损(距{abs(ABSOLUTE_STOP):.0%}仅{abs(pnl-ABSOLUTE_STOP):.1%})"
    elif trail_dd <= TRAILING_STOP + WARN_PCT:
        result["action"] = "warn"; result["reason"] = f"接近追踪止损(距{abs(TRAILING_STOP):.0%}仅{abs(trail_dd-TRAILING_STOP):.1%})"
    elif below == 2:
        result["action"] = "warn"; result["reason"] = f"接近MA10止损(已连续{below}日)"
    else:
        result["action"] = "hold"; result["reason"] = "正常持有"

    return result


# ── 主流程 ───────────────────────────────────────────
def main():
    if not HOLDINGS_FILE.exists():
        logger.error(f"持仓文件不存在: {HOLDINGS_FILE}")
        return

    df_h = pd.read_csv(HOLDINGS_FILE, dtype={"code": str})
    df_h["code"] = df_h["code"].str.zfill(6)
    results = []; alerts = []; today = date.today().strftime("%Y-%m-%d")

    for _, r in df_h.iterrows():
        code, name, monitor = r["code"], r["name"], r.get("monitor", True)
        if not monitor:
            results.append({"code": code, "name": name, "action": "skip", "reason": "monitor=False"})
            continue

        cp = float(r["cost_price"]) if pd.notna(r["cost_price"]) else 0
        sh = int(r["shares"]) if pd.notna(r["shares"]) else 0
        bd = str(r["buy_date"]) if pd.notna(r.get("buy_date")) else ""
        etf = code.startswith("51") or code.startswith("15")
        res = analyze(code, name, cp, sh, bd, etf)
        results.append(res)

    # ── 输出 ─────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  个人持仓监控  {today}")
    print(f"  规则: 绝对-12% | 追踪-18% | MA10三日 | 止盈25%/减半15% | T+1")
    print(f"{'='*65}")
    print(f"\n  {'代码':<8} {'名称':<8} {'现价':>8} {'盈亏':>7} {'MA10':>8} {'动作':<12} {'原因'}")
    print(f"  {'-'*65}")

    for r in results:
        act = r.get("action", "?"); cur = r.get("cur", 0) or 0; pnl = r.get("pnl_pct", 0) or 0
        tag = {"sell": "🔴 清仓", "reduce": "🟡 减半", "warn": "🔔 预警", "hold": "✅ 持有",
               "locked": "🔒 锁定", "skip": "⏭️ 跳过", "nodata": "❓"}.get(act, act)
        print(f"  {r.get('code',''):<8} {r.get('name',''):<8} {cur:>8.2f} {pnl:>+6.1f}% "
              f"{r.get('ma10',0):>8.2f} {tag:<12} {r.get('reason','')}")
        if act in ("sell", "reduce", "warn"):
            alerts.append(f"{tag} {r['code']} {r['name']} ({r.get('reason','')})")

    # ── 汇总 ─────────────────────────────────────────
    print(f"\n  {'─'*65}")
    total_mv = sum(r.get("mktval", 0) or 0 for r in results)
    total_pnl = sum((r.get("pnl_pct", 0) or 0) / 100 * (35000) for r in results)  # approximated
    print(f"  持仓市值: ¥{total_mv:,.0f}")
    if alerts:
        print(f"  ⚠️  需操作: {len(alerts)} 项")
        for a in alerts: print(f"    {a}")
        try:
            from monitoring.alerts import send_alert
            send_alert("【个人持仓监控】" + " | ".join(alerts))
        except Exception as e:
            logger.warning(f"告警失败: {e}")
    else:
        print(f"  ✅ 全部正常持有")

    # 归档
    LOG_FILE.parent.mkdir(exist_ok=True)
    import json
    LOG_FILE.write_text(json.dumps({"date": today, "results": results}, ensure_ascii=False, indent=2))
    print(f"\n  日志: {LOG_FILE}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
