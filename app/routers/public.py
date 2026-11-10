"""Public customer tracking API: no login, access is via the unguessable link token."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DriverLastLocation, Shipment, TrackingLink, Trip, TripEvent, TripStatus, Vehicle
from ..services import trips
from ..timeutil import iso, utcnow

router = APIRouter(prefix="/public", tags=["public tracking"])

S = TripStatus
# Customer-facing timeline, in order.
TIMELINE = [
    (S.PICKED_UP, "Shipment Picked Up"),
    (S.STARTED, "Trip Started"),
    (S.IN_TRANSIT, "In Transit"),
    (S.REACHED, "Reached Destination"),
    (S.DELIVERED, "Delivered"),
]


@router.get("/track/{token}")
def track(token: str, db: Session = Depends(get_db)):
    link = db.get(TrackingLink, token)
    if link is None:
        raise HTTPException(404, "Tracking link not found")
    if link.revoked_at is not None:
        raise HTTPException(410, "This tracking link has been revoked")
    if link.expires_at <= utcnow():
        raise HTTPException(410, "This tracking link has expired")

    shipment = db.get(Shipment, link.shipment_id)
    trip = db.scalar(select(Trip).where(Trip.shipment_id == shipment.id).order_by(Trip.created_at.desc()))

    reached = {}
    if trip:
        for e in db.scalars(select(TripEvent).where(TripEvent.trip_id == trip.id).order_by(TripEvent.ts)):
            reached.setdefault(e.status, e.ts)
    timeline = [{"status": code, "label": label, "done": code in reached, "ts": iso(reached.get(code))}
                for code, label in TIMELINE]

    # Live location only while the truck is actually on the trip; hidden after delivery/cancel.
    live = None
    eta = None
    if trip and trip.status in S.ON_RIDE and trip.driver_id:
        last = db.get(DriverLastLocation, trip.driver_id)
        if last and last.trip_id == trip.id:
            live = {"lat": last.lat, "lon": last.lon, "bearing": last.bearing, "ts": iso(last.ts),
                    "age_sec": int((utcnow() - last.received_at).total_seconds()),
                    # SIM fixes are approximate: the customer page draws this as a circle, not a pin.
                    "source": last.source, "accuracy_m": last.acc}
        eta_data = trips.compute_eta(db, trip, last)
        if eta_data:
            eta = {"eta": eta_data["eta"], "remaining_km": eta_data["remaining_km"]}

    vehicle = db.get(Vehicle, trip.vehicle_id) if trip and trip.vehicle_id else None
    return {
        "shipment": {"id": shipment.id, "reference": shipment.reference, "description": shipment.description,
                     "weight_kg": shipment.weight_kg, "customer_name": shipment.customer_name},
        "status": trip.status if trip else shipment.status,
        "pickup": {"address": shipment.pickup_address, "lat": shipment.pickup_lat, "lon": shipment.pickup_lon},
        "drop": {"address": shipment.drop_address, "lat": shipment.drop_lat, "lon": shipment.drop_lon},
        "vehicle_no": vehicle.reg_no if vehicle else None,
        "live_tracking": live is not None,
        "location": live,
        "last_update": live["ts"] if live else None,
        "eta": eta,
        "expected_delivery_at": iso(trip.expected_delivery_at) if trip else None,
        "delivered_at": iso(reached.get(S.DELIVERED)),
        "timeline": timeline,
        "link_expires_at": iso(link.expires_at),
    }
