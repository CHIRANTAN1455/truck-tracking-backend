"""Runtime settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_files() -> None:
    """Read KEY=VALUE lines from .env.local (written by `neon init`) and .env in the repo root.

    Real environment variables always win, so hosting platforms and tests are not affected.
    """
    root = Path(__file__).resolve().parent.parent
    for name in (".env.local", ".env"):
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


_load_env_files()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: str) -> bool:
    return _env(name, default).lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./tracking.db"))
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "admin123"))
    # DEV_MODE returns the driver OTP in the API response instead of needing an SMS provider.
    dev_mode: bool = field(default_factory=lambda: _bool("DEV_MODE", "true"))
    session_ttl_hours: int = field(default_factory=lambda: int(_env("SESSION_TTL_HOURS", "720")))
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "*"))

    # Same knobs as nammayatri location-tracking-service (dhall-configs: min_location_accuracy, batch_size)
    min_location_accuracy_m: float = field(default_factory=lambda: float(_env("MIN_LOCATION_ACCURACY_M", "50")))
    batch_size: int = field(default_factory=lambda: int(_env("LOCATION_BATCH_SIZE", "100")))

    # Trip automation
    in_transit_distance_m: float = field(default_factory=lambda: float(_env("IN_TRANSIT_DISTANCE_M", "300")))
    reached_radius_m: float = field(default_factory=lambda: float(_env("REACHED_RADIUS_M", "300")))

    # Alerts
    deviation_threshold_m: float = field(default_factory=lambda: float(_env("DEVIATION_THRESHOLD_M", "1000")))
    # Used when no road route is available and the planned route is a straight pickup->drop line.
    deviation_threshold_straight_line_m: float = field(
        default_factory=lambda: float(_env("DEVIATION_THRESHOLD_STRAIGHT_LINE_M", "5000"))
    )
    stale_location_minutes: float = field(default_factory=lambda: float(_env("STALE_LOCATION_MINUTES", "10")))
    delay_grace_minutes: float = field(default_factory=lambda: float(_env("DELAY_GRACE_MINUTES", "15")))
    alert_check_interval_sec: float = field(default_factory=lambda: float(_env("ALERT_CHECK_INTERVAL_SEC", "60")))

    # ETA
    default_speed_kmph: float = field(default_factory=lambda: float(_env("DEFAULT_SPEED_KMPH", "40")))
    road_factor: float = field(default_factory=lambda: float(_env("ROAD_FACTOR", "1.3")))
    # Optional OSRM server (e.g. nammayatri/osrm-backend). Empty = straight-line fallback.
    osrm_url: str = field(default_factory=lambda: _env("OSRM_URL", ""))

    # Customer tracking links
    tracking_link_ttl_hours: int = field(default_factory=lambda: int(_env("TRACKING_LINK_TTL_HOURS", "168")))
    public_tracking_base_url: str = field(
        default_factory=lambda: _env("PUBLIC_TRACKING_BASE_URL", "http://localhost:8501")
    )

    # Consent-based SIM tracking (telecom network location of the driver's SIM).
    # SIM_PROVIDER: "mock" simulates the telecom aggregator for development; set to your aggregator's
    # adapter name in production (see app/services/sim/providers.py).
    sim_provider: str = field(default_factory=lambda: _env("SIM_PROVIDER", "mock"))
    sim_api_base_url: str = field(default_factory=lambda: _env("SIM_API_BASE_URL", ""))
    sim_api_key: str = field(default_factory=lambda: _env("SIM_API_KEY", ""))
    # Shared secret for the HMAC-SHA256 signature on POST /integrations/sim/webhook. Empty = webhook disabled.
    sim_webhook_secret: str = field(default_factory=lambda: _env("SIM_WEBHOOK_SECRET", ""))
    # Send the consent SMS automatically when a driver is created.
    sim_auto_consent: bool = field(default_factory=lambda: _bool("SIM_AUTO_CONSENT", "true"))
    # Each ping is billed by the aggregator, so drivers on a trip are located more often than idle ones.
    sim_ping_on_trip_min: float = field(default_factory=lambda: float(_env("SIM_PING_ON_TRIP_MIN", "10")))
    sim_ping_idle_min: float = field(default_factory=lambda: float(_env("SIM_PING_IDLE_MIN", "60")))  # 0 = off
    sim_poll_tick_sec: float = field(default_factory=lambda: float(_env("SIM_POLL_TICK_SEC", "30")))
    sim_pending_consent_hours: float = field(default_factory=lambda: float(_env("SIM_PENDING_CONSENT_HOURS", "48")))
    sim_consent_validity_days: float = field(default_factory=lambda: float(_env("SIM_CONSENT_VALIDITY_DAYS", "180")))
    # Network fixes are coarse (cell tower), so trip automation uses wider radii for SIM points.
    sim_max_accuracy_m: float = field(default_factory=lambda: float(_env("SIM_MAX_ACCURACY_M", "5000")))
    sim_in_transit_distance_m: float = field(default_factory=lambda: float(_env("SIM_IN_TRANSIT_DISTANCE_M", "3000")))
    sim_reached_radius_m: float = field(default_factory=lambda: float(_env("SIM_REACHED_RADIUS_M", "2000")))
    # A SIM-tracked trip is "stale" after this many missed pings.
    sim_stale_after_pings: float = field(default_factory=lambda: float(_env("SIM_STALE_AFTER_PINGS", "2.5")))

    # Load the demo companies / drivers / trips on startup when the database is empty (free demo deploys).
    seed_demo: bool = field(default_factory=lambda: _bool("SEED_DEMO", "false"))

    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", "./uploads"))

    # Optional email delivery. Without SMTP_HOST notifications are only logged.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "tracking@example.com"))


settings = Settings()
