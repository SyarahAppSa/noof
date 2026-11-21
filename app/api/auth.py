"""المصادقة والصلاحيات للـAPI ولوحة التحكم."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, Role, User
from app.security import hash_api_key


def api_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(auth[7:].strip()), ApiKey.revoked.is_(False)))
    if not key or not key.user.is_active:
        raise HTTPException(401, "invalid token")
    return key.user


def require_owner(user: User = Depends(api_user)) -> User:
    if user.role != Role.owner:
        raise HTTPException(403, "owner only")
    return user


def session_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.get(User, uid)
    return u if u and u.is_active else None
