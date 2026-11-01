import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import auth, driver_app, fleet, lts, ops, public, shipments, trips_admin
from .services import alerts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tracking")


async def _alert_loop() -> None:
    while True:
        await asyncio.sleep(settings.alert_check_interval_sec)
        try:
            await asyncio.to_thread(_run_checks)
        except Exception:
            log.exception("Periodic alert check failed")


def _run_checks() -> None:
    with SessionLocal() as db:
        alerts.run_periodic_checks(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    task = asyncio.create_task(_alert_loop())
    yield
    task.cancel()


app = FastAPI(title="Truck Tracking Backend", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","), allow_methods=["*"],
                   allow_headers=["*"])

for r in (lts, auth, driver_app, fleet, shipments, trips_admin, ops, public):
    app.include_router(r.router)

app.mount("/driver-app", StaticFiles(directory=Path(__file__).parent / "static" / "driver", html=True),
          name="driver-app")


@app.get("/healthcheck", tags=["health"])
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")
