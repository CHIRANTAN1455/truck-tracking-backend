from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..db import get_db
from ..models import ProofOfDelivery, Trip, TripStatus
from ..schemas import TripAssign, TripCancel, TripIn, TripStatusIn
from ..services import trips
from .driver_app import MAX_PHOTO_BYTES

router = APIRouter(prefix="/trips", tags=["trips"], dependencies=[Depends(require_admin)])

STATUS_GROUPS = {
    "active": TripStatus.OPEN,
    "completed": (TripStatus.DELIVERED,),
    "cancelled": (TripStatus.CANCELLED,),
}


@router.get("")
def list_trips(group: str | None = None, status: str | None = None, driver_id: int | None = None,
               db: Session = Depends(get_db)):
    q = select(Trip).order_by(Trip.created_at.desc())
    if group:
        if group not in STATUS_GROUPS:
            raise HTTPException(400, f"group must be one of {list(STATUS_GROUPS)}")
        q = q.where(Trip.status.in_(STATUS_GROUPS[group]))
    if status:
        q = q.where(Trip.status == status)
    if driver_id:
        q = q.where(Trip.driver_id == driver_id)
    return [ser.trip(db, t) for t in db.scalars(q)]


@router.post("")
def create_trip(body: TripIn, db: Session = Depends(get_db)):
    trip = trips.create_trip(db, body.shipment_id, body.driver_id, body.vehicle_id, body.expected_delivery_at)
    return ser.trip(db, trip, detail=True)


@router.get("/{trip_id}")
def get_trip(trip_id: str, db: Session = Depends(get_db)):
    return ser.trip(db, trips.get_trip(db, trip_id), detail=True)


@router.post("/{trip_id}/assign")
def assign(trip_id: str, body: TripAssign, db: Session = Depends(get_db)):
    trip = trips.assign(db, trips.get_trip(db, trip_id), body.driver_id, body.vehicle_id)
    db.commit()
    return ser.trip(db, trip, detail=True)


@router.post("/{trip_id}/status")
def set_status(trip_id: str, body: TripStatusIn, db: Session = Depends(get_db)):
    if body.status not in trips.MANUAL_STATUSES:
        raise HTTPException(400, f"Use /cancel or /pod for {body.status}; settable: {sorted(trips.MANUAL_STATUSES)}")
    trip = trips.transition(db, trips.get_trip(db, trip_id), body.status, body.note or "By operator",
                            body.lat, body.lon)
    db.commit()
    return ser.trip(db, trip, detail=True)


@router.post("/{trip_id}/cancel")
def cancel(trip_id: str, body: TripCancel, db: Session = Depends(get_db)):
    trip = trips.transition(db, trips.get_trip(db, trip_id), TripStatus.CANCELLED, body.reason)
    db.commit()
    return ser.trip(db, trip, detail=True)


@router.post("/{trip_id}/pod")
async def record_pod(trip_id: str, receiver_name: str = Form(...), receiver_phone: str | None = Form(None),
                     otp: str | None = Form(None), notes: str | None = Form(None),
                     photo: UploadFile | None = File(None), db: Session = Depends(get_db)):
    data = await photo.read(MAX_PHOTO_BYTES + 1) if photo else None
    if data and len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo larger than 8 MB")
    trip = trips.deliver(db, trips.get_trip(db, trip_id), receiver_name, receiver_phone, otp, data or None,
                         photo.filename if photo else None, notes)
    return ser.trip(db, trip, detail=True)


@router.get("/{trip_id}/pod/photo")
def pod_photo(trip_id: str, db: Session = Depends(get_db)):
    pod = db.get(ProofOfDelivery, trip_id)
    if pod is None or not pod.photo_path:
        raise HTTPException(404, "No POD photo")
    return FileResponse(pod.photo_path)


@router.get("/{trip_id}/locations")
def trip_locations(trip_id: str, db: Session = Depends(get_db)):
    trips.get_trip(db, trip_id)
    return [ser.point(p) for p in trips.on_ride_points(db, trip_id)]
