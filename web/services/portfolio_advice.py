"""Portfolio operation advice — per-stock action suggestions during trading hours."""
import sys, os
from pathlib import Path
from datetime import datetime, date, time

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))


def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5: return False
    t = now.time()
    return time(9, 0) <= t <= time(15, 0)


def get_portfolio_advice(user_id: str = "", is_admin: bool = False) -> dict:
    """Generate per-stock operation advice."""
    from web.services.portfolio_service import _get_user_positions
    from scripts.morning_10dim_report import load_technical, fetch_rt_prices

    positions = _get_user_positions(user_id, is_admin)
    if not positions:
        return {"is_trading": _is_trading_time(), "advice": [], "note": "暂无持仓"}

    codes = [p["code"] for p in positions]
    rt_prices = fetch_rt_prices(codes)
    today_str = str(date.today())

    advice_list = []
    alerts_high = []  # 🔴 urgent
    alerts_mid = []   # 🟡 attention

    for pos in positions:
        code = pos["code"]
        name = pos.get("name", "")
        cost = float(pos["cost"])
        shares = int(pos["shares"])

        p = rt_prices.get(code, {})
        cur = p.get("cur", cost) if p else cost
        pnl_pct = (cur / cost - 1) * 100 if cost > 0 else 0
        chg_today = p.get("chg", 0)

        tech = load_technical(code, today_str) if cur > 0 else None
        ma10 = tech.get("ma10") if tech else None
        near_ma10 = ma10 and abs(cur - ma10) / ma10 < 0.02

        # Determine action
        action = "持有"
        action_color = ""
        reasons = []

        # Take profit checks
        if pnl_pct >= 60:
            action = "止盈"; action_color = "badge-green"
            reasons.append(f"浮盈{pnl_pct:.0f}%，触发TP2(+60%)，卖1/3")
            alerts_high.append(f"{code} {name}: TP2触发 {pnl_pct:.0f}%")
        elif pnl_pct >= 30:
            action = "止盈"; action_color = "badge-green"
            reasons.append(f"浮盈{pnl_pct:.0f}%，触发TP1(+30%)，卖1/3")
            alerts_high.append(f"{code} {name}: TP1触发 {pnl_pct:.0f}%")

        # Stop loss
        if pnl_pct <= -12:
            action = "止损"; action_color = "badge-bear"
            reasons.append(f"亏损{pnl_pct:.0f}%，触发-12%止损")
            alerts_high.append(f"{code} {name}: 止损触发 {pnl_pct:.0f}%")

        # MA10 exit
        if tech and ma10 and cur < ma10:
            if tech.get("cons_down", 0) >= 3:
                if action == "持有":
                    action = "关注"; action_color = "badge-warning"
                reasons.append(f"MA10下，连跌{tech['cons_down']}天")
                alerts_mid.append(f"{code} {name}: MA10下方 连跌{tech['cons_down']}天")

        # Near support
        if near_ma10 and pnl_pct > -8 and action == "持有":
            reasons.append("MA10附近，可关注")

        # Overbought
        if tech and tech.get("ret20", 0) > 50:
            if action == "持有":
                action = "关注"; action_color = "badge-warning"
            reasons.append(f"20日涨{tech['ret20']:.0f}%，过热")

        # Volume signal
        if tech and tech.get("vr", 1) > 2:
            reasons.append(f"放量{tech['vr']:.0f}倍")

        advice_list.append({
            "code": code, "name": name, "shares": shares,
            "cost": round(cost, 2), "price": round(cur, 2),
            "pnl_pct": round(pnl_pct, 1), "chg_today": round(chg_today, 2),
            "ma10": round(ma10, 2) if ma10 else None,
            "action": action, "action_color": action_color,
            "reasons": reasons,
        })

    # Sort: alerts first, then by P&L
    advice_list.sort(key=lambda x: (0 if x["action"] != "持有" else 1, -abs(x["pnl_pct"])))

    return {
        "is_trading": _is_trading_time(),
        "advice": advice_list,
        "alerts_high": alerts_high,
        "alerts_mid": alerts_mid,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }
