from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..db import get_db
from ..models import Alert, Driver, Notification, Trip, TripStatus, Vehicle
from ..services import alerts
from ..timeutil import iso, to_naive_utc, utcnow

router = APIRouter(tags=["alerts & reports"], dependencies=[Depends(require_admin)])


@router.get("/alerts")
def list_alerts(open_only: bool = True, trip_id: str | None = None, db: Session = Depends(get_db)):
    q = select(Alert).order_by(Alert.created_at.desc()).limit(500)
    if open_only:
        q = q.where(Alert.resolved_at.is_(None))
    if trip_id:
        q = q.where(Alert.trip_id == trip_id)
    return [ser.alert(a) for a in db.scalars(q)]


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "Alert not found")
    a.resolved_at = a.resolved_at or utcnow()
    db.commit()
    return ser.alert(a)


@router.post("/alerts/run-checks")
def run_checks(db: Session = Depends(get_db)):
    """Run the delay / stale-location checks now instead of waiting for the background loop."""
    return {"checked_trips": alerts.run_periodic_checks(db)}


@router.get("/notifications")
def list_notifications(trip_id: str | None = None, limit: int = 200, db: Session = Depends(get_db)):
    q = select(Notification).order_by(Notification.created_at.desc()).limit(min(limit, 1000))
    if trip_id:
        q = q.where(Notification.trip_id == trip_id)
    return [ser.notification(n) for n in db.scalars(q)]


@router.get("/reports/trips")
def trip_report(start: datetime | None = None, end: datetime | None = None, driver_id: int | None = None,
                db: Session = Depends(get_db)):
    """Distance and duration per trip, plus totals. Filters on trip creation time."""
    q = select(Trip).order_by(Trip.created_at.desc())
    if start:
        q = q.where(Trip.created_at >= to_naive_utc(start))
    if end:
        q = q.where(Trip.created_at <= to_naive_utc(end))
    if driver_id:
        q = q.where(Trip.driver_id == driver_id)
    rows, by_status = [], {}
    total_km = total_min = 0.0
    for t in db.scalars(q):
        by_status[t.status] = by_status.get(t.status, 0) + 1
        duration_min = None
        if t.started_at:
            duration_min = round(((t.ended_at or utcnow()) - t.started_at).total_seconds() / 60, 1)
        on_time = None
        if t.status == TripStatus.DELIVERED and t.expected_delivery_at and t.ended_at:
            on_time = t.ended_at <= t.expected_delivery_at
        d = db.get(Driver, t.driver_id) if t.driver_id else None
        v = db.get(Vehicle, t.vehicle_id) if t.vehicle_id else None
        if t.status == TripStatus.DELIVERED:
            total_km += t.distance_m / 1000
            total_min += duration_min or 0
        rows.append({"trip_id": t.id, "shipment_id": t.shipment_id, "status": t.status,
                     "driver": d.name if d else None, "vehicle": v.reg_no if v else None,
                     "created_at": iso(t.created_at), "started_at": iso(t.started_at), "ended_at": iso(t.ended_at),
                     "distance_km": round(t.distance_m / 1000, 2),
                     "planned_distance_km": round((t.planned_distance_m or 0) / 1000, 2),
                     "duration_min": duration_min, "on_time": on_time})
    return {"summary": {"trips": len(rows), "by_status": by_status,
                        "delivered_distance_km": round(total_km, 2),
                        "delivered_duration_hours": round(total_min / 60, 2)},
            "trips": rows}
