from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import DriverLastLocation
from app.services import alerts

PICKUP = (13.0285, 77.5197)
DROP = (13.3409, 77.1010)


def _setup(client, admin, require_otp=True):
    v = client.post("/vehicles", json={"reg_no": "KA01AB1234"}, headers=admin).json()
    d = client.post("/drivers", json={"name": "Ravi", "phone": "9000000001", "license_no": "L1",
                                      "vehicle_id": v["id"]}, headers=admin).json()
    s = client.post("/shipments", headers=admin, json={
        "customer_name": "Acme", "customer_phone": "9800000001", "customer_email": "a@x.example",
        "pickup_address": "Peenya", "pickup_lat": PICKUP[0], "pickup_lon": PICKUP[1],
        "drop_address": "Tumakuru", "drop_lat": DROP[0], "drop_lon": DROP[1], "require_otp": require_otp,
    }).json()
    t = client.post("/trips", json={"shipment_id": s["id"], "driver_id": d["id"]}, headers=admin).json()
    otp = client.post("/auth/driver/otp/request", json={"phone": "9000000001"}).json()["dev_otp"]
    drv = {"token": client.post("/auth/driver/otp/verify",
                                json={"phone": "9000000001", "otp": otp}).json()["token"]}
    return v, d, s, t, drv


def _pt(lat, lon, ts, acc=10.0, v=12.0):
    return {"pt": {"lat": lat, "lon": lon}, "ts": ts.isoformat(), "acc": acc, "v": v, "bear": 300}


def _interp(f):
    return PICKUP[0] + (DROP[0] - PICKUP[0]) * f, PICKUP[1] + (DROP[1] - PICKUP[1]) * f


def test_full_trip_lifecycle(client, admin):
    v, d, s, t, drv = _setup(client, admin)
    assert t["status"] == "ASSIGNED" and t["vehicle"]["reg_no"] == "KA01AB1234"

    # Driver must not see the delivery OTP.
    mine = client.get("/driver/trips", headers=drv).json()
    assert "delivery_otp" not in mine[0]["shipment"]

    now = datetime.now(timezone.utc)
    client.post("/ui/driver/location", json=[_pt(*PICKUP, now)], headers=drv)
    assert client.post(f"/driver/trips/{t['id']}/status", json={"status": "PICKED_UP"}, headers=drv).status_code == 200
    r = client.post(f"/driver/trips/{t['id']}/status", json={"status": "STARTED"}, headers=drv)
    assert r.status_code == 200, r.text

    # Tracking link auto-created on start and customer notified.
    shp = client.get(f"/shipments/{s['id']}", headers=admin).json()
    token = shp["tracking_links"][0]["token"]
    assert "wa.me/9800000001" in shp["tracking_links"][0]["share"]["whatsapp"]
    notes = client.get("/notifications", params={"trip_id": t["id"]}, headers=admin).json()
    assert any(n["channel"] == "email" and token in n["message"] for n in notes)

    # Offline buffering: a batch sent late, out of order, with one bad-accuracy point and one duplicate.
    batch = [_pt(*_interp(i / 10), now + timedelta(seconds=10 * i)) for i in range(1, 6)]
    batch[2]["acc"] = 500
    r = client.post("/ui/driver/location", json=list(reversed(batch)), headers=drv).json()
    assert r == {"accepted": 5, "dropped": 0, "tripId": t["id"]}
    r = client.post("/ui/driver/location", json=[batch[-1]], headers=drv).json()
    assert r["accepted"] == 0 and r["dropped"] == 1

    detail = client.get(f"/trips/{t['id']}", headers=admin).json()
    assert detail["status"] == "IN_TRANSIT"
    assert 10 < detail["distance_km"] < 30
    assert detail["eta"]["remaining_km"] > 0

    pts = client.get(f"/trips/{t['id']}/locations", headers=admin).json()
    assert len(pts) == 5 and [p["accurate"] for p in pts].count(False) == 1
    assert pts == sorted(pts, key=lambda p: p["ts"])

    # Public page while live.
    pub = client.get(f"/public/track/{token}").json()
    assert pub["live_tracking"] and pub["location"] and pub["eta"]
    assert [x["done"] for x in pub["timeline"]] == [True, True, True, False, False]

    # Arrive -> REACHED automatically.
    client.post("/ui/driver/location", json=[_pt(*DROP, now + timedelta(minutes=30))], headers=drv)
    assert client.get(f"/trips/{t['id']}", headers=admin).json()["status"] == "REACHED"

    # POD: wrong / missing OTP rejected, correct accepted with photo.
    form = {"receiver_name": "Store Manager"}
    assert client.post(f"/driver/trips/{t['id']}/pod", data=form, headers=drv).status_code == 400
    assert client.post(f"/driver/trips/{t['id']}/pod", data={**form, "otp": "xxxx"}, headers=drv).status_code == 400
    r = client.post(f"/driver/trips/{t['id']}/pod", data={**form, "otp": s["delivery_otp"]},
                    files={"photo": ("pod.jpg", b"\xff\xd8fakejpeg", "image/jpeg")}, headers=drv)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "DELIVERED" and r.json()["pod"]["otp_verified"]
    assert client.get(f"/trips/{t['id']}/pod/photo", headers=admin).content == b"\xff\xd8fakejpeg"

    # Live tracking stops after delivery; timeline complete.
    pub = client.get(f"/public/track/{token}").json()
    assert not pub["live_tracking"] and pub["location"] is None and pub["delivered_at"]
    assert all(x["done"] for x in pub["timeline"])

    rep = client.get("/reports/trips", headers=admin).json()
    assert rep["summary"]["by_status"] == {"DELIVERED": 1}
    assert rep["trips"][0]["distance_km"] > 30

    # Revocation.
    client.post(f"/tracking-links/{token}/revoke", headers=admin)
    assert client.get(f"/public/track/{token}").status_code == 410


def test_alerts_deviation_stale_delay(client, admin):
    v, d, s, t, drv = _setup(client, admin, require_otp=False)
    now = datetime.now(timezone.utc)
    client.post(f"/trips/{t['id']}/status", json={"status": "STARTED"}, headers=admin)

    # 20 km north of the straight-line route -> deviation alert (once), then resolves when back.
    off = _interp(0.3)
    for i in range(2):
        client.post("/ui/driver/location", json=[_pt(off[0] + 0.18, off[1], now + timedelta(seconds=i + 1))],
                    headers=drv)
    open_alerts = client.get("/alerts", headers=admin).json()
    assert [a["kind"] for a in open_alerts] == ["ROUTE_DEVIATION"]
    client.post("/ui/driver/location", json=[_pt(*_interp(0.4), now + timedelta(seconds=5))], headers=drv)
    assert client.get("/alerts", headers=admin).json() == []

    # Stale location + delay.
    with SessionLocal() as db:
        last = db.get(DriverLastLocation, d["id"])
        last.received_at -= timedelta(hours=1)
        from app.models import Trip
        db.get(Trip, t["id"]).expected_delivery_at = last.received_at
        db.commit()
        alerts.run_periodic_checks(db)
        alerts.run_periodic_checks(db)
    kinds = sorted(a["kind"] for a in client.get("/alerts", headers=admin).json())
    assert kinds == ["DELAY", "LOCATION_STALE"]

    # A new ping resolves the stale alert.
    client.post("/ui/driver/location", json=[_pt(*_interp(0.5), now + timedelta(seconds=9))], headers=drv)
    kinds = [a["kind"] for a in client.get("/alerts", headers=admin).json()]
    assert kinds == ["DELAY"]

    # Cancel closes everything.
    client.post(f"/trips/{t['id']}/cancel", json={"reason": "Breakdown"}, headers=admin)
    assert client.get("/alerts", headers=admin).json() == []
    assert client.get("/trips", params={"group": "cancelled"}, headers=admin).json()[0]["cancel_reason"] == "Breakdown"


def test_lts_compat_and_rules(client, admin):
    v, d, s, t, drv = _setup(client, admin)
    assert client.post("/ui/driver/location", json=[]).status_code == 401
    assert client.post("/ui/driver/location", json=[], headers=drv).json()["accepted"] == 0
    r = client.post(f"/internal/ride/{t['id']}/start", json={"merchantId": "m1", "driverId": d["id"]}, headers=admin)
    assert r.json() == {"result": "Success"}
    now = datetime.now(timezone.utc)
    client.post("/ui/driver/location", json=[_pt(*PICKUP, now), _pt(*_interp(0.1), now + timedelta(seconds=5))],
                headers=drv)
    loc = client.get(f"/ui/driver/location/{t['id']}", headers=admin).json()
    assert set(loc) == {"currPoint", "lastUpdate"}
    end = client.post(f"/internal/ride/{t['id']}/end",
                      json={"lat": 1, "lon": 2, "driverId": d["id"], "merchantId": "m1"}, headers=admin).json()
    assert end["rideId"] == t["id"] and len(end["loc"]) == 2
    assert client.get("/internal/drivers/location", params={"driverIds": d["id"]}, headers=admin).json()[0]["driverId"] == d["id"]

    # Driver can't start a second trip; vehicle can't be double-assigned; drivers can't call admin APIs.
    assert client.get("/trips", headers=drv).status_code == 403
    d2 = client.post("/drivers", json={"name": "B", "phone": "9000000002", "vehicle_id": v["id"]}, headers=admin)
    assert d2.status_code == 409
    assert client.post(f"/driver/trips/{t['id']}/status", json={"status": "DELIVERED"}, headers=drv).status_code == 400
