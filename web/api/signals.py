"""Signal API routes."""
from datetime import date
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from web.auth import current_user
from web.services.signal_service import get_latest_signal, get_candidates, get_stock_analysis
from web.snapshots import list_snapshots

router = APIRouter(prefix="/api/signals", tags=["signals"])

# Per-user daily refresh counter: {user_id: {"date": "2026-08-04", "count": 2}}
_refresh_quota: dict[str, dict] = defaultdict(lambda: {"date": "", "count": 0})
DEFAULT_LIMIT = 4
ADMIN_LIMIT = 20


def _get_limit(user_role: str) -> int:
    return ADMIN_LIMIT if user_role == "admin" else DEFAULT_LIMIT


def _check_quota(user_id: str, role: str = "member") -> dict:
    """Check and increment refresh quota. Returns {allowed, remaining, reset}."""
    today = str(date.today())
    limit = _get_limit(role)
    q = _refresh_quota[user_id]
    if q["date"] != today:
        q["date"] = today
        q["count"] = 0
    q["count"] += 1
    used_now = min(q["count"], limit)
    remaining = max(0, limit - used_now)
    return {
        "allowed": q["count"] <= limit,
        "used": used_now,
        "remaining": remaining,
        "limit": limit,
    }


@router.get("/quota")
async def signal_quota(user=Depends(current_user)):
    """Check remaining signal refresh quota for today."""
    today = str(date.today())
    limit = _get_limit(user.role)
    q = _refresh_quota[user.id]
    if q["date"] != today:
        return {"used": 0, "remaining": limit, "limit": limit}
    used = min(q["count"], limit)
    return {"used": used, "remaining": max(0, limit - used), "limit": limit}


@router.get("/latest")
async def signal_latest(user=Depends(current_user)):
    """Latest daily signal snapshot."""
    return get_latest_signal()


@router.get("/history")
async def signal_history(limit: int = 30, user=Depends(current_user)):
    """Historical signal snapshots."""
    snaps = list_snapshots("signal", limit=limit)
    history = []
    for s in snaps:
        p = s.get("payload", s)
        history.append({
            "date": p.get("signal_date") or p.get("date") or s.get("computed_at", "")[:10],
            "regime": p.get("regime"),
            "position_ratio": p.get("position_ratio"),
            "holdings_count": len(p.get("holdings", [])),
        })
    return history


@router.get("/candidates")
async def signal_candidates(limit: int = 10, refresh: bool = False, user=Depends(current_user)):
    """Strategy-filtered candidates. Set refresh=true to consume daily quota."""
    quota = None
    if refresh:
        quota = _check_quota(user.id, user.role)
        if not quota["allowed"]:
            raise HTTPException(
                status_code=429,
                detail=f"每日限刷新{quota['limit']}次，已用完。请明天再试。"
            )
        result = get_candidates(limit=limit)
    else:
        # Read from cache, no quota consumed
        from web.snapshots import read_snapshot
        snap = read_snapshot("candidates")
        payload = snap.get("payload", snap) if snap else None
        if payload and isinstance(payload, list) and len(payload) > 0:
            result = payload
        else:
            result = get_candidates(limit=limit)
            # Cache for next load
            from web.snapshots import write_snapshot
            write_snapshot("candidates", result, source="signal_candidates")
        today = str(date.today())
        q = _refresh_quota[user.id]
        used = q["count"] if q["date"] == today else 0
        limit = _get_limit(user.role)
        quota = {"used": min(used, limit), "remaining": max(0, limit - used), "limit": limit}
    return {"candidates": result, "quota": quota}


@router.get("/candidates/{code}/ai")
async def stock_ai_comment(code: str, user=Depends(current_user)):
    """AI-generated analysis comment for a stock."""
    analysis = get_stock_analysis(code)
    if analysis.get("error"):
        return {"error": analysis["error"]}
    from web.services.llm_analyzer import generate_stock_comment
    comment = generate_stock_comment(code, analysis.get("price", {}).get("name", ""), analysis)
    return {"code": code, "name": analysis.get("price", {}).get("name", ""), "comment": comment or "AI分析暂不可用"}


@router.get("/candidates/{code}")
async def stock_detail(code: str, user=Depends(current_user)):
    """Deep-dive analysis for one stock."""
    return get_stock_analysis(code)
