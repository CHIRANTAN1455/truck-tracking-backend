import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ["DEV_MODE"] = "true"
os.environ["OSRM_URL"] = ""
os.environ["ALERT_CHECK_INTERVAL_SEC"] = "3600"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin(client):
    token = client.post("/auth/admin/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    return {"token": token}
