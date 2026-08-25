"""Portfolio service — MA10 triggers, stop-loss, take-profit tracking."""
import sys, os, json
from pathlib import Path
from datetime import date

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from config.settings import ROOT
from web.snapshots import read_snapshot, write_snapshot


def get_ma10_triggers() -> dict:
    """Get MA10-4d trigger log with stats. Cached snapshot."""
    snap = read_snapshot("ma10_triggers")
    if snap:
        return snap

    try:
        from scripts.check_ma10_triggers import run as run_ma10
        df = run_ma10(send=False)
        if df.empty:
            return {"triggers": [], "stats": {}}

        triggers = df.fillna("").to_dict(orient="records")
        # Clean up for JSON
        for t in triggers:
            for k, v in t.items():
                if hasattr(v, "item"):  # numpy types
                    t[k] = v.item()
                elif isinstance(v, float) and str(v) == "nan":
                    t[k] = None

        # Stats
        judged = [t for t in triggers if t.get("verdict") and t["verdict"] != "待判定"]
        effective = [t for t in judged if t.get("verdict") == "有效"]
        stats = {
            "total": len(triggers),
            "judged": len(judged),
            "effective": len(effective),
            "effective_rate": round(len(effective) / len(judged) * 100, 1) if judged else 0,
        }

        payload = {"triggers": triggers, "stats": stats}
        write_snapshot("ma10_triggers", payload, source="check_ma10_triggers.run")
        return read_snapshot("ma10_triggers")
    except Exception as e:
        return {"triggers": [], "stats": {}, "error": str(e)}


def _get_user_positions(user_id: str, is_admin: bool) -> list[dict]:
    """Get positions for a user. Admin sees CSV; others see their DB record."""
    if is_admin:
        # Admin: read from CSV (personal holdings)
        import pandas as pd
        hf = ROOT / "config" / "my_holdings.csv"
        if not hf.exists():
            return []
        df = pd.read_csv(hf, dtype={"code": str})
        return [{"code": str(r["code"]).zfill(6), "name": str(r.get("name", "")),
                 "cost": float(r["cost_price"]), "shares": int(r["shares"])}
                for _, r in df.iterrows()]
    else:
        # Other users: read from DB
        from web.db import SessionLocal
        from web.models import UserHoldings
        db = SessionLocal()
        try:
            uh = db.query(UserHoldings).filter(UserHoldings.user_id == user_id).first()
            if uh:
                return json.loads(uh.data_json)
            return []
        finally:
            db.close()


def save_user_positions(user_id: str, positions: list[dict]) -> None:
    """Save positions for a user."""
    from web.db import SessionLocal
    from web.models import UserHoldings
    db = SessionLocal()
    try:
        uh = db.query(UserHoldings).filter(UserHoldings.user_id == user_id).first()
        if uh:
            uh.data_json = json.dumps(positions)
            uh.updated_at = __import__('datetime').datetime.utcnow()
        else:
            uh = UserHoldings(user_id=user_id, data_json=json.dumps(positions))
            db.add(uh)
        db.commit()
    finally:
        db.close()


def get_exit_status(user_id: str = "", is_admin: bool = False) -> dict:
    """Compute exit status for a user's holdings."""
    import pandas as pd
    from scripts.morning_10dim_report import load_technical, fetch_rt_prices

    positions = _get_user_positions(user_id, is_admin)
    if not positions:
        return {"holdings": [], "summary": {"count": 0, "total_market_value": 0, "total_pnl": 0, "triggers_active": 0},
                "note": "暂无持仓。请在下方添加。"}

    codes = [p["code"] for p in positions]
    rt_prices = fetch_rt_prices(codes)
    today_str = str(date.today())
    holdings = []

    for pos in positions:
        code = pos["code"]
        cost = float(pos["cost"])
        shares = int(pos["shares"])
        name = pos.get("name", "")

        p = rt_prices.get(code, {})
        cur = p.get("cur", 0) if p else 0
        if cur <= 0:
            # Pre-market/after-hours: Sina returns 0, fall back to prev_close or cost
            cur = p.get("prev", 0) if p else 0
        if cur <= 0:
            cur = cost
        pnl_pct = (cur / cost - 1) * 100 if cost > 0 else 0
        mkt_val = cur * shares

        tech = load_technical(code, today_str) if cur > 0 else None
        days_below = 0
        ma10_val = None
        if tech and cur > 0:
            ma10_val = tech.get("ma10")
            if ma10_val and cur < ma10_val:
                try:
                    from data.storage import load_daily
                    import pandas as pd
                    df = load_daily(code, '2026-06-01', today_str)
                    if not df.empty:
                        df['date'] = pd.to_datetime(df['date'])
                        df = df.set_index('date').sort_index()
                        cl = pd.to_numeric(df['close'], errors='coerce').dropna()
                        if len(cl) >= 11:
                            ma10_series = cl.rolling(10).mean()
                            for j in range(len(cl)-1, -1, -1):
                                if pd.notna(ma10_series.iloc[j]) and cl.iloc[j] < ma10_series.iloc[j]:
                                    days_below += 1
                                else: break
                except Exception:
                    days_below = 1

        triggers = []
        if days_below >= 4:
            triggers.append("🔴 MA10-4d触发")
        elif days_below >= 2:
            triggers.append(f"🟡 MA10下{days_below}d")
        if pnl_pct <= -12:
            triggers.append("🔴 止损-12%")
        if pnl_pct >= 30:
            triggers.append(f"🟢 TP1: +30%卖1/3")
        if pnl_pct >= 60:
            triggers.append(f"🟢 TP2: +60%再卖1/3")

        holdings.append({
            "code": code, "name": name, "shares": shares,
            "cost": round(cost, 2), "price": round(cur, 2),
            "prev_close": round(p.get("prev", 0), 2) if p else None,
            "pnl_pct": round(pnl_pct, 1), "mkt_val": round(mkt_val, 0),
            "ma10": round(ma10_val, 2) if ma10_val else None,
            "days_below_ma10": days_below,
            "triggers": triggers,
        })

    total_mkt = sum(h["mkt_val"] for h in holdings)
    total_pl = sum((h["price"] - h["cost"]) * h["shares"] for h in holdings)

    return {
        "holdings": sorted(holdings, key=lambda x: x["pnl_pct"], reverse=True),
        "summary": {
            "count": len(holdings),
            "total_market_value": round(total_mkt, 0),
            "total_pnl": round(total_pl, 0),
            "triggers_active": len([h for h in holdings if h["triggers"]]),
        }
    }
