"""Endpoints used by the driver web app (app/static/driver)."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_driver
from ..db import get_db
from ..models import Driver, Trip, TripStatus
from ..schemas import TripStatusIn
from ..services import trips

router = APIRouter(prefix="/driver", tags=["driver app"])

MAX_PHOTO_BYTES = 8 * 1024 * 1024


def _own_trip(db: Session, driver: Driver, trip_id: str) -> Trip:
    trip = trips.get_trip(db, trip_id)
    if trip.driver_id != driver.id:
        raise HTTPException(403, "Trip is not assigned to you")
    return trip


@router.get("/me")
def me(driver: Driver = Depends(require_driver), db: Session = Depends(get_db)):
    return ser.driver(db, driver)


@router.get("/trips")
def my_trips(driver: Driver = Depends(require_driver), db: Session = Depends(get_db)):
    rows = db.scalars(select(Trip).where(Trip.driver_id == driver.id, Trip.status.in_(TripStatus.OPEN))
                      .order_by(Trip.created_at))
    return [ser.trip(db, t, include_otp=False) for t in rows]


@router.post("/trips/{trip_id}/status")
def set_status(trip_id: str, body: TripStatusIn, driver: Driver = Depends(require_driver),
               db: Session = Depends(get_db)):
    if body.status not in trips.MANUAL_STATUSES:
        raise HTTPException(400, f"Drivers can set: {sorted(trips.MANUAL_STATUSES)}")
    trip = _own_trip(db, driver, trip_id)
    trips.transition(db, trip, body.status, body.note or "By driver", body.lat, body.lon)
    db.commit()
    return ser.trip(db, trip, include_otp=False)


@router.post("/trips/{trip_id}/pod")
async def submit_pod(trip_id: str, receiver_name: str = Form(...), receiver_phone: str | None = Form(None),
                     otp: str | None = Form(None), notes: str | None = Form(None),
                     lat: float | None = Form(None), lon: float | None = Form(None),
                     photo: UploadFile | None = File(None),
                     driver: Driver = Depends(require_driver), db: Session = Depends(get_db)):
    trip = _own_trip(db, driver, trip_id)
    data = await photo.read(MAX_PHOTO_BYTES + 1) if photo else None
    if data and len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo larger than 8 MB")
    trips.deliver(db, trip, receiver_name, receiver_phone, otp, data or None, photo.filename if photo else None,
                  notes, lat, lon)
    return ser.trip(db, trip, detail=True, include_otp=False)
