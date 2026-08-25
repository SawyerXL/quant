"""Market regime service — wraps scripts/market_regime_monitor.py."""
import sys, os, time
from pathlib import Path
from datetime import datetime

_project_root = Path(__file__).parent.parent.parent
os.chdir(_project_root)
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from scripts.market_regime_monitor import run_all
from web.snapshots import read_snapshot, write_snapshot
import threading

_regime_cache = {"ts": 0, "data": None}
_REGIME_TTL = 120  # 2 min

# Cache for intraday index snapshot (1 min TTL)
_intraday_cache = {"ts": 0, "data": None}


def _get_intraday_sh() -> dict | None:
    """Get real-time 上证指数 price from Sina (1 min cache)."""
    now = time.time()
    if _intraday_cache["data"] and (now - _intraday_cache["ts"]) < 60:
        return _intraday_cache["data"]
    try:
        import requests
        resp = requests.get("http://hq.sinajs.cn/list=s_sh000001",
            headers={"Referer": "https://finance.sina.com.cn"}, timeout=3)
        resp.encoding = 'gb2312'
        d = resp.text.split('"')[1].split(',')
        cur = float(d[1]) if d[1] else None
        chg = float(d[3]) if len(d) > 3 and d[3] else None
        result = {"price": round(cur, 2) if cur else None, "chg_pct": round(chg, 2) if chg else None}
        _intraday_cache["ts"] = now
        _intraday_cache["data"] = result
        return result
    except Exception:
        return None


def get_regime_snapshot() -> dict:
    """Read cached regime snapshot. Refreshes if older than 5 min."""
    now = time.time()
    if _regime_cache["data"] and (now - _regime_cache["ts"]) < _REGIME_TTL:
        snap = _regime_cache["data"].copy()
        # Inject intraday price if available
        intra = _get_intraday_sh()
        if intra and intra.get("price"):
            snap["payload"]["intraday_price"] = intra["price"]
            snap["payload"]["intraday_chg"] = intra.get("chg_pct")
        return snap

    snap = read_snapshot("regime")
    if snap:
        _regime_cache["ts"] = now
        _regime_cache["data"] = snap
        intra = _get_intraday_sh()
        if intra and intra.get("price"):
            snap = snap.copy()
            snap["payload"]["intraday_price"] = intra["price"]
            snap["payload"]["intraday_chg"] = intra.get("chg_pct")
        return snap
    return refresh_regime()


def _last_trade_day() -> str:
    """返回最近一个交易日（周末/节假日回退到周五）。"""
    from data.storage import load_meta
    from datetime import date as _date
    cal = load_meta('trade_calendar')
    dates = sorted(cal['trade_date'].astype(str).tolist())
    today = str(_date.today())
    past = [d for d in dates if d <= today]
    return past[-1] if past else today


def refresh_regime() -> dict:
    """Run market regime monitor and save snapshot."""
    import time as _time
    import sys as _sys, io as _io
    old_stdout = _sys.stdout
    _sys.stdout = _io.StringIO()
    try:
        verdict, result = run_all(quiet=True, json_out=False)
    finally:
        _sys.stdout = old_stdout
    payload = {"verdict": verdict, **result}
    payload = _add_transition_analysis(payload)
    # 补上MA200偏离字段(与cron回填格式一致, 防覆盖丢失)
    payload = _add_ma200_dist(payload)
    # 用最近交易日日期写快照（周末不产生假日期条目）
    trade_day = _last_trade_day()
    payload["date"] = trade_day
    from web.snapshots import write_snapshot as _ws
    _ws("regime", payload, source="market_regime_monitor.run_all", snapshot_date=trade_day)
    snap = read_snapshot("regime")
    _regime_cache["ts"] = _time.time()
    _regime_cache["data"] = snap
    return snap


def _add_ma200_dist(payload: dict) -> dict:
    """计算ma200_dist_pct字段, 防止覆盖历史快照时丢字段。"""
    try:
        import pandas as pd
        from data.storage import load_daily
        from datetime import date as _date

        today = str(_date.today())
        dfs = []
        for y in [2024, 2025, 2026]:
            df = load_daily('000001', f'{y}-01-01', f'{y}-12-31')
            if not df.empty: dfs.append(df)
        if not dfs: return payload
        sh = pd.concat(dfs)
        sh['date'] = pd.to_datetime(sh['date'])
        sh = sh.set_index('date').sort_index()
        close = sh['close'].dropna()
        ma200 = close.rolling(200).mean()
        if len(close) == 0: return payload
        cur = float(close.iloc[-1])
        m200 = ma200.iloc[-1]
        if pd.isna(m200): return payload
        payload["ma200_dist_pct"] = round((cur/m200 - 1) * 100, 2)
        payload["sh_close"] = round(cur, 1)
        payload["date"] = today
    except Exception:
        pass
    return payload


def _add_transition_analysis(payload: dict) -> dict:
    """Detect transitional market states that pure rules miss."""
    import re
    sh_close = payload.get("sh_close", 0)
    days_below = payload.get("sh_days_below_ma200", 0)
    verdict = payload.get("verdict", "normal")
    margin_chg = payload.get("margin_chg_5d", 0)
    margin_days = payload.get("margin_cons_days", 0)
    dd_52w = payload.get("sh_dd_52w", 0)

    ma20_val = None; ma200_val = None
    alerts = payload.get("alerts", [])
    for a in alerts:
        msg = str(a.get("msg", ""))
        if "MA20" in msg and "MA60" in msg:
            m = re.search(r'MA20\((\d+)\)', msg)
            if m: ma20_val = int(m.group(1))
        if "MA200" in msg and ma200_val is None:
            m = re.search(r'MA200\((\d+)\)', msg)
            if m: ma200_val = int(m.group(1))

    transition = {}
    reasons = []

    # Check: above MA20 despite being in BEAR = improving
    above_ma20 = ma20_val and sh_close > ma20_val
    if above_ma20 and verdict == "bear":
        transition["state"] = "熊→震荡过渡"
        transition["hint"] = f"虽在MA200下方{days_below}天，但已站上MA20({ma20_val})，短期动能改善"
        reasons.append(f"✅ 上证{sh_close:.0f}站上MA20({ma20_val})")
        if ma200_val:
            dist_to_ma200 = ma200_val - sh_close
            pct_to_ma200 = (ma200_val/sh_close - 1)*100
            reasons.append(f"距MA200({ma200_val})仅{dist_to_ma200:.0f}点({pct_to_ma200:.1f}%)")
        if margin_chg > 0:
            reasons.append(f"✅ 融资回升{margin_chg:+.1f}%，资金回补")
        if dd_52w > -10:
            reasons.append(f"✅ 回撤仅{dd_52w:.1f}%，调整温和")
    elif verdict == "bear":
        transition["state"] = "熊市确认"
        transition["hint"] = "MA200下方且MA20未收复，建议防御为主"
        reasons.append(f"⚠️ MA200下方{days_below}天，MA20未收复")
        if margin_days > 0:
            reasons.append(f"⚠️ 融资连降{margin_days}天")

    if transition:
        payload["transition"] = transition
        payload["transition_reasons"] = reasons

    return payload


def get_regime_history(limit: int = 120) -> list[dict]:
    from web.snapshots import list_snapshots
    return list_snapshots("regime", limit=limit)
