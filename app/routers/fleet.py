from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..config import settings
from ..db import get_db
from ..models import Company, Driver, DriverLastLocation, LocationPoint, SimConsent, SimStatus, Trip, TripStatus, Vehicle
from ..schemas import CompanyIn, CompanyUpdate, DriverIn, DriverUpdate, VehicleIn, VehicleUpdate
from ..services import sim
from ..timeutil import to_naive_utc

router = APIRouter(tags=["fleet"], dependencies=[Depends(require_admin)])


def _commit_unique(db: Session, what: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{what} already exists")


def _check_company(db: Session, company_id: int | None) -> None:
    if company_id is not None and db.get(Company, company_id) is None:
        raise HTTPException(404, "Company not found")


def _check_vehicle_free(db: Session, vehicle_id: int, driver_id: int | None, company_id: int | None) -> None:
    v = db.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Vehicle not found")
    if v.company_id and company_id and v.company_id != company_id:
        raise HTTPException(409, "Vehicle belongs to a different company")
    holder = db.scalar(select(Driver).where(Driver.vehicle_id == vehicle_id))
    if holder and holder.id != driver_id:
        raise HTTPException(409, f"Vehicle is already assigned to {holder.name}")


# ---------------------------------------------------------------- companies

def _company_counts(db: Session, company_id: int) -> dict:
    drivers = select(Driver.id).where(Driver.company_id == company_id)
    return {
        "drivers": db.scalar(select(func.count()).select_from(Driver).where(Driver.company_id == company_id)),
        "vehicles": db.scalar(select(func.count()).select_from(Vehicle).where(Vehicle.company_id == company_id)),
        "on_trip": db.scalar(select(func.count(func.distinct(Trip.driver_id)))
                             .where(Trip.driver_id.in_(drivers), Trip.status.in_(TripStatus.ON_RIDE))),
        "sim_active": db.scalar(select(func.count()).select_from(SimConsent)
                                .where(SimConsent.driver_id.in_(drivers), SimConsent.status == SimStatus.ACTIVE)),
    }


@router.get("/companies")
def list_companies(db: Session = Depends(get_db)):
    return [ser.company(c, _company_counts(db, c.id)) for c in db.scalars(select(Company).order_by(Company.name))]


@router.post("/companies")
def create_company(body: CompanyIn, db: Session = Depends(get_db)):
    c = Company(**body.model_dump())
    db.add(c)
    _commit_unique(db, "Company with this code or name")
    return ser.company(c, _company_counts(db, c.id))


@router.patch("/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate, db: Session = Depends(get_db)):
    c = db.get(Company, company_id)
    if c is None:
        raise HTTPException(404, "Company not found")
    for k, val in body.model_dump(exclude_none=True).items():
        setattr(c, k, val)
    _commit_unique(db, "Company with this name")
    return ser.company(c, _company_counts(db, c.id))


# ---------------------------------------------------------------- vehicles & drivers

@router.get("/vehicles")
def list_vehicles(company_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Vehicle).order_by(Vehicle.reg_no)
    if company_id:
        q = q.where(Vehicle.company_id == company_id)
    return [ser.vehicle(v) for v in db.scalars(q)]


@router.post("/vehicles")
def create_vehicle(body: VehicleIn, db: Session = Depends(get_db)):
    _check_company(db, body.company_id)
    v = Vehicle(**body.model_dump())
    db.add(v)
    _commit_unique(db, "Vehicle with this registration number")
    return ser.vehicle(v)


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, body: VehicleUpdate, db: Session = Depends(get_db)):
    v = db.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Vehicle not found")
    changes = body.model_dump(exclude_none=True)
    if "company_id" in changes:
        _check_company(db, changes["company_id"])
    for k, val in changes.items():
        setattr(v, k, val)
    db.commit()
    return ser.vehicle(v)


@router.get("/drivers")
def list_drivers(company_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Driver).order_by(Driver.name)
    if company_id:
        q = q.where(Driver.company_id == company_id)
    return [ser.driver(db, d) for d in db.scalars(q)]


@router.post("/drivers")
def create_driver(body: DriverIn, db: Session = Depends(get_db)):
    _check_company(db, body.company_id)
    if body.vehicle_id is not None:
        _check_vehicle_free(db, body.vehicle_id, None, body.company_id)
    d = Driver(**body.model_dump())
    db.add(d)
    _commit_unique(db, "Driver with this phone number")
    if settings.sim_auto_consent:
        sim.request_consent(db, d)  # onboarding sends the consent SMS straight away
        db.commit()
    return ser.driver(db, d)


@router.patch("/drivers/{driver_id}")
def update_driver(driver_id: int, body: DriverUpdate, db: Session = Depends(get_db)):
    d = db.get(Driver, driver_id)
    if d is None:
        raise HTTPException(404, "Driver not found")
    changes = body.model_dump(exclude_none=True)
    if "company_id" in changes:
        _check_company(db, changes["company_id"])
    company_id = changes.get("company_id", d.company_id)
    vehicle_id = changes.pop("vehicle_id", None)
    if vehicle_id == -1:
        d.vehicle_id = None
    elif vehicle_id is not None:
        _check_vehicle_free(db, vehicle_id, d.id, company_id)
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
def fleet_live(company_id: int | None = None, db: Session = Depends(get_db)):
    """Every active driver with company, last location, SIM tracking state and current trip (live map)."""
    companies = {c.id: ser.company_ref(c) for c in db.scalars(select(Company))}
    q = select(Driver).where(Driver.active.is_(True)).order_by(Driver.name)
    if company_id:
        q = q.where(Driver.company_id == company_id)
    out = []
    for d in db.scalars(q):
        last = db.get(DriverLastLocation, d.id)
        trip = db.scalar(select(Trip).where(Trip.driver_id == d.id, Trip.status.in_(TripStatus.OPEN))
                         .order_by(Trip.started_at.desc().nulls_last(), Trip.created_at.desc()))
        consent = db.get(SimConsent, d.id)
        interval = settings.sim_ping_on_trip_min if trip else settings.sim_ping_idle_min
        interval = interval if interval > 0 else settings.sim_ping_on_trip_min  # idle pings switched off
        out.append({
            "driver": {"id": d.id, "name": d.name, "phone": d.phone},
            "company": companies.get(d.company_id),
            "vehicle": ser.vehicle(db.get(Vehicle, d.vehicle_id)) if d.vehicle_id else None,
            "last_location": ser.last_location(last),
            "sim": ser.sim(consent, interval) if consent else None,
            # Seconds between expected updates, so the map can judge freshness per tracking method.
            "expected_interval_sec": int(interval * 60) if last and last.source == "sim" else 30,
            "trip": {"id": trip.id, "status": trip.status, "shipment_id": trip.shipment_id} if trip else None,
        })
    return out
