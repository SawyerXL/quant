"""Market API routes."""
from fastapi import APIRouter, Depends
from web.auth import current_user, optional_user
from web.services.regime_service import get_regime_snapshot, refresh_regime, get_regime_history
from web.services.market_analysis import get_market_analysis
from web.services.intraday_service import get_intraday_analysis, get_opening30_analysis

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/regime")
async def regime(user=Depends(optional_user)):
    """Latest market regime status."""
    snap = get_regime_snapshot()
    if not snap:
        return {"error": "No snapshot available"}
    return snap


@router.get("/regime/history")
async def regime_history(limit: int = 120, user=Depends(optional_user)):
    """Historical regime verdicts — chart-friendly format. Public read."""
    raw = get_regime_history(limit=limit)
    result = []
    for s in raw:
        p = s.get("payload", s)
        d = (p.get("date") or s.get("computed_at", "")[:10] or "")
        result.append({
            "date": str(d)[:10],
            "verdict": p.get("verdict", "normal"),
            "ma200_dist_pct": p.get("ma200_dist_pct"),
            "sh_close": p.get("sh_close"),
        })
    result.sort(key=lambda x: x["date"])
    return result


@router.post("/regime/refresh")
async def regime_refresh(user=Depends(current_user)):
    """Manual refresh (admin-only in production)."""
    snap = refresh_regime()
    return snap


@router.get("/opening30")
async def opening30(user=Depends(optional_user)):
    """开盘半小时量能分析 (9:30-11:00有效)."""
    from web.services.intraday_service import get_opening30_analysis
    return get_opening30_analysis()


@router.get("/analysis")
async def market_analysis(user=Depends(optional_user)):
    """Full market analysis: overnight + technical + outlook + LLM summary."""
    result = get_market_analysis()
    # Add LLM summary asynchronously (don't block the response)
    import threading
    def _add_llm():
        try:
            from web.services.llm_analyzer import generate_market_summary
            llm = generate_market_summary(result)
            if llm:
                result["llm_summary"] = llm
        except Exception:
            pass
    t = threading.Thread(target=_add_llm, daemon=True)
    t.start()
    t.join(timeout=8)  # Wait up to 8s for LLM
    return result


@router.get("/intraday")
async def intraday_analysis(user=Depends(optional_user)):
    """Intraday A-share market trend analysis."""
    return get_intraday_analysis()


@router.get("/opening30")
async def opening30_analysis(user=Depends(optional_user)):
    """开盘半小时量能分析（修正版框架）."""
    return get_opening30_analysis()
