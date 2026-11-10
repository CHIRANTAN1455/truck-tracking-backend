"""Request bodies. Responses are plain dicts built in app/serializers.py."""

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from .phone import normalize_in_mobile


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


def _soft_phone(v: str) -> str:
    try:
        return normalize_in_mobile(v)
    except ValueError:
        return v  # not a valid mobile -> no driver matches, endpoint answers as before


class OtpRequestIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)

    _norm = field_validator("phone")(_soft_phone)


class OtpVerifyIn(BaseModel):
    phone: str
    otp: str

    _norm = field_validator("phone")(_soft_phone)


class CompanyIn(BaseModel):
    code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Z0-9-]+$")
    name: str = Field(min_length=2, max_length=120)
    gstin: str | None = Field(default=None, pattern=r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    hub_city: str | None = None
    hub_lat: float | None = Field(default=None, ge=-90, le=90)
    hub_lon: float | None = Field(default=None, ge=-180, le=180)


class CompanyUpdate(BaseModel):
    name: str | None = None
    gstin: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    hub_city: str | None = None
    hub_lat: float | None = None
    hub_lon: float | None = None
    active: bool | None = None


class VehicleIn(BaseModel):
    reg_no: str = Field(min_length=2, max_length=32)
    vehicle_type: str = "truck"
    capacity_kg: float | None = None
    company_id: int | None = None
    active: bool = True


class VehicleUpdate(BaseModel):
    vehicle_type: str | None = None
    capacity_kg: float | None = None
    company_id: int | None = None
    active: bool | None = None


class DriverIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=6, max_length=20, description="Indian mobile number; tracked via its SIM")
    license_no: str | None = None
    company_id: int | None = None
    vehicle_id: int | None = None

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_in_mobile(v)


class DriverUpdate(BaseModel):
    name: str | None = None
    license_no: str | None = None
    company_id: int | None = None
    active: bool | None = None
    # -1 un-assigns the vehicle; None leaves it unchanged.
    vehicle_id: int | None = None


class SimWebhookEvent(BaseModel):
    """One event pushed by the telecom location aggregator (see README "SIM webhook contract")."""

    type: Literal["location", "consent"]
    msisdn: str
    event_id: str | None = None
    # location
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    ts: datetime | None = None
    operator: str | None = None
    cell_id: str | None = None
    # consent
    status: str | None = None
    error: str | None = None


class SimWebhookIn(BaseModel):
    events: list[SimWebhookEvent] = Field(max_length=1000)


class SimMockReply(BaseModel):
    phone: str
    reply: str = "Y"


class SimBulkConsent(BaseModel):
    company_id: int | None = None


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
