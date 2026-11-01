from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import utcnow


class TripStatus:
    CREATED = "CREATED"  # no driver yet
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    STARTED = "STARTED"
    IN_TRANSIT = "IN_TRANSIT"
    REACHED = "REACHED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"

    # Statuses during which the driver is "on ride" in LTS terms and points belong to the trip.
    ON_RIDE = (STARTED, IN_TRANSIT, REACHED)
    OPEN = (CREATED, ASSIGNED, PICKED_UP, STARTED, IN_TRANSIT, REACHED)
    TERMINAL = (DELIVERED, CANCELLED)


class AlertKind:
    ROUTE_DEVIATION = "ROUTE_DEVIATION"
    DELAY = "DELAY"
    LOCATION_STALE = "LOCATION_STALE"


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(primary_key=True)
    reg_no: Mapped[str] = mapped_column(String(32), unique=True)
    vehicle_type: Mapped[str] = mapped_column(String(32), default="truck")
    capacity_kg: Mapped[float | None]
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Driver(Base):
    __tablename__ = "drivers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    license_no: Mapped[str | None] = mapped_column(String(50))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # admin | driver
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    expires_at: Mapped[datetime]


class OtpRequest(Base):
    __tablename__ = "otp_requests"
    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    otp_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    reference: Mapped[str | None] = mapped_column(String(64))
    customer_name: Mapped[str] = mapped_column(String(100))
    customer_phone: Mapped[str | None] = mapped_column(String(20))
    customer_email: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    weight_kg: Mapped[float | None]
    pickup_address: Mapped[str] = mapped_column(Text)
    pickup_lat: Mapped[float]
    pickup_lon: Mapped[float]
    drop_address: Mapped[str] = mapped_column(Text)
    drop_lat: Mapped[float]
    drop_lon: Mapped[float]
    require_otp: Mapped[bool] = mapped_column(default=False)
    delivery_otp: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20), default="BOOKED")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"))
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"))
    status: Mapped[str] = mapped_column(String(20), default=TripStatus.CREATED)
    planned_route: Mapped[list | None] = mapped_column(JSON)  # [[lat, lon], ...]
    route_source: Mapped[str] = mapped_column(String(16), default="straight_line")
    planned_distance_m: Mapped[float | None]
    planned_duration_s: Mapped[float | None]
    expected_delivery_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    distance_m: Mapped[float] = mapped_column(default=0.0)
    # Last accepted on-ride point, used to accumulate distance incrementally.
    last_lat: Mapped[float | None]
    last_lon: Mapped[float | None]
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TripEvent(Base):
    __tablename__ = "trip_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None]
    lon: Mapped[float | None]


class LocationPoint(Base):
    __tablename__ = "location_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.id"))
    lat: Mapped[float]
    lon: Mapped[float]
    ts: Mapped[datetime]  # device time of the GPS fix
    acc: Mapped[float | None]
    speed: Mapped[float | None]  # m/s
    bearing: Mapped[float | None]
    # False when accuracy was worse than MIN_LOCATION_ACCURACY_M (kept for history, ignored for distance).
    accurate: Mapped[bool] = mapped_column(default=True)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        Index("ix_location_driver_ts", "driver_id", "ts"),
        Index("ix_location_trip_ts", "trip_id", "ts"),
    )


class DriverLastLocation(Base):
    __tablename__ = "driver_last_locations"
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"), primary_key=True)
    lat: Mapped[float]
    lon: Mapped[float]
    ts: Mapped[datetime]
    acc: Mapped[float | None]
    speed: Mapped[float | None]
    bearing: Mapped[float | None]
    received_at: Mapped[datetime]
    trip_id: Mapped[str | None] = mapped_column(String(20))


class TrackingLink(Base):
    __tablename__ = "tracking_links"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class ProofOfDelivery(Base):
    __tablename__ = "proof_of_delivery"
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id"), primary_key=True)
    receiver_name: Mapped[str] = mapped_column(String(100))
    receiver_phone: Mapped[str | None] = mapped_column(String(20))
    photo_path: Mapped[str | None] = mapped_column(Text)
    otp_verified: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    delivered_at: Mapped[datetime] = mapped_column(default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))  # sms | whatsapp | email | ops
    recipient: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # sent | logged | failed
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
