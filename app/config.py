"""Runtime settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field


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

    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", "./uploads"))

    # Optional email delivery. Without SMTP_HOST notifications are only logged.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "tracking@example.com"))


settings = Settings()
