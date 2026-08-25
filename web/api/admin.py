"""Admin API routes."""
import json
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.orm import Session
from web.auth import admin_user, hash_password
from web.db import get_db
from web.models import User, InviteCode
from web.config import SNAPSHOT_DIR

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/snapshots")
async def snapshot_status(user=Depends(admin_user)):
    snaps = {}
    for f in sorted(SNAPSHOT_DIR.glob("*_latest.json"), reverse=True):
        name = f.name.replace("_latest.json", "")
        data = json.loads(f.read_text())
        snaps[name] = {"computed_at": data.get("computed_at", "")[:19], "source": data.get("source", "")}
    return {"snapshots": snaps, "dir": str(SNAPSHOT_DIR)}


@router.post("/refresh/{name}")
async def refresh_snapshot(name: str, user=Depends(admin_user)):
    import threading
    def _refresh():
        try:
            if name == "regime":
                from web.services.regime_service import refresh_regime; refresh_regime()
            elif name == "signal":
                from web.services.signal_service import get_latest_signal; get_latest_signal(force=True)
            elif name == "candidates":
                from web.services.signal_service import get_candidates; get_candidates()
            elif name == "ma10_triggers":
                from web.services.portfolio_service import get_ma10_triggers; get_ma10_triggers()
        except Exception: pass
    threading.Thread(target=_refresh, daemon=True).start()
    return {"status": "started", "name": name}


# ═══ User CRUD ═══

@router.get("/users")
async def list_users(user=Depends(admin_user), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [{
        "username": u.username, "role": u.role,
        "is_active": u.is_active,
        "expires_at": str(u.expires_at)[:10] if u.expires_at else None,
        "invite_code": u.invite_code or "—",
        "created_at": str(u.created_at)[:19] if u.created_at else "",
        "last_login": str(u.last_login)[:19] if u.last_login else "",
    } for u in users]


@router.post("/users")
async def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
    expires_at: str = Form(""),
    user=Depends(admin_user),
    db: Session = Depends(get_db),
):
    """Admin creates a user directly (no invite needed)."""
    username = username.strip()
    if len(username) < 2: raise HTTPException(status_code=400, detail="Username too short")
    if len(password) < 4: raise HTTPException(status_code=400, detail="Password too short")
    if role not in ("admin", "member"): raise HTTPException(status_code=400, detail="Invalid role")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=400, detail="Username already taken")

    u = User(username=username, password_hash=hash_password(password), role=role)
    if expires_at.strip():
        try: u.expires_at = datetime.strptime(expires_at.strip(), "%Y-%m-%d")
        except ValueError: raise HTTPException(status_code=400, detail="Invalid date: YYYY-MM-DD")
    db.add(u); db.commit()
    return {"status": "created", "username": username, "role": role}


@router.put("/users/{username}")
async def update_user(
    username: str,
    role: str = Form(""),
    is_active: str = Form(""),
    expires_at: str = Form(""),
    new_password: str = Form(""),
    user=Depends(admin_user),
    db: Session = Depends(get_db),
):
    """Update user fields. Only changed fields need to be sent."""
    target = db.query(User).filter(User.username == username).first()
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id and role and role != user.role:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    changed = []
    if role and role in ("admin", "member") and role != target.role:
        if role == "member" and target.role == "admin" and db.query(User).filter(User.role=="admin").count() <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote last admin")
        target.role = role; changed.append("role")
    if is_active in ("true", "false"):
        val = is_active == "true"
        if target.id == user.id and not val:
            raise HTTPException(status_code=400, detail="Cannot disable yourself")
        target.is_active = val; changed.append("active")
    if expires_at is not None:
        if expires_at.strip():
            try: target.expires_at = datetime.strptime(expires_at.strip(), "%Y-%m-%d")
            except ValueError: raise HTTPException(status_code=400, detail="Invalid date: YYYY-MM-DD")
        else: target.expires_at = None
        changed.append("expiry")
    if new_password:
        if len(new_password) < 4: raise HTTPException(status_code=400, detail="Password too short")
        target.password_hash = hash_password(new_password); changed.append("password")

    db.commit()
    return {"status": "updated", "username": username, "changed": changed}


@router.delete("/users/{username}")
async def delete_user(username: str, user=Depends(admin_user), db: Session = Depends(get_db)):
    target = db.query(User).filter(User.username == username).first()
    if not target: raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id: raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db.query(InviteCode).filter(InviteCode.created_by == target.id).delete()
    db.query(InviteCode).filter(InviteCode.used_by == target.id).update({InviteCode.used_by: None})
    db.delete(target); db.commit()
    return {"status": "deleted", "username": username}


# ═══ Invites ═══

@router.get("/invites")
async def list_invites(user=Depends(admin_user), db: Session = Depends(get_db)):
    invites = db.query(InviteCode).order_by(InviteCode.created_at.desc()).limit(20).all()
    return [{"code": i.code, "used": i.used_by is not None,
        "created_at": str(i.created_at)[:19] if i.created_at else "",
        "used_at": str(i.used_at)[:19] if i.used_at else "",
        "expires_at": str(i.expires_at)[:19] if i.expires_at else ""} for i in invites]


@router.delete("/invites/{code}")
async def delete_invite(code: str, user=Depends(admin_user), db: Session = Depends(get_db)):
    inv = db.query(InviteCode).filter(InviteCode.code == code.upper()).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite code not found")
    db.delete(inv); db.commit()
    return {"status": "deleted", "code": code.upper()}
