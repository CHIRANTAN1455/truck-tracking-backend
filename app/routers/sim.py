"""Consent-based SIM tracking: operator actions and the aggregator webhook."""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..config import settings
from ..db import get_db
from ..models import Company, Driver, SimConsent, SimEvent, SimStatus
from ..phone import to_msisdn
from ..schemas import SimBulkConsent, SimMockReply, SimWebhookIn
from ..services import sim
from ..services.sim.providers import SimFix, map_status
from ..timeutil import to_naive_utc, utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["sim tracking"])
admin = [Depends(require_admin)]


def _driver(db: Session, driver_id: int) -> Driver:
    d = db.get(Driver, driver_id)
    if d is None:
        raise HTTPException(404, "Driver not found")
    return d


def _row(db: Session, c: SimConsent) -> dict:
    d = db.get(Driver, c.driver_id)
    company = db.get(Company, d.company_id) if d.company_id else None
    return {"driver": {"id": d.id, "name": d.name, "phone": d.phone, "active": d.active},
            "company": ser.company_ref(company), **ser.sim(c, sim.ping_interval_min(db, d.id))}


@router.get("/sim/status", dependencies=admin)
def sim_status(db: Session = Depends(get_db)):
    """Integration health: provider, ping policy and consent counts."""
    counts = dict(db.execute(select(SimConsent.status, func.count()).group_by(SimConsent.status)).all())
    without = db.scalar(select(func.count()).select_from(Driver)
                        .where(Driver.active.is_(True), Driver.id.not_in(select(SimConsent.driver_id))))
    return {"provider": settings.sim_provider, "webhook_enabled": bool(settings.sim_webhook_secret),
            "webhook_path": "/integrations/sim/webhook",
            "ping_on_trip_min": settings.sim_ping_on_trip_min, "ping_idle_min": settings.sim_ping_idle_min,
            "pending_consent_hours": settings.sim_pending_consent_hours,
            "consent_validity_days": settings.sim_consent_validity_days,
            "counts": {s: counts.get(s, 0) for s in (SimStatus.ACTIVE, SimStatus.PENDING, SimStatus.REVOKED,
                                                     SimStatus.EXPIRED, SimStatus.FAILED)},
            "drivers_without_consent": without}


@router.get("/sim/consents", dependencies=admin)
def list_consents(company_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)):
    q = select(SimConsent).join(Driver, Driver.id == SimConsent.driver_id).order_by(Driver.name)
    if company_id:
        q = q.where(Driver.company_id == company_id)
    if status:
        q = q.where(SimConsent.status == status)
    return [_row(db, c) for c in db.scalars(q)]


@router.get("/sim/events", dependencies=admin)
def list_events(driver_id: int | None = None, limit: int = 200, db: Session = Depends(get_db)):
    q = select(SimEvent).order_by(SimEvent.created_at.desc(), SimEvent.id.desc()).limit(min(limit, 2000))
    if driver_id:
        q = q.where(SimEvent.driver_id == driver_id)
    return [ser.sim_event(e) for e in db.scalars(q)]


@router.post("/drivers/{driver_id}/sim/consent", dependencies=admin)
def request_consent(driver_id: int, db: Session = Depends(get_db)):
    """Send the consent SMS (again) to the driver's SIM."""
    d = _driver(db, driver_id)
    if not d.active:
        raise HTTPException(409, "Driver is inactive")
    c = sim.request_consent(db, d)
    db.commit()
    return _row(db, c)


@router.post("/drivers/{driver_id}/sim/revoke", dependencies=admin)
def revoke(driver_id: int, db: Session = Depends(get_db)):
    c = sim.revoke(db, _driver(db, driver_id))
    db.commit()
    return _row(db, c)


@router.post("/drivers/{driver_id}/sim/locate", dependencies=admin)
def locate(driver_id: int, db: Session = Depends(get_db)):
    """Locate the driver's SIM now instead of waiting for the next scheduled ping."""
    _driver(db, driver_id)
    c = sim.get_consent(db, driver_id)
    if c is None:
        raise HTTPException(404, "Driver has no SIM tracking registration")
    return sim.locate_now(db, c)


@router.post("/sim/consents/bulk-request", dependencies=admin)
def bulk_request(body: SimBulkConsent, db: Session = Depends(get_db)):
    """Send consent SMS to every active driver (optionally of one company) who is not ACTIVE or PENDING."""
    q = select(Driver).where(Driver.active.is_(True))
    if body.company_id:
        q = q.where(Driver.company_id == body.company_id)
    sent = 0
    for d in db.scalars(q):
        c = sim.get_consent(db, d.id)
        if c is None or c.status not in (SimStatus.ACTIVE, SimStatus.PENDING):
            sim.request_consent(db, d)
            sent += 1
    db.commit()
    return {"requested": sent}


@router.post("/sim/poll", dependencies=admin)
def poll_now(db: Session = Depends(get_db)):
    """Run one scheduler tick now (expire consents, ping due SIMs)."""
    return sim.poll(db)


@router.post("/integrations/sim/mock/reply", dependencies=admin)
def mock_reply(body: SimMockReply, db: Session = Depends(get_db)):
    """Development only: simulate the driver replying to the consent SMS (Y = allow, N = deny)."""
    if settings.sim_provider != "mock":
        raise HTTPException(404, "Only available with SIM_PROVIDER=mock")
    status = SimStatus.ACTIVE if body.reply.strip().upper() in ("Y", "YES") else SimStatus.REVOKED
    try:
        msisdn = to_msisdn(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    c = sim.set_status_from_provider(db, msisdn, status, f"driver SMS reply '{body.reply}'")
    if c is None:
        raise HTTPException(404, "No consent request for this number")
    db.commit()
    return _row(db, c)


# ------------------------------------------------------------------ webhook (called by the aggregator)

def _verify_signature(raw: bytes, signature: str | None) -> None:
    if not settings.sim_webhook_secret:
        raise HTTPException(503, "SIM webhook is not configured (set SIM_WEBHOOK_SECRET)")
    expected = "sha256=" + hmac.new(settings.sim_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature.strip()):
        raise HTTPException(401, "Bad signature")


@router.post("/integrations/sim/webhook")
async def webhook(request: Request, x_signature: str | None = Header(default=None),
                  db: Session = Depends(get_db)):
    """Location and consent events pushed by the telecom location aggregator.

    Signed with `X-Signature: sha256=<hex HMAC-SHA256 of the raw body with SIM_WEBHOOK_SECRET>`.
    Always answers 200 for a valid, signed request, with per-event results, so the provider does not
    retry events we deliberately ignored (unknown number, no consent).
    """
    raw = await request.body()
    _verify_signature(raw, x_signature)
    try:
        body = SimWebhookIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, f"Invalid payload: {exc}")

    results = []
    for ev in body.events:
        try:
            msisdn = to_msisdn(ev.msisdn)
        except ValueError:
            results.append({"event_id": ev.event_id, "result": "invalid_msisdn"})
            continue
        if ev.type == "consent":
            c = sim.set_status_from_provider(db, msisdn, map_status(ev.status), "webhook",
                                             ev.operator, ev.error)
            db.commit()
            results.append({"event_id": ev.event_id, "result": "ok" if c else "unknown_msisdn"})
            continue

        c = db.scalar(select(SimConsent).where(SimConsent.msisdn == msisdn))
        if c is None:
            results.append({"event_id": ev.event_id, "result": "unknown_msisdn"})
            continue
        if c.status != SimStatus.ACTIVE:
            # Never store a location we have no consent for.
            results.append({"event_id": ev.event_id, "result": f"ignored_consent_{c.status.lower()}"})
            continue
        if ev.lat is None or ev.lon is None:
            results.append({"event_id": ev.event_id, "result": "missing_coordinates"})
            continue
        fix = SimFix(lat=ev.lat, lon=ev.lon, accuracy_m=ev.accuracy_m or 1000.0,
                     ts=to_naive_utc(ev.ts) if ev.ts else utcnow(), operator=ev.operator, cell_id=ev.cell_id)
        c.consecutive_failures, c.last_error = 0, None
        res = sim.ingest_fix(db, c, fix, "webhook")
        results.append({"event_id": ev.event_id, "result": "accepted" if res["accepted"] else "duplicate_or_old"})
    return {"received": len(body.events), "results": results}
