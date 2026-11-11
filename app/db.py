from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


def _url(url: str) -> str:
    # Hosted Postgres hands out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    _url(settings.database_url),
    connect_args={"check_same_thread": False} if _sqlite
    # Neon's pooled endpoint (PgBouncer) and psycopg's automatic prepared statements don't mix.
    else {"prepare_threshold": None} if "-pooler." in settings.database_url else {},
    # Free Postgres tiers close idle connections / suspend; check each connection before use.
    pool_pre_ping=not _sqlite,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
