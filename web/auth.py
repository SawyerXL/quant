"""Auth: invite-code registration, pbkdf2 login, signed session cookie, CSRF."""
import hashlib, secrets, string
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Depends, status
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from web.config import SECRET_KEY, SESSION_DAYS, INVITE_CODE_LENGTH, INVITE_CODE_EXPIRY_DAYS
from web.db import get_db
from web.models import User, InviteCode

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")
csrf_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="csrf")

# ═══ Password helpers ═══

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${h.hex()}"

def verify_password(password: str, stored: str) -> bool:
    salt, h = stored.split("$")
    return h == hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()

# ═══ Session helpers ═══

def create_session(user_id: str) -> str:
    return serializer.dumps({"user_id": user_id, "iat": datetime.utcnow().isoformat()})

def read_session(token: str) -> dict | None:
    try:
        data = serializer.loads(token, max_age=SESSION_DAYS * 86400)
        return data
    except Exception:
        return None

# ═══ Auth dependencies ═══

async def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    data = read_session(token)
    if not data:
        raise HTTPException(status_code=401, detail="Session expired")
    user = db.query(User).filter(User.id == data["user_id"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    if user.expires_at and datetime.utcnow() > user.expires_at:
        raise HTTPException(status_code=403, detail="Account expired")
    return user

async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

async def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get("session")
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    return db.query(User).filter(User.id == data["user_id"]).first()

# ═══ CSRF ═══

def generate_csrf_token() -> str:
    return csrf_serializer.dumps({"nonce": secrets.token_hex(16)})

def verify_csrf(token: str) -> bool:
    try:
        csrf_serializer.loads(token, max_age=3600)
        return True
    except Exception:
        return False

# ═══ Invite code helpers ═══

def generate_invite_code(db: Session, creator: User) -> str:
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(INVITE_CODE_LENGTH))
    inv = InviteCode(
        code=code,
        created_by=creator.id,
        expires_at=datetime.utcnow() + timedelta(days=INVITE_CODE_EXPIRY_DAYS),
    )
    db.add(inv)
    db.commit()
    return code

def validate_invite_code(db: Session, code: str) -> InviteCode | None:
    inv = db.query(InviteCode).filter(InviteCode.code == code.upper()).first()
    if inv and inv.is_valid:
        return inv
    return None
