"""Token auth. Like LTS, the session token travels in a `token` request header."""

import hashlib
import secrets
from datetime import timedelta

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuthSession, Driver
from .timeutil import utcnow


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_session(db: Session, role: str, driver_id: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token=token,
            role=role,
            driver_id=driver_id,
            expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        )
    )
    db.commit()
    return token


def current_session(token: str | None = Header(default=None), db: Session = Depends(get_db)) -> AuthSession:
    if not token:
        raise HTTPException(401, "token (Header) not found")
    sess = db.get(AuthSession, token)
    if sess is None or sess.expires_at < utcnow():
        raise HTTPException(401, "Invalid or expired token")
    return sess


def require_admin(sess: AuthSession = Depends(current_session)) -> AuthSession:
    if sess.role != "admin":
        raise HTTPException(403, "Admin access required")
    return sess


def require_driver(sess: AuthSession = Depends(current_session), db: Session = Depends(get_db)) -> Driver:
    if sess.role != "driver" or sess.driver_id is None:
        raise HTTPException(403, "Driver access required")
    driver = db.get(Driver, sess.driver_id)
    if driver is None or not driver.active:
        raise HTTPException(403, "Driver is inactive")
    return driver
