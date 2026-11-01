"""Location-tracking-service compatible endpoints (same paths and payloads as nammayatri LTS).

A driver app written against LTS can talk to this backend unchanged, and these routes can later be
pointed at a real LTS deployment if the fleet outgrows a single Python service.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_driver
from ..db import get_db
from ..models import Driver, DriverLastLocation, TripStatus
from ..schemas import RideEndRequest, RideStartRequest, UpdateDriverLocationRequest
from ..serializers import point
from ..services import location, trips
from ..timeutil import iso

router = APIRouter(tags=["location-tracking (LTS compatible)"])


@router.post("/ui/driver/location")
def update_driver_location(batch: list[UpdateDriverLocationRequest], driver: Driver = Depends(require_driver),
                           db: Session = Depends(get_db)):
    """Batch of GPS points from the driver app (header `token`)."""
    return location.update_driver_location(db, driver, batch)


@router.get("/ui/driver/location/{ride_id}", dependencies=[Depends(require_admin)])
def driver_location_for_ride(ride_id: str, db: Session = Depends(get_db)):
    trip = trips.get_trip(db, ride_id)
    last = db.get(DriverLastLocation, trip.driver_id) if trip.driver_id else None
    if last is None:
        raise HTTPException(404, "Driver location not found")
    return {"currPoint": {"lat": last.lat, "lon": last.lon}, "lastUpdate": iso(last.ts)}


@router.post("/internal/ride/{ride_id}/start", dependencies=[Depends(require_admin)])
def ride_start(ride_id: str, body: RideStartRequest, db: Session = Depends(get_db)):
    trip = trips.get_trip(db, ride_id)
    if trip.driver_id != body.driver_id:
        raise HTTPException(400, "driverId does not match the trip")
    trips.transition(db, trip, TripStatus.STARTED)
    db.commit()
    return {"result": "Success"}


@router.post("/internal/ride/{ride_id}/end", dependencies=[Depends(require_admin)])
def ride_end(ride_id: str, body: RideEndRequest, db: Session = Depends(get_db)):
    """Ends on-ride tracking and returns the trip's way points, like LTS `RideEndResponse`.

    This does not mark the shipment delivered; use the POD endpoint for that.
    """
    trip = trips.get_trip(db, ride_id)
    if trip.driver_id != body.driver_id:
        raise HTTPException(400, "driverId does not match the trip")
    return {"rideId": trip.id, "driverId": trip.driver_id,
            "loc": [{"pt": {"lat": p.lat, "lon": p.lon}, "ts": iso(p.ts), "acc": p.acc}
                    for p in trips.on_ride_points(db, trip.id) if p.accurate],
            "distanceKm": round(trip.distance_m / 1000, 3)}


@router.get("/internal/ride/{ride_id}/driver/locations", dependencies=[Depends(require_admin)])
def ride_driver_locations(ride_id: str, db: Session = Depends(get_db)):
    trips.get_trip(db, ride_id)
    return [point(p) for p in trips.on_ride_points(db, ride_id)]


@router.get("/internal/drivers/location", dependencies=[Depends(require_admin)])
def drivers_location(driver_ids: list[int] = Query(alias="driverIds"), db: Session = Depends(get_db)):
    rows = db.scalars(select(DriverLastLocation).where(DriverLastLocation.driver_id.in_(driver_ids)))
    return [{"driverId": r.driver_id, "lat": r.lat, "lon": r.lon, "coordinatesCalculatedAt": iso(r.ts),
             "updatedAt": iso(r.received_at)} for r in rows]
