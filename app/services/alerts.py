"""Route deviation, delivery delay and stale-location alerts.

Deviation is evaluated inline for every location batch (services/location.py); delay and stale
location are evaluated by a periodic background check (`run_periodic_checks`). Each trip has at
most one open alert per kind, so a long outage produces one alert, not one per minute.
"""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alert, AlertKind, DriverLastLocation, Shipment, SimConsent, SimStatus, Trip, TripStatus
from ..timeutil import iso, utcnow
from . import notify

log = logging.getLogger(__name__)


def open_alert(db: Session, trip: Trip, kind: str, message: str, lat=None, lon=None) -> Alert | None:
    existing = db.scalar(
        select(Alert).where(Alert.trip_id == trip.id, Alert.kind == kind, Alert.resolved_at.is_(None))
    )
    if existing:
        return None
    alert = Alert(trip_id=trip.id, kind=kind, message=message, lat=lat, lon=lon)
    db.add(alert)
    notify.notify_ops(db, f"[{kind}] Trip {trip.id}: {message}", trip.id)
    return alert


def resolve_alerts(db: Session, trip_id: str, kind: str | None = None) -> None:
    q = select(Alert).where(Alert.trip_id == trip_id, Alert.resolved_at.is_(None))
    if kind:
        q = q.where(Alert.kind == kind)
    for alert in db.scalars(q):
        alert.resolved_at = utcnow()


def check_trip(db: Session, trip: Trip) -> None:
    from .sim import stale_after_minutes  # local imports: both modules import this one
    from .trips import compute_eta

    now = utcnow()
    last = db.get(DriverLastLocation, trip.driver_id) if trip.driver_id else None

    stale_min = stale_after_minutes(last)
    if last is None or now - last.received_at > timedelta(minutes=stale_min):
        since = iso(last.received_at) if last else "trip start"
        consent = db.get(SimConsent, trip.driver_id) if trip.driver_id else None
        why = ""
        if consent is None:
            why = " Driver is not registered for SIM tracking."
        elif consent.status != SimStatus.ACTIVE:
            why = f" SIM tracking consent is {consent.status}."
        elif consent.last_error:
            why = f" {consent.last_error}."
        open_alert(db, trip, AlertKind.LOCATION_STALE,
                   f"No location update for more than {stale_min:g} min (last: {since}).{why}",
                   last.lat if last else None, last.lon if last else None)

    if trip.expected_delivery_at and trip.status != TripStatus.REACHED:
        grace = timedelta(minutes=settings.delay_grace_minutes)
        eta = compute_eta(db, trip, last)
        eta_at = eta["eta_dt"] if eta else None
        late_now = now > trip.expected_delivery_at + grace
        late_eta = eta_at is not None and eta_at > trip.expected_delivery_at + grace
        if late_now or late_eta:
            shown = iso(eta_at) if eta_at else "unknown"
            open_alert(db, trip, AlertKind.DELAY,
                       f"Delivery expected by {iso(trip.expected_delivery_at)}, current ETA {shown}",
                       last.lat if last else None, last.lon if last else None)


def run_periodic_checks(db: Session) -> int:
    trips = list(db.scalars(select(Trip).where(Trip.status.in_(TripStatus.ON_RIDE))))
    for trip in trips:
        try:
            check_trip(db, trip)
        except Exception:
            log.exception("Alert check failed for trip %s", trip.id)
    db.commit()
    return len(trips)


def shipment_for(db: Session, trip: Trip) -> Shipment:
    return db.get(Shipment, trip.shipment_id)
