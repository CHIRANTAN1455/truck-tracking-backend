import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serializers as ser
from ..auth import require_admin
from ..db import get_db
from ..models import Shipment, TrackingLink, Trip
from ..schemas import ShipmentIn, TrackingLinkIn
from ..services import trips
from ..timeutil import utcnow

router = APIRouter(tags=["shipments"], dependencies=[Depends(require_admin)])


def _get(db: Session, shipment_id: str) -> Shipment:
    s = db.get(Shipment, shipment_id)
    if s is None:
        raise HTTPException(404, "Shipment not found")
    return s


@router.get("/shipments")
def list_shipments(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Shipment).order_by(Shipment.created_at.desc())
    if status:
        q = q.where(Shipment.status == status)
    return [ser.shipment(s) for s in db.scalars(q)]


@router.post("/shipments")
def create_shipment(body: ShipmentIn, db: Session = Depends(get_db)):
    s = Shipment(id=trips.new_id("SHP"), delivery_otp=f"{secrets.randbelow(10_000):04d}", **body.model_dump())
    db.add(s)
    db.commit()
    return ser.shipment(s)


@router.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str, db: Session = Depends(get_db)):
    s = _get(db, shipment_id)
    data = ser.shipment(s)
    data["trips"] = [{"id": t.id, "status": t.status} for t in
                     db.scalars(select(Trip).where(Trip.shipment_id == s.id).order_by(Trip.created_at))]
    data["tracking_links"] = [ser.tracking_link(link, s) for link in db.scalars(
        select(TrackingLink).where(TrackingLink.shipment_id == s.id).order_by(TrackingLink.created_at.desc()))]
    return data


@router.post("/shipments/{shipment_id}/tracking-links")
def create_tracking_link(shipment_id: str, body: TrackingLinkIn, db: Session = Depends(get_db)):
    s = _get(db, shipment_id)
    link = trips.create_tracking_link(db, s, body.ttl_hours)
    db.commit()
    return ser.tracking_link(link, s)


@router.post("/tracking-links/{link_token}/revoke")
def revoke_tracking_link(link_token: str, db: Session = Depends(get_db)):
    link = db.get(TrackingLink, link_token)
    if link is None:
        raise HTTPException(404, "Tracking link not found")
    link.revoked_at = link.revoked_at or utcnow()
    db.commit()
    return ser.tracking_link(link, db.get(Shipment, link.shipment_id))
