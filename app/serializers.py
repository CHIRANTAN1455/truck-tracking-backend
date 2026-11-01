from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Alert, Driver, DriverLastLocation, LocationPoint, Notification, ProofOfDelivery, Shipment,
    TrackingLink, Trip, TripEvent, Vehicle,
)
from .services import notify
from .timeutil import iso, utcnow


def vehicle(v: Vehicle | None) -> dict | None:
    if v is None:
        return None
    return {"id": v.id, "reg_no": v.reg_no, "vehicle_type": v.vehicle_type, "capacity_kg": v.capacity_kg,
            "active": v.active, "created_at": iso(v.created_at)}


def last_location(loc: DriverLastLocation | None) -> dict | None:
    if loc is None:
        return None
    age = (utcnow() - loc.received_at).total_seconds()
    return {"lat": loc.lat, "lon": loc.lon, "ts": iso(loc.ts), "received_at": iso(loc.received_at),
            "acc": loc.acc, "speed_kmph": round(loc.speed * 3.6, 1) if loc.speed is not None else None,
            "bearing": loc.bearing, "age_sec": int(age), "trip_id": loc.trip_id}


def driver(db: Session, d: Driver) -> dict:
    return {"id": d.id, "name": d.name, "phone": d.phone, "license_no": d.license_no, "active": d.active,
            "vehicle": vehicle(db.get(Vehicle, d.vehicle_id)) if d.vehicle_id else None,
            "last_location": last_location(db.get(DriverLastLocation, d.id)), "created_at": iso(d.created_at)}


def shipment(s: Shipment, include_otp: bool = True) -> dict:
    data = {"id": s.id, "reference": s.reference, "customer_name": s.customer_name,
            "customer_phone": s.customer_phone, "customer_email": s.customer_email,
            "description": s.description, "weight_kg": s.weight_kg,
            "pickup": {"address": s.pickup_address, "lat": s.pickup_lat, "lon": s.pickup_lon},
            "drop": {"address": s.drop_address, "lat": s.drop_lat, "lon": s.drop_lon},
            "require_otp": s.require_otp, "status": s.status, "created_at": iso(s.created_at)}
    if include_otp:
        data["delivery_otp"] = s.delivery_otp
    return data


def event(e: TripEvent) -> dict:
    return {"status": e.status, "ts": iso(e.ts), "note": e.note, "lat": e.lat, "lon": e.lon}


def point(p: LocationPoint) -> dict:
    return {"lat": p.lat, "lon": p.lon, "ts": iso(p.ts), "acc": p.acc,
            "speed_kmph": round(p.speed * 3.6, 1) if p.speed is not None else None,
            "bearing": p.bearing, "accurate": p.accurate, "trip_id": p.trip_id}


def pod(p: ProofOfDelivery | None) -> dict | None:
    if p is None:
        return None
    return {"receiver_name": p.receiver_name, "receiver_phone": p.receiver_phone, "has_photo": bool(p.photo_path),
            "otp_verified": p.otp_verified, "notes": p.notes, "lat": p.lat, "lon": p.lon,
            "delivered_at": iso(p.delivered_at)}


def alert(a: Alert) -> dict:
    return {"id": a.id, "trip_id": a.trip_id, "kind": a.kind, "message": a.message, "lat": a.lat, "lon": a.lon,
            "created_at": iso(a.created_at), "resolved_at": iso(a.resolved_at)}


def notification(n: Notification) -> dict:
    return {"id": n.id, "trip_id": n.trip_id, "channel": n.channel, "recipient": n.recipient,
            "message": n.message, "status": n.status, "created_at": iso(n.created_at)}


def tracking_link(link: TrackingLink, s: Shipment) -> dict:
    now = utcnow()
    state = "revoked" if link.revoked_at else ("expired" if link.expires_at <= now else "active")
    return {"token": link.token, "shipment_id": link.shipment_id, "state": state,
            "created_at": iso(link.created_at), "expires_at": iso(link.expires_at),
            "revoked_at": iso(link.revoked_at),
            "share": notify.share_links(link.token, s.customer_phone, s.customer_email, s.id)}


def trip(db: Session, t: Trip, detail: bool = False, include_otp: bool = True) -> dict:
    from .services.trips import compute_eta

    s = db.get(Shipment, t.shipment_id)
    d = db.get(Driver, t.driver_id) if t.driver_id else None
    last = db.get(DriverLastLocation, t.driver_id) if t.driver_id else None
    end = t.ended_at or utcnow()
    duration = (end - t.started_at).total_seconds() if t.started_at else None
    eta = compute_eta(db, t, last)
    if eta:
        eta = {k: v for k, v in eta.items() if k != "eta_dt"}
    data = {
        "id": t.id, "status": t.status, "shipment": shipment(s, include_otp),
        "driver": {"id": d.id, "name": d.name, "phone": d.phone} if d else None,
        "vehicle": vehicle(db.get(Vehicle, t.vehicle_id)) if t.vehicle_id else None,
        "expected_delivery_at": iso(t.expected_delivery_at), "started_at": iso(t.started_at),
        "ended_at": iso(t.ended_at), "distance_km": round(t.distance_m / 1000, 2),
        "duration_min": round(duration / 60, 1) if duration is not None else None,
        "planned_distance_km": round(t.planned_distance_m / 1000, 2) if t.planned_distance_m else None,
        "route_source": t.route_source, "cancel_reason": t.cancel_reason, "created_at": iso(t.created_at),
        "last_location": last_location(last) if last and last.trip_id == t.id else None,
        "eta": eta,
        "open_alerts": len(list(db.scalars(select(Alert.id).where(Alert.trip_id == t.id,
                                                                  Alert.resolved_at.is_(None))))),
    }
    if detail:
        data["planned_route"] = t.planned_route
        data["events"] = [event(e) for e in db.scalars(
            select(TripEvent).where(TripEvent.trip_id == t.id).order_by(TripEvent.ts, TripEvent.id))]
        data["pod"] = pod(db.get(ProofOfDelivery, t.id))
        data["alerts"] = [alert(a) for a in db.scalars(
            select(Alert).where(Alert.trip_id == t.id).order_by(Alert.created_at.desc()))]
    return data
