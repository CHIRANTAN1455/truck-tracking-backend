import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import DriverLastLocation, SimConsent
from app.services import alerts, sim

PICKUP = (12.9716, 77.5946)  # Bengaluru
DROP = (13.0827, 80.2707)  # Chennai, ~290 km


def _company(client, admin, code="VFC", name="Vayu Freight Carriers"):
    r = client.post("/companies", json={"code": code, "name": name, "hub_city": "Bengaluru",
                                        "hub_lat": PICKUP[0], "hub_lon": PICKUP[1]}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def _driver(client, admin, company_id, phone="+91 98450 00001", reg="KA01AB0001"):
    v = client.post("/vehicles", json={"reg_no": reg, "company_id": company_id}, headers=admin).json()
    r = client.post("/drivers", json={"name": "Ravi", "phone": phone, "company_id": company_id,
                                      "vehicle_id": v["id"]}, headers=admin)
    assert r.status_code == 200, r.text
    return v, r.json()


def _consent(client, admin, phone):
    r = client.post("/integrations/sim/mock/reply", json={"phone": phone, "reply": "Y"}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Signature": sig, "Content-Type": "application/json"}


def _loc(msisdn, lat, lon, ts, acc=1500, eid=None):
    return {"type": "location", "msisdn": msisdn, "lat": lat, "lon": lon, "accuracy_m": acc,
            "ts": ts.isoformat(), "operator": "JIO", "event_id": eid}


def _interp(f):
    return PICKUP[0] + (DROP[0] - PICKUP[0]) * f, PICKUP[1] + (DROP[1] - PICKUP[1]) * f


def test_consent_lifecycle_and_locate(client, admin):
    c = _company(client, admin)
    assert client.post("/drivers", json={"name": "X", "phone": "12345"}, headers=admin).status_code == 422
    v, d = _driver(client, admin, c["id"])
    assert d["phone"] == "9845000001"  # normalised to 10 digits
    assert d["company"]["code"] == "VFC"
    assert d["sim"]["status"] == "PENDING" and d["sim"]["msisdn"] == "919845000001"

    # No consent yet -> cannot locate.
    assert client.post(f"/drivers/{d['id']}/sim/locate", headers=admin).status_code == 409
    row = _consent(client, admin, "9845000001")
    assert row["status"] == "ACTIVE" and row["expires_at"]

    r = client.post(f"/drivers/{d['id']}/sim/locate", headers=admin).json()
    assert r["ok"] and r["accepted"] == 1 and r["fix"]["accuracy_m"] >= 300
    live = client.get("/fleet/live", headers=admin).json()[0]
    assert live["company"]["code"] == "VFC" and live["last_location"]["source"] == "sim"
    assert live["sim"]["status"] == "ACTIVE" and live["expected_interval_sec"] == 3600  # idle driver

    # Revoke -> audit trail, and the provider's stray "Y" must not re-enable it.
    assert client.post(f"/drivers/{d['id']}/sim/revoke", headers=admin).json()["status"] == "REVOKED"
    _consent(client, admin, "9845000001")
    assert client.get("/sim/consents", headers=admin).json()[0]["status"] == "REVOKED"
    kinds = [e["kind"] for e in client.get("/sim/events", params={"driver_id": d["id"]}, headers=admin).json()]
    assert kinds == ["REVOKED", "ACTIVE", "CONSENT_REQUESTED"]
    # Re-request works after revoke.
    assert client.post(f"/drivers/{d['id']}/sim/consent", headers=admin).json()["status"] == "PENDING"
    st = client.get("/sim/status", headers=admin).json()
    assert st["provider"] == "mock" and st["counts"]["PENDING"] == 1


def test_webhook_security_and_consent_gate(client, admin):
    c = _company(client, admin)
    _, d = _driver(client, admin, c["id"])
    now = datetime.now(timezone.utc)
    body = {"events": [_loc("919845000001", *PICKUP, now, eid="e1")]}
    raw, headers = _signed(body)

    assert client.post("/integrations/sim/webhook", content=raw,
                       headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post("/integrations/sim/webhook", content=raw,
                       headers={**headers, "X-Signature": "sha256=00"}).status_code == 401
    # Signed but consent still PENDING -> ignored, nothing stored.
    r = client.post("/integrations/sim/webhook", content=raw, headers=headers).json()
    assert r["results"][0]["result"] == "ignored_consent_pending"

    # Consent pushed by the provider, then location is accepted; a replay is a duplicate.
    raw2, h2 = _signed({"events": [{"type": "consent", "msisdn": "+919845000001", "status": "APPROVED"}]})
    assert client.post("/integrations/sim/webhook", content=raw2, headers=h2).json()["results"][0]["result"] == "ok"
    assert client.post("/integrations/sim/webhook", content=raw, headers=headers).json()["results"][0]["result"] == "accepted"
    assert client.post("/integrations/sim/webhook", content=raw, headers=headers).json()["results"][0]["result"] == "duplicate_or_old"

    # Unknown number and bad number are reported, not errors.
    raw3, h3 = _signed({"events": [_loc("919999999999", *PICKUP, now), _loc("123", *PICKUP, now)]})
    res = [x["result"] for x in client.post("/integrations/sim/webhook", content=raw3, headers=h3).json()["results"]]
    assert res == ["unknown_msisdn", "invalid_msisdn"]

    # Driver opts out via the provider -> later fixes are dropped.
    raw4, h4 = _signed({"events": [{"type": "consent", "msisdn": "919845000001", "status": "OPTED_OUT"},
                                   _loc("919845000001", *_interp(0.1), now + timedelta(minutes=10))]})
    res = [x["result"] for x in client.post("/integrations/sim/webhook", content=raw4, headers=h4).json()["results"]]
    assert res == ["ok", "ignored_consent_revoked"]


def test_sim_trip_automation_uses_coarse_thresholds(client, admin):
    c = _company(client, admin)
    v, d = _driver(client, admin, c["id"])
    _consent(client, admin, "9845000001")
    s = client.post("/shipments", headers=admin, json={
        "customer_name": "Acme", "pickup_address": "Peenya", "pickup_lat": PICKUP[0], "pickup_lon": PICKUP[1],
        "drop_address": "Sriperumbudur", "drop_lat": DROP[0], "drop_lon": DROP[1]}).json()
    t = client.post("/trips", json={"shipment_id": s["id"], "driver_id": d["id"]}, headers=admin).json()
    client.post(f"/trips/{t['id']}/status", json={"status": "STARTED"}, headers=admin)
    now = datetime.now(timezone.utc) - timedelta(hours=6)  # replayed history; future fixes get clamped to now

    def send(lat, lon, minutes, acc=1500):
        raw, h = _signed({"events": [_loc("919845000001", lat, lon, now + timedelta(minutes=minutes), acc)]})
        return client.post("/integrations/sim/webhook", content=raw, headers=h).json()

    # 1.5 km jitter around the depot with 2 km accuracy: no distance, still STARTED (app GPS would flip at 300 m).
    send(*PICKUP, 0)
    send(PICKUP[0] + 0.0135, PICKUP[1], 10, acc=2000)
    tr = client.get(f"/trips/{t['id']}", headers=admin).json()
    assert tr["status"] == "STARTED" and tr["distance_km"] == 0

    send(*_interp(0.2), 60)
    tr = client.get(f"/trips/{t['id']}", headers=admin).json()
    assert tr["status"] == "IN_TRANSIT" and 50 < tr["distance_km"] < 70
    assert tr["last_location"]["source"] == "sim" and tr["last_location"]["speed_kmph"] > 30
    assert client.get("/alerts", headers=admin).json() == []

    # 1.8 km from the drop is "reached" for a SIM fix.
    send(DROP[0] + 0.016, DROP[1], 300, acc=1200)
    assert client.get(f"/trips/{t['id']}", headers=admin).json()["status"] == "REACHED"

    pts = client.get(f"/trips/{t['id']}/locations", headers=admin).json()
    assert all(p["source"] == "sim" and p["accurate"] for p in pts)


def test_stale_threshold_and_scheduler(client, admin):
    c = _company(client, admin)
    v, d = _driver(client, admin, c["id"])
    _consent(client, admin, "9845000001")
    s = client.post("/shipments", headers=admin, json={
        "customer_name": "Acme", "pickup_address": "A", "pickup_lat": PICKUP[0], "pickup_lon": PICKUP[1],
        "drop_address": "B", "drop_lat": DROP[0], "drop_lon": DROP[1]}).json()
    t = client.post("/trips", json={"shipment_id": s["id"], "driver_id": d["id"]}, headers=admin).json()
    client.post(f"/trips/{t['id']}/status", json={"status": "STARTED"}, headers=admin)

    # Scheduler: first tick pings, an immediate second tick does not (on-trip interval is 10 min).
    assert client.post("/sim/poll", headers=admin).json()["pinged"] == 1
    assert client.post("/sim/poll", headers=admin).json()["pinged"] == 0
    with SessionLocal() as db:
        db.get(SimConsent, d["id"]).last_ping_at -= timedelta(minutes=11)
        db.commit()
    assert client.post("/sim/poll", headers=admin).json()["pinged"] == 1

    with SessionLocal() as db:
        last = db.get(DriverLastLocation, d["id"])
        # 15 min without a SIM fix is normal (pings are 10 min apart): no alert. 30 min is stale.
        last.received_at -= timedelta(minutes=15)
        db.commit()
        alerts.run_periodic_checks(db)
    assert client.get("/alerts", headers=admin).json() == []
    with SessionLocal() as db:
        db.get(DriverLastLocation, d["id"]).received_at -= timedelta(minutes=15)
        db.commit()
        alerts.run_periodic_checks(db)
    assert [a["kind"] for a in client.get("/alerts", headers=admin).json()] == ["LOCATION_STALE"]

    # Pending consent expires after SIM_PENDING_CONSENT_HOURS.
    _, d2 = _driver(client, admin, c["id"], phone="9845000002", reg="KA01AB0002")
    with SessionLocal() as db:
        db.get(SimConsent, d2["id"]).requested_at -= timedelta(hours=49)
        db.commit()
        assert sim.poll(db)["expired"] == 1
        assert db.get(SimConsent, d2["id"]).status == "EXPIRED"


def test_company_scoping(client, admin):
    a = _company(client, admin)
    b = _company(client, admin, code="SHC", name="Sahyadri Haulage Co.")
    assert client.post("/companies", json={"code": "VFC", "name": "Dup"}, headers=admin).status_code == 409
    va, da = _driver(client, admin, a["id"])
    vb, db_ = _driver(client, admin, b["id"], phone="9845000002", reg="MH12AB0002")

    # A driver cannot take another company's truck, at onboarding or at trip assignment.
    vb2 = client.post("/vehicles", json={"reg_no": "MH12AB0003", "company_id": b["id"]}, headers=admin).json()
    assert client.patch(f"/drivers/{da['id']}", json={"vehicle_id": vb2["id"]}, headers=admin).status_code == 409
    s = client.post("/shipments", headers=admin, json={
        "customer_name": "Acme", "pickup_address": "A", "pickup_lat": 12.9, "pickup_lon": 77.5,
        "drop_address": "B", "drop_lat": 13.0, "drop_lon": 77.6}).json()
    r = client.post("/trips", json={"shipment_id": s["id"], "driver_id": da["id"], "vehicle_id": vb2["id"]},
                    headers=admin)
    assert r.status_code == 400
    t = client.post("/trips", json={"shipment_id": s["id"], "driver_id": da["id"]}, headers=admin).json()
    assert t["company"]["code"] == "VFC"

    assert [d["id"] for d in client.get("/drivers", params={"company_id": b["id"]}, headers=admin).json()] == [db_["id"]]
    assert len(client.get("/fleet/live", params={"company_id": a["id"]}, headers=admin).json()) == 1
    assert len(client.get("/trips", params={"company_id": a["id"]}, headers=admin).json()) == 1
    assert client.get("/trips", params={"company_id": b["id"]}, headers=admin).json() == []
    rows = {c["code"]: c["counts"] for c in client.get("/companies", headers=admin).json()}
    assert rows["VFC"]["drivers"] == 1 and rows["SHC"]["vehicles"] == 2
