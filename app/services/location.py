"""Driver location ingestion, ported from nammayatri location-tracking-service.

Mirrors `update_driver_location` in crates/location_tracking_service/src/domain/action/ui/location.rs:
  * accepts a batch of points (the driver app buffers while offline and flushes on reconnect)
  * warns when the batch is bigger than LOCATION_BATCH_SIZE, sorts by device timestamp
  * drops points not newer than the driver's last known location (duplicate / out-of-order retries)
  * while on ride, points with accuracy worse than MIN_LOCATION_ACCURACY_M are stored but ignored
    for distance (LTS `get_filtered_driver_locations`)
  * the newest point becomes the driver's last known location
Redis/Kafka from LTS are replaced by the `driver_last_locations` and `location_points` tables.

Points come from two sources (`LocationSource`): the driver phone app (GPS, ~10 m) and consent-based
SIM tracking (telecom network location, ~300 m - 3 km). SIM points use wider radii for the automatic
In Transit / Reached transitions and for route deviation, and only add to trip distance when the truck
moved further than the fix's accuracy, so cell-tower jitter does not inflate kilometres.
"""

import logging

from sqlalchemy.orm import Session

from ..config import settings
from ..geo import distance_to_polyline_m, haversine_m
from ..models import (
    AlertKind, DriverLastLocation, Driver, LocationPoint, LocationSource, Shipment, Trip, TripStatus,
)
from ..schemas import UpdateDriverLocationRequest
from ..timeutil import to_naive_utc, utcnow
from . import alerts, trips

log = logging.getLogger(__name__)
S = TripStatus


def _max_accuracy(source: str) -> float:
    return settings.sim_max_accuracy_m if source == LocationSource.SIM else settings.min_location_accuracy_m


def update_driver_location(db: Session, driver: Driver, batch: list[UpdateDriverLocationRequest],
                           source: str = LocationSource.APP) -> dict:
    if not batch:
        return {"accepted": 0, "dropped": 0}
    if len(batch) > settings.batch_size:
        log.warning("Way points more than %s points => %s points", settings.batch_size, len(batch))

    points = sorted(batch, key=lambda p: to_naive_utc(p.ts))
    last = db.get(DriverLastLocation, driver.id)
    if last is not None:
        points = [p for p in points if to_naive_utc(p.ts) > last.ts]
    dropped = len(batch) - len(points)
    if not points:
        return {"accepted": 0, "dropped": dropped}

    trip = trips.active_trip_for_driver(db, driver.id)
    now = utcnow()
    for p in points:
        accurate = p.acc is None or p.acc <= _max_accuracy(source)
        db.add(LocationPoint(
            driver_id=driver.id, trip_id=trip.id if trip else None, lat=p.pt.lat, lon=p.pt.lon,
            ts=to_naive_utc(p.ts), acc=p.acc, speed=p.v, bearing=p.bear, accurate=accurate, source=source,
            received_at=now,
        ))
        if trip and accurate:
            _on_ride_point(db, trip, p.pt.lat, p.pt.lon, source, p.acc or 0.0)

    newest = points[-1]
    if last is None:
        last = DriverLastLocation(driver_id=driver.id)
        db.add(last)
    last.lat, last.lon, last.ts = newest.pt.lat, newest.pt.lon, to_naive_utc(newest.ts)
    last.acc, last.speed, last.bearing = newest.acc, newest.v, newest.bear
    last.received_at, last.trip_id, last.source = now, trip.id if trip else None, source

    if trip:
        alerts.resolve_alerts(db, trip.id, AlertKind.LOCATION_STALE)
    db.commit()
    return {"accepted": len(points), "dropped": dropped, "tripId": trip.id if trip else None}


def _on_ride_point(db: Session, trip: Trip, lat: float, lon: float, source: str, acc: float) -> None:
    sim = source == LocationSource.SIM
    if trip.last_lat is None:
        trip.last_lat, trip.last_lon = lat, lon
    else:
        moved = haversine_m(trip.last_lat, trip.last_lon, lat, lon)
        # A SIM fix that lands inside its own error circle is noise, not movement.
        if not sim or moved > acc:
            trip.distance_m += moved
            trip.last_lat, trip.last_lon = lat, lon

    in_transit_m = settings.sim_in_transit_distance_m if sim else settings.in_transit_distance_m
    reached_m = max(settings.reached_radius_m, settings.sim_reached_radius_m) if sim else settings.reached_radius_m
    shipment = db.get(Shipment, trip.shipment_id)
    if trip.status == S.STARTED and \
            haversine_m(lat, lon, shipment.pickup_lat, shipment.pickup_lon) > in_transit_m:
        trips.transition(db, trip, S.IN_TRANSIT, "Auto: left pickup area", lat, lon)
    if trip.status in (S.STARTED, S.IN_TRANSIT) and \
            haversine_m(lat, lon, shipment.drop_lat, shipment.drop_lon) <= reached_m:
        trips.transition(db, trip, S.REACHED, "Auto: entered destination area", lat, lon)

    if trip.status in (S.STARTED, S.IN_TRANSIT) and trip.planned_route:
        threshold = settings.deviation_threshold_m if trip.route_source == "osrm" \
            else settings.deviation_threshold_straight_line_m
        if sim:
            threshold += acc
        off_by = distance_to_polyline_m(lat, lon, trip.planned_route)
        if off_by > threshold:
            alerts.open_alert(db, trip, AlertKind.ROUTE_DEVIATION,
                              f"Truck is {off_by / 1000:.1f} km away from the planned route", lat, lon)
        else:
            alerts.resolve_alerts(db, trip.id, AlertKind.ROUTE_DEVIATION)
