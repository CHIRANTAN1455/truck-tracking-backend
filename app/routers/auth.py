import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_session, current_session, hash_secret
from ..config import settings
from ..db import get_db
from ..models import AuthSession, Driver, OtpRequest
from ..schemas import AdminLogin, OtpRequestIn, OtpVerifyIn
from ..services import notify
from ..timeutil import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)

OTP_TTL = timedelta(minutes=5)
MAX_OTP_ATTEMPTS = 5


@router.post("/admin/login")
def admin_login(body: AdminLogin, db: Session = Depends(get_db)):
    user_ok = secrets.compare_digest(body.username, settings.admin_username)
    pass_ok = secrets.compare_digest(body.password, settings.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials")
    return {"token": create_session(db, "admin"), "role": "admin"}


@router.post("/driver/otp/request")
def driver_otp_request(body: OtpRequestIn, db: Session = Depends(get_db)):
    driver = db.scalar(select(Driver).where(Driver.phone == body.phone, Driver.active.is_(True)))
    if driver is None:
        raise HTTPException(404, "No active driver with this phone number")
    otp = f"{secrets.randbelow(10_000):04d}"
    db.merge(OtpRequest(phone=body.phone, otp_hash=hash_secret(otp), expires_at=utcnow() + OTP_TTL, attempts=0))
    notify.send(db, "sms", body.phone, f"Your login OTP is {otp}")
    db.commit()
    resp = {"sent": True}
    if settings.dev_mode:
        resp["dev_otp"] = otp
    return resp


@router.post("/driver/otp/verify")
def driver_otp_verify(body: OtpVerifyIn, db: Session = Depends(get_db)):
    req = db.get(OtpRequest, body.phone)
    if req is None or req.expires_at < utcnow() or req.attempts >= MAX_OTP_ATTEMPTS:
        raise HTTPException(401, "OTP expired, request a new one")
    if not secrets.compare_digest(req.otp_hash, hash_secret(body.otp.strip())):
        req.attempts += 1
        db.commit()
        raise HTTPException(401, "Wrong OTP")
    driver = db.scalar(select(Driver).where(Driver.phone == body.phone))
    db.delete(req)
    db.commit()
    return {"token": create_session(db, "driver", driver.id), "role": "driver", "driver_id": driver.id}


@router.post("/logout")
def logout(sess: AuthSession = Depends(current_session), db: Session = Depends(get_db)):
    db.delete(db.get(AuthSession, sess.token))
    db.commit()
    return {"ok": True}
