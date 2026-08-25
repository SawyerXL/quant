"""Auth API routes."""
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from web.db import get_db
from web.auth import (
    hash_password, verify_password, create_session,
    generate_invite_code, validate_invite_code, generate_csrf_token,
    current_user, admin_user,
)
from web.models import User

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    invite_code: str = Form(""),
    db: Session = Depends(get_db),
):
    """Register with invite code. First user becomes admin (no invite needed)."""
    is_first = db.query(User).count() == 0
    error = None

    if not is_first:
        inv = validate_invite_code(db, invite_code)
        if not inv:
            error = "邀请码无效或已过期"
    else:
        inv = None

    username = username.strip()
    # XSS prevention: only allow alphanumeric + Chinese chars + underscore
    import re
    if not re.match(r'^[\w一-鿿]+$', username):
        error = "用户名只能包含字母、数字、中文和下划线"
    elif not error and len(username) < 2:
        error = "用户名至少2个字符"
    if not error and len(password) < 4:
        error = "密码至少4个字符"
    if not error and db.query(User).filter(User.username == username).first():
        error = "用户名已被占用"

    if error:
        return _templates.TemplateResponse(request, "login.html", {
            "error": error, "reg_username": username,
        }, status_code=400)

    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin" if is_first else "member",
        invite_code=(inv.code if inv else None),
    )
    db.add(user)
    db.commit()

    if inv:
        inv.used_by = user.id
        inv.used_at = datetime.utcnow()
        db.commit()

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        "session", create_session(user.id),
        httponly=True, max_age=7*86400, samesite="lax",
    )
    return resp


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    error = None
    if not user or not verify_password(password, user.password_hash):
        error = "用户名或密码错误"
    elif not user.is_active:
        error = "账号已被禁用，请联系管理员"
    elif user.expires_at and datetime.utcnow() > user.expires_at:
        error = "账号已过期，请联系管理员"

    if error:
        return _templates.TemplateResponse(request, "login.html", {
            "error": error, "username": username,
        }, status_code=401)

    user.last_login = datetime.utcnow()
    db.commit()

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        "session", create_session(user.id),
        httponly=True, max_age=7*86400, samesite="lax",
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session")
    return resp


@router.get("/me")
async def me(user=Depends(current_user)):
    return {"username": user.username, "role": user.role}


@router.post("/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    """User changes their own password."""
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="New password too short (min 4 chars)")
    user.password_hash = hash_password(new_password)
    db.commit()
    return {"status": "changed"}


@router.post("/invites")
async def create_invite(user=Depends(admin_user), db: Session = Depends(get_db)):
    code = generate_invite_code(db, user)
    return {"code": code}
