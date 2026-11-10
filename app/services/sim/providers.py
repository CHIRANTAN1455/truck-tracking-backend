"""Telecom location providers.

In India a SIM cannot be located directly by a fleet platform. Licensed aggregators (who have the
agreements with Jio, Airtel, Vi and BSNL) send the driver a consent SMS, and once the driver replies
they return the SIM's network location (cell tower / LBS) on request or push it to a webhook.

Every aggregator has its own API, so the rest of the backend only talks to this interface:

    request_consent(db, consent) -> ProviderResult    send the consent SMS
    consent_status(db, consent)  -> ProviderResult    poll a PENDING consent (if the provider has no webhook)
    locate(db, consent)          -> SimFix | None     current network location; None = SIM not reachable
    revoke(db, consent)          -> None              stop tracking at the provider

`MockProvider` simulates an aggregator for development and demos. `AggregatorProvider` is the template
to fill in for the provider you sign with: map their endpoints and fields onto these four methods.
"""

import hashlib
import json
import logging
import math
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Company, Driver, Shipment, SimConsent, SimStatus, Trip, TripStatus
from ...timeutil import to_naive_utc, utcnow

log = logging.getLogger(__name__)


class SimProviderError(Exception):
    pass


@dataclass
class SimFix:
    lat: float
    lon: float
    accuracy_m: float
    ts: datetime  # naive UTC, time the network computed the fix
    operator: str | None = None
    cell_id: str | None = None


@dataclass
class ProviderResult:
    status: str  # a SimStatus value
    provider_ref: str | None = None
    operator: str | None = None
    error: str | None = None


class SimProvider:
    name = "base"

    def request_consent(self, db: Session, consent: SimConsent) -> ProviderResult:
        raise NotImplementedError

    def consent_status(self, db: Session, consent: SimConsent) -> ProviderResult:
        return ProviderResult(status=consent.status, provider_ref=consent.provider_ref, operator=consent.operator)

    def locate(self, db: Session, consent: SimConsent) -> SimFix | None:
        raise NotImplementedError

    def revoke(self, db: Session, consent: SimConsent) -> None:
        return None


# --------------------------------------------------------------------------------------------------
# Mock: behaves like an aggregator so the whole flow (consent -> pings -> live map -> alerts) can be
# demoed without a contract. Consent stays PENDING until /integrations/sim/mock/reply says "Y".
# --------------------------------------------------------------------------------------------------

_OPERATORS = ("JIO", "AIRTEL", "VI", "BSNL")
_OPERATOR_WEIGHTS = (45, 35, 15, 5)  # rough Indian subscriber share


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def _point_along(route: list, fraction: float) -> tuple[float, float]:
    if len(route) == 1:
        return route[0][0], route[0][1]
    seg = [math.dist(route[i], route[i + 1]) for i in range(len(route) - 1)]
    target = sum(seg) * max(0.0, min(1.0, fraction))
    for i, length in enumerate(seg):
        if target <= length or i == len(seg) - 1:
            f = 0.0 if length == 0 else min(1.0, target / length)
            a, b = route[i], route[i + 1]
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        target -= length
    return route[-1][0], route[-1][1]


class MockProvider(SimProvider):
    name = "mock"

    def request_consent(self, db, consent):
        r = random.Random(_seed("op", consent.msisdn))
        operator = r.choices(_OPERATORS, _OPERATOR_WEIGHTS)[0]
        return ProviderResult(status=SimStatus.PENDING, provider_ref=f"MOCK-{consent.msisdn[-6:]}", operator=operator)

    def locate(self, db, consent):
        return self.fix_at(db, db.get(Driver, consent.driver_id), utcnow(), consent.operator)

    def fix_at(self, db: Session, driver: Driver, at: datetime, operator: str | None = None) -> SimFix | None:
        """Simulated network fix for `driver` at time `at` (also used by the seed to backfill trails)."""
        # About 1 in 25 phones went "switched off / out of coverage" 45 min ago -> no fix (stale alert demo).
        if _seed("offline", driver.id) % 25 == 0 and at > utcnow() - timedelta(minutes=45):
            return None
        r = random.Random(_seed("fix", driver.id, at.replace(second=0, microsecond=0)))
        # Urban towers are dense (~300-800 m), highway / rural cells are wide (1-3 km).
        acc = r.choice((r.uniform(300, 800), r.uniform(800, 3000)))

        trip = db.scalar(select(Trip).where(Trip.driver_id == driver.id, Trip.status.in_(TripStatus.OPEN))
                         .order_by(Trip.created_at.desc()))
        if trip and trip.status in TripStatus.ON_RIDE and trip.started_at and trip.planned_route:
            pace = 0.8 + (_seed("pace", driver.id) % 40) / 100  # each driver 0.8x - 1.2x of planned speed
            elapsed = max(0.0, (at - trip.started_at).total_seconds())
            fraction = elapsed * pace / max(trip.planned_duration_s or 1, 1)
            lat, lon = _point_along(trip.planned_route, fraction)
            # One driver in ~30 takes a long detour in the middle of the trip (route deviation demo).
            if _seed("detour", driver.id) % 30 == 0 and 0.35 < fraction < 0.65:
                lat += 0.12
        elif trip:
            s = db.get(Shipment, trip.shipment_id)
            lat, lon = s.pickup_lat, s.pickup_lon
        else:
            company = db.get(Company, driver.company_id) if driver.company_id else None
            base_lat = company.hub_lat if company and company.hub_lat else 12.9716
            base_lon = company.hub_lon if company and company.hub_lon else 77.5946
            spread = random.Random(_seed("park", driver.id))
            lat, lon = base_lat + spread.uniform(-0.08, 0.08), base_lon + spread.uniform(-0.08, 0.08)

        # Network fix: true position plus an error inside the accuracy circle.
        angle, dist = r.uniform(0, 2 * math.pi), r.uniform(0, 0.6) * acc
        lat += (dist * math.cos(angle)) / 111_320
        lon += (dist * math.sin(angle)) / (111_320 * math.cos(math.radians(lat)))
        return SimFix(lat=round(lat, 6), lon=round(lon, 6), accuracy_m=round(acc), ts=at - timedelta(seconds=20),
                      operator=operator, cell_id=f"404-{r.randint(10, 99)}-{r.randint(1000, 9999)}")


# --------------------------------------------------------------------------------------------------
# Real aggregator: template. Fill in the four calls with your provider's documented API.
# --------------------------------------------------------------------------------------------------

class AggregatorProvider(SimProvider):
    """Generic REST adapter. Endpoint paths and field names below are placeholders: replace them with the
    ones in your aggregator's API document (most expose: register/consent, consent status, locate, deregister).
    """

    name = "aggregator"

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not settings.sim_api_base_url or not settings.sim_api_key:
            raise SimProviderError("SIM_API_BASE_URL and SIM_API_KEY must be set for the aggregator provider")
        req = urllib.request.Request(
            settings.sim_api_base_url.rstrip("/") + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {settings.sim_api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SimProviderError(f"{method} {path} failed: {exc}") from exc

    def request_consent(self, db, consent):
        data = self._call("POST", "/consents", {"msisdn": consent.msisdn})  # TODO: provider path / fields
        return ProviderResult(status=map_status(data.get("status")), provider_ref=data.get("id"),
                              operator=data.get("operator"))

    def consent_status(self, db, consent):
        data = self._call("GET", f"/consents/{consent.provider_ref or consent.msisdn}")  # TODO
        return ProviderResult(status=map_status(data.get("status")), provider_ref=data.get("id"),
                              operator=data.get("operator"))

    def locate(self, db, consent):
        data = self._call("GET", f"/locations/{consent.msisdn}")  # TODO
        if not data or data.get("lat") is None:
            return None
        return SimFix(lat=float(data["lat"]), lon=float(data["lon"]), accuracy_m=float(data.get("accuracy", 1000)),
                      ts=to_naive_utc(datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))),
                      operator=data.get("operator"), cell_id=data.get("cell_id"))

    def revoke(self, db, consent):
        self._call("DELETE", f"/consents/{consent.provider_ref or consent.msisdn}")  # TODO


def map_status(raw: str | None) -> str:
    raw = (raw or "").upper()
    if raw in ("ACTIVE", "APPROVED", "CONSENTED", "ALLOWED", "Y"):
        return SimStatus.ACTIVE
    if raw in ("REVOKED", "DENIED", "REJECTED", "N", "OPTED_OUT"):
        return SimStatus.REVOKED
    if raw in ("EXPIRED", "TIMEOUT"):
        return SimStatus.EXPIRED
    if raw in ("FAILED", "ERROR", "UNSUPPORTED", "INVALID"):
        return SimStatus.FAILED
    return SimStatus.PENDING


_PROVIDERS: dict[str, type[SimProvider]] = {"mock": MockProvider, "aggregator": AggregatorProvider}


def get_provider() -> SimProvider:
    cls = _PROVIDERS.get(settings.sim_provider)
    if cls is None:
        raise SimProviderError(f"Unknown SIM_PROVIDER {settings.sim_provider!r}; known: {sorted(_PROVIDERS)}")
    return cls()
