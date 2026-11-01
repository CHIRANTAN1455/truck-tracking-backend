"""Request bodies. Responses are plain dicts built in app/serializers.py."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class UpdateDriverLocationRequest(BaseModel):
    """Identical to LTS `UpdateDriverLocationRequest` (crates/.../types/ui/location.rs)."""

    pt: Point
    ts: datetime
    acc: float | None = None
    v: float | None = None  # speed, m/s
    bear: float | None = None


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RideStartRequest(_Camel):
    """LTS `RideStartRequest`. merchant_id is accepted for compatibility and ignored."""

    merchant_id: str | None = None
    driver_id: int


class RideEndRequest(_Camel):
    """LTS `RideEndRequest`."""

    lat: float
    lon: float
    ts: int | None = None
    driver_id: int
    merchant_id: str | None = None


class AdminLogin(BaseModel):
    username: str
    password: str


class OtpRequestIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)


class OtpVerifyIn(BaseModel):
    phone: str
    otp: str


class VehicleIn(BaseModel):
    reg_no: str = Field(min_length=2, max_length=32)
    vehicle_type: str = "truck"
    capacity_kg: float | None = None
    active: bool = True


class VehicleUpdate(BaseModel):
    vehicle_type: str | None = None
    capacity_kg: float | None = None
    active: bool | None = None


class DriverIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=6, max_length=20)
    license_no: str | None = None
    vehicle_id: int | None = None


class DriverUpdate(BaseModel):
    name: str | None = None
    license_no: str | None = None
    active: bool | None = None
    # -1 un-assigns the vehicle; None leaves it unchanged.
    vehicle_id: int | None = None


class ShipmentIn(BaseModel):
    reference: str | None = None
    customer_name: str
    customer_phone: str | None = None
    customer_email: str | None = None
    description: str | None = None
    weight_kg: float | None = None
    pickup_address: str
    pickup_lat: float = Field(ge=-90, le=90)
    pickup_lon: float = Field(ge=-180, le=180)
    drop_address: str
    drop_lat: float = Field(ge=-90, le=90)
    drop_lon: float = Field(ge=-180, le=180)
    require_otp: bool = False


class TripIn(BaseModel):
    shipment_id: str
    driver_id: int | None = None
    vehicle_id: int | None = None
    expected_delivery_at: datetime | None = None


class TripAssign(BaseModel):
    driver_id: int
    vehicle_id: int | None = None


class TripStatusIn(BaseModel):
    status: str
    note: str | None = None
    lat: float | None = None
    lon: float | None = None


class TripCancel(BaseModel):
    reason: str = "Cancelled by operator"


class TrackingLinkIn(BaseModel):
    ttl_hours: int | None = Field(default=None, ge=1, le=24 * 90)
