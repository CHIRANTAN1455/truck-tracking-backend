"""Trip lifecycle: create -> assign -> pickup -> start -> in transit -> reached -> delivered.

Starting / ending a trip is the equivalent of LTS `/internal/ride/{rideId}/start|end`: while a trip
is in an ON_RIDE status, the driver's location updates are attached to it.
"""

import os
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..geo import haversine_m, osrm_route
from ..models import (
    DriverLastLocation, Driver, LocationPoint, ProofOfDelivery, Shipment, TrackingLink, Trip,
    TripEvent, TripStatus, Vehicle,
)
from ..timeutil import iso, to_naive_utc, utcnow
from . import alerts, notify

S = TripStatus

ALLOWED = {
    S.CREATED: {S.ASSIGNED, S.CANCELLED},
    S.ASSIGNED: {S.ASSIGNED, S.PICKED_UP, S.STARTED, S.CANCELLED},
    S.PICKED_UP: {S.STARTED, S.CANCELLED},
    S.STARTED: {S.IN_TRANSIT, S.REACHED, S.DELIVERED, S.CANCELLED},
    S.IN_TRANSIT: {S.REACHED, S.DELIVERED, S.CANCELLED},
    S.REACHED: {S.DELIVERED, S.CANCELLED},
}

# Status a driver / operator may set by hand. IN_TRANSIT and REACHED are also set automatically
# from GPS; DELIVERED requires proof of delivery.
MANUAL_STATUSES = {S.PICKED_UP, S.STARTED, S.IN_TRANSIT, S.REACHED}


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def get_trip(db: Session, trip_id: str) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(404, "Trip not found")
    return trip


def active_trip_for_driver(db: Session, driver_id: int) -> Trip | None:
    return db.scalar(
        select(Trip).where(Trip.driver_id == driver_id, Trip.status.in_(S.ON_RIDE)).order_by(Trip.started_at.desc())
    )


def _event(db: Session, trip: Trip, status: str, note: str | None = None, lat=None, lon=None) -> None:
    db.add(TripEvent(trip_id=trip.id, status=status, note=note, lat=lat, lon=lon))


def create_trip(db: Session, shipment_id: str, driver_id: int | None, vehicle_id: int | None,
                expected_delivery_at) -> Trip:
    shipment = db.get(Shipment, shipment_id)
    if shipment is None:
        raise HTTPException(404, "Shipment not found")
    open_trip = db.scalar(select(Trip).where(Trip.shipment_id == shipment_id, Trip.status.in_(S.OPEN)))
    if open_trip:
        raise HTTPException(409, f"Shipment already has open trip {open_trip.id}")

    route = osrm_route(settings.osrm_url, shipment.pickup_lat, shipment.pickup_lon,
                       shipment.drop_lat, shipment.drop_lon)
    if route:
        polyline, dist, dur = route
        source = "osrm"
    else:
        polyline = [[shipment.pickup_lat, shipment.pickup_lon], [shipment.drop_lat, shipment.drop_lon]]
        dist = haversine_m(shipment.pickup_lat, shipment.pickup_lon, shipment.drop_lat, shipment.drop_lon) \
            * settings.road_factor
        dur = dist / (settings.default_speed_kmph / 3.6)
        source = "straight_line"

    trip = Trip(
        id=new_id("TRP"), shipment_id=shipment_id, status=S.CREATED, planned_route=polyline,
        route_source=source, planned_distance_m=dist, planned_duration_s=dur,
        expected_delivery_at=to_naive_utc(expected_delivery_at) if expected_delivery_at else None,
    )
    db.add(trip)
    db.flush()
    _event(db, trip, S.CREATED)
    if driver_id is not None:
        assign(db, trip, driver_id, vehicle_id)
    db.commit()
    return trip


def assign(db: Session, trip: Trip, driver_id: int, vehicle_id: int | None) -> Trip:
    if trip.status not in (S.CREATED, S.ASSIGNED):
        raise HTTPException(409, f"Cannot (re)assign a trip in status {trip.status}")
    driver = db.get(Driver, driver_id)
    if driver is None or not driver.active:
        raise HTTPException(400, "Driver not found or inactive")
    vehicle_id = vehicle_id or driver.vehicle_id
    if vehicle_id is None:
        raise HTTPException(400, "Driver has no vehicle assigned; pass vehicle_id")
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or not vehicle.active:
        raise HTTPException(400, "Vehicle not found or inactive")
    if driver.company_id and vehicle.company_id and driver.company_id != vehicle.company_id:
        raise HTTPException(400, "Driver and vehicle belong to different companies")
    trip.driver_id, trip.vehicle_id = driver.id, vehicle.id
    trip.status = S.ASSIGNED
    _event(db, trip, S.ASSIGNED, f"Driver {driver.name}, vehicle {vehicle.reg_no}")
    db.get(Shipment, trip.shipment_id).status = S.ASSIGNED
    return trip


def ensure_tracking_link(db: Session, shipment: Shipment) -> TrackingLink:
    now = utcnow()
    link = db.scalar(
        select(TrackingLink)
        .where(TrackingLink.shipment_id == shipment.id, TrackingLink.revoked_at.is_(None),
               TrackingLink.expires_at > now)
        .order_by(TrackingLink.created_at.desc())
    )
    return link or create_tracking_link(db, shipment, None)


def create_tracking_link(db: Session, shipment: Shipment, ttl_hours: int | None) -> TrackingLink:
    ttl = ttl_hours or settings.tracking_link_ttl_hours
    link = TrackingLink(token=secrets.token_urlsafe(16), shipment_id=shipment.id,
                        expires_at=utcnow() + timedelta(hours=ttl))
    db.add(link)
    db.flush()
    return link


def transition(db: Session, trip: Trip, new_status: str, note: str | None = None, lat=None, lon=None) -> Trip:
    if new_status not in ALLOWED.get(trip.status, set()):
        raise HTTPException(409, f"Invalid transition {trip.status} -> {new_status}")
    shipment = db.get(Shipment, trip.shipment_id)
    now = utcnow()

    if new_status == S.STARTED:
        if trip.driver_id is None:
            raise HTTPException(409, "Assign a driver before starting the trip")
        other = active_trip_for_driver(db, trip.driver_id)
        if other and other.id != trip.id:
            raise HTTPException(409, f"Driver is already on trip {other.id}")
        if trip.status == S.ASSIGNED:  # starting straight away implies the goods were picked up
            _event(db, trip, S.PICKED_UP, "Implied by trip start", lat, lon)
        trip.started_at = now
        if trip.expected_delivery_at is None and trip.planned_duration_s:
            trip.expected_delivery_at = now + timedelta(seconds=trip.planned_duration_s * 1.2)
        last = db.get(DriverLastLocation, trip.driver_id)
        if last:
            trip.last_lat, trip.last_lon = last.lat, last.lon
        link = ensure_tracking_link(db, shipment)
        msg = f"Your shipment {shipment.id} is on the way. Track live: {notify.tracking_url(link.token)}"
        if shipment.require_otp:
            msg += f" Share delivery OTP {shipment.delivery_otp} with the driver only at delivery."
        notify.notify_customer(db, shipment, msg, trip.id)
        notify.notify_ops(db, f"Trip {trip.id} started", trip.id)
    elif new_status == S.REACHED:
        notify.notify_customer(db, shipment, f"Your shipment {shipment.id} has reached the destination.", trip.id)
    elif new_status in S.TERMINAL:
        trip.ended_at = now
        alerts.resolve_alerts(db, trip.id)
        if new_status == S.DELIVERED:
            notify.notify_customer(db, shipment, f"Your shipment {shipment.id} has been delivered.", trip.id)
            notify.notify_ops(db, f"Trip {trip.id} delivered", trip.id)
        else:
            trip.cancel_reason = note
            notify.notify_customer(db, shipment, f"Your shipment {shipment.id} trip was cancelled.", trip.id)

    trip.status = new_status
    shipment.status = new_status
    _event(db, trip, new_status, note, lat, lon)
    return trip


def deliver(db: Session, trip: Trip, receiver_name: str, receiver_phone: str | None, otp: str | None,
            photo: bytes | None, photo_name: str | None, notes: str | None, lat=None, lon=None) -> Trip:
    if trip.status not in (S.STARTED, S.IN_TRANSIT, S.REACHED):
        raise HTTPException(409, f"Cannot deliver a trip in status {trip.status}")
    shipment = db.get(Shipment, trip.shipment_id)
    otp_ok = bool(otp) and secrets.compare_digest(otp.strip(), shipment.delivery_otp)
    if shipment.require_otp and not otp_ok:
        raise HTTPException(400, "Valid delivery OTP is required")
    if otp and not otp_ok:
        raise HTTPException(400, "Delivery OTP does not match")

    photo_path = None
    if photo:
        os.makedirs(settings.upload_dir, exist_ok=True)
        ext = os.path.splitext(photo_name or "")[1].lower()
        ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic") else ".jpg"
        photo_path = os.path.join(settings.upload_dir, f"pod_{trip.id}{ext}")
        with open(photo_path, "wb") as f:
            f.write(photo)

    if trip.status != S.REACHED:
        transition(db, trip, S.REACHED, "Marked at delivery", lat, lon)
    db.merge(ProofOfDelivery(trip_id=trip.id, receiver_name=receiver_name, receiver_phone=receiver_phone,
                             photo_path=photo_path, otp_verified=otp_ok, notes=notes, lat=lat, lon=lon))
    transition(db, trip, S.DELIVERED, f"Received by {receiver_name}", lat, lon)
    db.commit()
    return trip


def on_ride_points(db: Session, trip_id: str) -> list[LocationPoint]:
    return list(db.scalars(select(LocationPoint).where(LocationPoint.trip_id == trip_id).order_by(LocationPoint.ts)))


def compute_eta(db: Session, trip: Trip, last: DriverLastLocation | None) -> dict | None:
    if trip.status not in S.ON_RIDE or last is None:
        return None
    if trip.status == S.REACHED:
        return {"eta_dt": utcnow(), "eta": iso(utcnow()), "remaining_km": 0.0, "speed_kmph": 0.0}
    shipment = db.get(Shipment, trip.shipment_id)
    remaining_m = haversine_m(last.lat, last.lon, shipment.drop_lat, shipment.drop_lon) * settings.road_factor
    speeds = list(db.scalars(
        select(LocationPoint.speed)
        .where(LocationPoint.trip_id == trip.id, LocationPoint.speed.is_not(None))
        .order_by(LocationPoint.ts.desc()).limit(10)
    ))
    avg_kmph = (sum(speeds) / len(speeds)) * 3.6 if speeds else 0.0
    speed_kmph = avg_kmph if avg_kmph >= 5 else settings.default_speed_kmph
    eta_dt = utcnow() + timedelta(seconds=remaining_m / (speed_kmph / 3.6))
    return {"eta_dt": eta_dt, "eta": iso(eta_dt), "remaining_km": round(remaining_m / 1000, 2),
            "speed_kmph": round(speed_kmph, 1)}
