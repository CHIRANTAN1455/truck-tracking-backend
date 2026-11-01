from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..db import get_db
from ..models import Driver, DriverLastLocation, LocationPoint, Trip, TripStatus, Vehicle
from ..schemas import DriverIn, DriverUpdate, VehicleIn, VehicleUpdate
from ..timeutil import to_naive_utc

router = APIRouter(tags=["fleet"], dependencies=[Depends(require_admin)])


def _commit_unique(db: Session, what: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{what} already exists")


def _check_vehicle_free(db: Session, vehicle_id: int, driver_id: int | None) -> None:
    if db.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Vehicle not found")
    holder = db.scalar(select(Driver).where(Driver.vehicle_id == vehicle_id))
    if holder and holder.id != driver_id:
        raise HTTPException(409, f"Vehicle is already assigned to {holder.name}")


@router.get("/vehicles")
def list_vehicles(db: Session = Depends(get_db)):
    return [ser.vehicle(v) for v in db.scalars(select(Vehicle).order_by(Vehicle.reg_no))]


@router.post("/vehicles")
def create_vehicle(body: VehicleIn, db: Session = Depends(get_db)):
    v = Vehicle(**body.model_dump())
    db.add(v)
    _commit_unique(db, "Vehicle with this registration number")
    return ser.vehicle(v)


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, body: VehicleUpdate, db: Session = Depends(get_db)):
    v = db.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Vehicle not found")
    for k, val in body.model_dump(exclude_none=True).items():
        setattr(v, k, val)
    db.commit()
    return ser.vehicle(v)


@router.get("/drivers")
def list_drivers(db: Session = Depends(get_db)):
    return [ser.driver(db, d) for d in db.scalars(select(Driver).order_by(Driver.name))]


@router.post("/drivers")
def create_driver(body: DriverIn, db: Session = Depends(get_db)):
    if body.vehicle_id is not None:
        _check_vehicle_free(db, body.vehicle_id, None)
    d = Driver(**body.model_dump())
    db.add(d)
    _commit_unique(db, "Driver with this phone number")
    return ser.driver(db, d)


@router.patch("/drivers/{driver_id}")
def update_driver(driver_id: int, body: DriverUpdate, db: Session = Depends(get_db)):
    d = db.get(Driver, driver_id)
    if d is None:
        raise HTTPException(404, "Driver not found")
    changes = body.model_dump(exclude_none=True)
    vehicle_id = changes.pop("vehicle_id", None)
    if vehicle_id == -1:
        d.vehicle_id = None
    elif vehicle_id is not None:
        _check_vehicle_free(db, vehicle_id, d.id)
        d.vehicle_id = vehicle_id
    for k, val in changes.items():
        setattr(d, k, val)
    db.commit()
    return ser.driver(db, d)


@router.get("/drivers/{driver_id}/locations")
def driver_location_history(driver_id: int, start: datetime | None = None, end: datetime | None = None,
                            limit: int = 5000, db: Session = Depends(get_db)):
    q = select(LocationPoint).where(LocationPoint.driver_id == driver_id)
    if start:
        q = q.where(LocationPoint.ts >= to_naive_utc(start))
    if end:
        q = q.where(LocationPoint.ts <= to_naive_utc(end))
    rows = list(db.scalars(q.order_by(LocationPoint.ts.desc()).limit(min(limit, 20000))))
    return [ser.point(p) for p in reversed(rows)]


@router.get("/fleet/live")
def fleet_live(db: Session = Depends(get_db)):
    """Every active driver with last location, vehicle and current trip (for the live map)."""
    out = []
    for d in db.scalars(select(Driver).where(Driver.active.is_(True)).order_by(Driver.name)):
        last = db.get(DriverLastLocation, d.id)
        trip = db.scalar(select(Trip).where(Trip.driver_id == d.id, Trip.status.in_(TripStatus.OPEN))
                         .order_by(Trip.started_at.desc().nulls_last(), Trip.created_at.desc()))
        out.append({
            "driver": {"id": d.id, "name": d.name, "phone": d.phone},
            "vehicle": ser.vehicle(db.get(Vehicle, d.vehicle_id)) if d.vehicle_id else None,
            "last_location": ser.last_location(last),
            "trip": {"id": trip.id, "status": trip.status, "shipment_id": trip.shipment_id} if trip else None,
        })
    return out
