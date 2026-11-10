"""Consent-based SIM tracking: consent lifecycle, scheduled pings, and turning network fixes into locations.

Rules
  * A number is located only while its consent is ACTIVE. Fixes for any other state are discarded.
  * Every consent change is written to `sim_events` (audit trail / proof of consent).
  * Drivers with an open trip are pinged every SIM_PING_ON_TRIP_MIN, idle drivers every SIM_PING_IDLE_MIN
    (pings are billed per request, so idle trucks are checked rarely).
  * Fixes go through the same ingestion as app GPS (services/location.py) with source="sim".
"""

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...geo import haversine_m
from ...models import Driver, DriverLastLocation, LocationSource, SimConsent, SimEvent, SimStatus, Trip, TripStatus
from ...phone import to_msisdn
from ...schemas import Point, UpdateDriverLocationRequest
from ...timeutil import utcnow
from .. import location
from .providers import ProviderResult, SimFix, SimProviderError, get_provider

log = logging.getLogger(__name__)

MAX_FAILURES_BEFORE_BACKOFF = 3


def _event(db: Session, consent: SimConsent, kind: str, detail: str | None = None) -> None:
    db.add(SimEvent(driver_id=consent.driver_id, msisdn=consent.msisdn, kind=kind, detail=detail))


def _apply(db: Session, consent: SimConsent, result: ProviderResult, via: str) -> None:
    """Move a consent to the status the provider reported, recording the change."""
    consent.provider_ref = result.provider_ref or consent.provider_ref
    consent.operator = result.operator or consent.operator
    if result.status == consent.status:
        return
    now = utcnow()
    consent.status = result.status
    if result.status == SimStatus.ACTIVE:
        consent.activated_at = now
        consent.expires_at = now + timedelta(days=settings.sim_consent_validity_days)
        consent.revoked_at = None
        consent.consecutive_failures, consent.last_error = 0, None
    elif result.status == SimStatus.REVOKED:
        consent.revoked_at = now
    elif result.status == SimStatus.FAILED:
        consent.last_error = result.error or "Provider could not register this number"
    _event(db, consent, result.status, f"via {via}" + (f": {result.error}" if result.error else ""))


def get_consent(db: Session, driver_id: int) -> SimConsent | None:
    return db.get(SimConsent, driver_id)


def request_consent(db: Session, driver: Driver) -> SimConsent:
    """Send (or re-send) the consent SMS to the driver's SIM. Does not commit."""
    consent = db.get(SimConsent, driver.id)
    if consent and consent.status in (SimStatus.ACTIVE, SimStatus.PENDING):
        return consent
    provider = get_provider()
    msisdn = to_msisdn(driver.phone)
    if consent is None:
        consent = SimConsent(driver_id=driver.id, msisdn=msisdn, provider=provider.name)
        db.add(consent)
    consent.msisdn, consent.provider = msisdn, provider.name
    consent.status, consent.requested_at = SimStatus.PENDING, utcnow()
    consent.activated_at = consent.expires_at = consent.revoked_at = None
    consent.consecutive_failures, consent.last_error = 0, None
    db.flush()
    _event(db, consent, "CONSENT_REQUESTED", f"provider {provider.name}")
    try:
        _apply(db, consent, provider.request_consent(db, consent), provider.name)
    except SimProviderError as exc:
        _apply(db, consent, ProviderResult(status=SimStatus.FAILED, error=str(exc)), provider.name)
    return consent


def revoke(db: Session, driver: Driver, reason: str = "by operator") -> SimConsent:
    consent = db.get(SimConsent, driver.id)
    if consent is None:
        raise HTTPException(404, "Driver has no SIM tracking registration")
    if consent.status in (SimStatus.ACTIVE, SimStatus.PENDING):
        try:
            get_provider().revoke(db, consent)
        except SimProviderError as exc:
            log.warning("Provider revoke failed for %s: %s", consent.msisdn, exc)
        _apply(db, consent, ProviderResult(status=SimStatus.REVOKED), reason)
    return consent


def set_status_from_provider(db: Session, msisdn: str, status: str, via: str, operator: str | None = None,
                             error: str | None = None) -> SimConsent | None:
    """Consent change pushed by the provider (webhook) or by the driver's SMS reply (mock)."""
    consent = db.scalar(select(SimConsent).where(SimConsent.msisdn == msisdn))
    if consent is None:
        return None
    if status == SimStatus.ACTIVE and consent.status not in (SimStatus.PENDING, SimStatus.ACTIVE):
        # Only a consent we asked for can become active; a stray "yes" must not re-enable a revoked number.
        return consent
    _apply(db, consent, ProviderResult(status=status, operator=operator, error=error), via)
    return consent


def ping_interval_min(db: Session, driver_id: int) -> float:
    on_trip = db.scalar(select(Trip.id).where(Trip.driver_id == driver_id, Trip.status.in_(TripStatus.OPEN)).limit(1))
    return settings.sim_ping_on_trip_min if on_trip else settings.sim_ping_idle_min


def stale_after_minutes(last: DriverLastLocation | None) -> float:
    """How long without a location before a trip is flagged as stale, given how it is tracked."""
    if last is not None and last.source == LocationSource.SIM:
        return max(settings.stale_location_minutes, settings.sim_ping_on_trip_min * settings.sim_stale_after_pings)
    return settings.stale_location_minutes


def ingest_fix(db: Session, consent: SimConsent, fix: SimFix, via: str) -> dict:
    """Store a network fix as a location point. Commits."""
    if consent.status != SimStatus.ACTIVE:
        return {"accepted": 0, "dropped": 1, "reason": f"consent is {consent.status}"}
    now = utcnow()
    if fix.ts > now + timedelta(minutes=5):
        fix.ts = now  # a clock running ahead would make every later fix look "old" and get dropped
    driver = db.get(Driver, consent.driver_id)
    last = db.get(DriverLastLocation, driver.id)
    speed = None  # networks do not report speed; derive it from the previous fix when the truck clearly moved
    if last is not None and fix.ts > last.ts:
        dt = (fix.ts - last.ts).total_seconds()
        moved = haversine_m(last.lat, last.lon, fix.lat, fix.lon)
        if dt >= 60:
            # Straight-line progress between sparse fixes x ROAD_FACTOR ~ road speed (the basis the ETA uses).
            speed = moved / dt * settings.road_factor if moved > fix.accuracy_m else 0.0
    consent.last_fix_at = fix.ts
    consent.operator = fix.operator or consent.operator
    point = UpdateDriverLocationRequest(pt=Point(lat=fix.lat, lon=fix.lon), ts=fix.ts, acc=fix.accuracy_m, v=speed)
    result = location.update_driver_location(db, driver, [point], source=LocationSource.SIM)
    log.debug("SIM fix via %s for driver %s: %s", via, driver.id, result)
    return result


def locate_now(db: Session, consent: SimConsent) -> dict:
    """Ask the provider for a fix right now. Commits."""
    if consent.status != SimStatus.ACTIVE:
        raise HTTPException(409, f"SIM consent is {consent.status}; the driver must consent first")
    provider = get_provider()
    consent.last_ping_at = utcnow()
    try:
        fix = provider.locate(db, consent)
    except SimProviderError as exc:
        consent.consecutive_failures += 1
        consent.last_error = str(exc)
        db.commit()
        return {"ok": False, "error": str(exc)}
    if fix is None:
        consent.consecutive_failures += 1
        consent.last_error = "SIM not reachable (switched off or out of coverage)"
        db.commit()
        return {"ok": False, "error": consent.last_error}
    consent.consecutive_failures, consent.last_error = 0, None
    result = ingest_fix(db, consent, fix, provider.name)
    return {"ok": True, "fix": {"lat": fix.lat, "lon": fix.lon, "accuracy_m": fix.accuracy_m}, **result}


def _due(db: Session, consent: SimConsent, now) -> bool:
    if not db.get(Driver, consent.driver_id).active:
        return False
    interval = ping_interval_min(db, consent.driver_id)
    if interval <= 0:
        return False
    if consent.consecutive_failures >= MAX_FAILURES_BEFORE_BACKOFF:
        interval *= 2  # unreachable phone: back off to halve the cost of failed pings
    return consent.last_ping_at is None or now - consent.last_ping_at >= timedelta(minutes=interval)


def poll(db: Session) -> dict:
    """One scheduler tick: expire old consents, refresh pending ones, ping the ones that are due."""
    now = utcnow()
    provider = get_provider()
    stats = {"expired": 0, "pinged": 0, "fixes": 0, "failed": 0}

    for c in db.scalars(select(SimConsent).where(SimConsent.status.in_((SimStatus.PENDING, SimStatus.ACTIVE)))):
        if c.status == SimStatus.PENDING:
            if now - c.requested_at > timedelta(hours=settings.sim_pending_consent_hours):
                _apply(db, c, ProviderResult(status=SimStatus.EXPIRED), "no reply from driver")
                stats["expired"] += 1
                continue
            try:
                _apply(db, c, provider.consent_status(db, c), provider.name)
            except SimProviderError as exc:
                c.last_error = str(exc)
        elif c.expires_at and c.expires_at <= now:
            _apply(db, c, ProviderResult(status=SimStatus.EXPIRED), "consent validity ended")
            stats["expired"] += 1
    db.commit()

    due = [c for c in db.scalars(select(SimConsent).where(SimConsent.status == SimStatus.ACTIVE)) if _due(db, c, now)]
    for c in due:
        stats["pinged"] += 1
        try:
            res = locate_now(db, c)
        except Exception:
            log.exception("SIM ping failed for driver %s", c.driver_id)
            db.rollback()
            stats["failed"] += 1
            continue
        stats["fixes" if res["ok"] else "failed"] += 1
    return stats
