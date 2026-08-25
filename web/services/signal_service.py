"""Signal service — wraps daily_signal_a_v2 output + morning_10dim_report scoring."""
import sys, os, json
from pathlib import Path
from datetime import date

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from config.settings import ROOT, DATA_STORE
from web.snapshots import read_snapshot, write_snapshot

SIGNAL_FILE = DATA_STORE / "meta" / "signal_a_latest.json"
FLOW_CACHE_DIR = ROOT / "logs" / "cache"


def _load_stock_names() -> dict:
    """Load code→name mapping from stock_info_full."""
    try:
        import pandas as pd
        si = pd.read_parquet(DATA_STORE / "meta" / "stock_info_full.parquet")
        return dict(zip(si["code"].astype(str).str.zfill(6), si["name"]))
    except Exception:
        return {}


def get_latest_signal(force: bool = False) -> dict:
    """Read latest daily signal JSON, enriched with stock names. Set force=True to re-read source."""
    if not force:
        snap = read_snapshot("signal")
        if snap:
            return snap
    if SIGNAL_FILE.exists():
        payload = json.loads(SIGNAL_FILE.read_text())
        names = _load_stock_names()
        payload["names"] = {c: names.get(str(c).zfill(6), "") for c in payload.get("holdings", [])}
        write_snapshot("signal", payload, source="signal_a_latest.json")
        return read_snapshot("signal")
    return {"error": "No signal available", "holdings": [], "buy": [], "sell": [], "names": {}}


def _load_flow_data():
    """Load most recent available flow data."""
    # Try today, yesterday, then any recent cache
    from scripts.morning_10dim_report import fetch_fund_flow
    flow_data = fetch_fund_flow()
    if flow_data:
        return flow_data
    # Fallback: scan cache dir for most recent flow file
    if FLOW_CACHE_DIR.exists():
        files = sorted(FLOW_CACHE_DIR.glob("flow_*.json"), reverse=True)
        for f in files:
            if "prev" in f.name:
                continue
            try:
                return json.loads(f.read_text())
            except:
                pass
    return {}


def get_candidates(limit: int = 10) -> list[dict]:
    """Strategy stock selection: CSI800 TOP60 by turnover → 4 conditions → 十维 → ranked."""
    from scripts.morning_10dim_report import score_stock, load_technical, fetch_rt_prices
    from data.storage import load_meta, load_daily
    import pandas as pd
    import numpy as np

    today = date.today().strftime('%Y-%m-%d')

    # ── Build TOP60 from CSI800 turnover (same as CLI) ──
    c800 = load_meta('csi800')
    turnover = {}
    for code in (c800['code'].astype(str).str.zfill(6)):
        try:
            df = load_daily(code, '2026-07-01', today)
            if df.empty: continue
            df['date'] = pd.to_datetime(df['date']); df = df.set_index('date').sort_index()
            amt = pd.to_numeric(df.get('amount', pd.Series(dtype=float)), errors='coerce').dropna()
            if len(amt) >= 20: turnover[code] = amt.iloc[-20:].mean()
        except: pass
    top60 = sorted(turnover.items(), key=lambda x: x[1], reverse=True)[:60]
    top_codes = [c for c, _ in top60]
    price_cache = fetch_rt_prices(top_codes)

    results = []
    for code in top_codes:
        f = {}
        cur_info = price_cache.get(code, {})
        cur = cur_info.get("cur", 0)
        if cur <= 0:
            continue

        name = cur_info.get("name", "")
        tech = load_technical(code, today)
        if not tech:
            continue

        # ═══ 4条件买入筛选 (与CLI scan一致) ═══
        dh = tech.get("dh", 0)
        ma10 = tech.get("ma10", 0)

        # ① MA10附近: -3% ~ +2%
        near_ma10 = cur <= ma10 * 1.02 and cur >= ma10 * 0.97
        # ② RSI < 65 (不过热) — tech may not have rsi field, fallback to ret20
        rsi_val = tech.get("rsi", 50)
        not_overbought = rsi_val < 65
        # ③ 有过回调: dh < -3%
        has_pullback = dh < -3

        # Score with 十维
        rt_info = {"cur": cur, "chg": cur_info.get("chg", 0)}
        pos_info = {"shares": 0, "pnl_pct": 0, "mkt_pct": 0, "eps": 1}
        sig = {}
        score, plus, minus, has_grab = score_stock(code, name, rt_info, tech, f, sig, pos_info)

        # ④ 十维≥0
        ten_ok = score >= 0



        # All 4 conditions
        passed = near_ma10 and not_overbought and has_pullback and ten_ok

        results.append({
            "code": code, "name": name, "price": float(cur),
            "chg_pct": float(cur_info.get("chg", 0)),
            "score": int(score), "plus": [str(p) for p in plus],
            "minus": [str(m) for m in minus],
            "ma10_above": bool(cur > ma10),
            "near_ma10": bool(near_ma10),
            "dh": float(dh), "ret20": float(tech.get("ret20", 0)), "rsi": float(tech.get("rsi", 50)),
            "main_flow": float(f.get("main", 0)),
            "passed": bool(passed),
            "has_grab": bool(has_grab),
        })

    passed_results = [r for r in results if r["passed"]]
    passed_results.sort(key=lambda x: -x["score"])
    return passed_results[:limit] if len(passed_results) > limit else passed_results


def get_stock_analysis(code: str) -> dict:
    """Deep-dive analysis for a single stock."""
    from scripts.enhanced_analyzer import get_technicals, get_financials, get_sector_peers, rt_price, check_stoploss
    from scripts.morning_10dim_report import load_technical
    from datetime import date as dt_date

    result = {"code": code}

    # Realtime price
    p = rt_price(code)
    if not p:
        return {"code": code, "error": "Cannot fetch price"}
    cur = p.get("cur", 0)
    result["price"] = p

    # Enhanced technicals (MA20/60/120, Bollinger, RSI, MACD, ATR, divergence)
    tech = get_technicals(code)
    if tech:
        tech["cur"] = cur  # Override with realtime price
        # Clean numpy types
        for k in list(tech.keys()):
            v = tech[k]
            if hasattr(v, "item"): tech[k] = v.item()
            elif isinstance(v, float) and str(v) == "nan": tech[k] = None
    result["technicals"] = tech

    # Lightweight technicals (MA10, ret5, cons_up/down)
    today = str(dt_date.today())
    lite = load_technical(code, today)
    if lite:
        for k in list(lite.keys()):
            v = lite[k]
            if hasattr(v, "item"): lite[k] = v.item()
    result["lite"] = lite

    # Financials
    fin = get_financials(code, cur)
    if fin:
        for k in list(fin.keys()):
            v = fin[k]
            if hasattr(v, "item"): fin[k] = v.item()
    result["financials"] = fin

    # Sector peers
    peers = get_sector_peers(code)
    result["sector"] = peers

    # Trend verdict
    if tech:
        if cur > tech.get("ma60", cur): trend = "多头"
        elif cur > tech.get("ma20", cur): trend = "震荡"
        elif cur > tech.get("boll_low", cur): trend = "弱势"
        else: trend = "超卖"

        macd_bar = tech.get("macd_bar", 0) or 0
        dif = tech.get("dif", 0) or 0
        dea = tech.get("dea", 0) or 0
        macd_sig = "金叉" if macd_bar > 0 and dif > dea else ("修复" if macd_bar > 0 else "死叉")
        result["verdict"] = {"trend": trend, "macd_signal": macd_sig}

    # Stop-loss check
    if tech:
        stop = check_stoploss(code, p.get("name", ""), tech)
        result["stop_alert"] = stop

    return result
