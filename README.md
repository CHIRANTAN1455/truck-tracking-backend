# truck-tracking-backend

FastAPI backend for truck tracking, shipment management and customer tracking links.

**Trucks are tracked through the driver's SIM card** (consent-based telecom network location, the method
Indian transporters use under TRAI / DoT rules: the driver must explicitly consent by SMS). No app or
hardware is needed on the truck. The phone app (`/driver-app/`) is still supported as an optional, more
precise source. Drivers belong to **trucking companies**, and every list can be filtered by company.
See [SIM tracking](#sim-tracking) below.

Location ingestion is a Python port of
[nammayatri/location-tracking-service](https://github.com/nammayatri/location-tracking-service) (LTS):
same endpoints, same payload (`[{pt:{lat,lon}, ts, acc, v, bear}]` with a `token` header), same rules
(batched points, sorted by device time, stale/duplicate points dropped, points worse than
`MIN_LOCATION_ACCURACY_M` kept but ignored for distance). Redis/Kafka from LTS are replaced by SQL tables,
so a single process plus SQLite/Postgres is enough to run it.

Fleet, trip, delivery and tracking concepts follow the nammayatri monorepo
(`FleetDriverAssociation`, `TripTransaction`, `DeliveryDetails`, `FollowRide`, `TripAlertRequest`).

## Run

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # edit ADMIN_PASSWORD etc.
.venv/bin/python -m scripts.seed --reset   # demo: 4 companies, 100 SIM-tracked drivers, ~60 live trips
.venv/bin/uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

* API docs: http://localhost:8000/docs
* Driver app (phone): http://<host>:8000/driver-app/
* Tests: `.venv/bin/python -m pytest`

Phones only allow GPS on **HTTPS** (or localhost). For a phone test, expose port 8000 through an HTTPS
tunnel (e.g. `cloudflared tunnel --url http://localhost:8000` or ngrok) and open `/driver-app/` there.

### Simulate a truck with the phone app (GPS)

```sh
.venv/bin/python scripts/simulate_driver.py --phone 9000000001 --trip TRP-XXXXXX \
    --offline 10-25 --deviate 30-40 --otp 1234
```

`--offline` holds points locally and sends them as one batch when the network comes back;
`--deviate` pushes the truck ~8 km off route to trigger a route-deviation alert.

## Requirement → implementation

| Requirement | Where |
|---|---|
| SIM-based tracking (no app) | `app/services/sim/` consent + scheduled pings, `POST /integrations/sim/webhook`, `/sim/*`, `/drivers/{id}/sim/*` |
| Trucking companies | `/companies`; `company_id` filter on `/drivers`, `/vehicles`, `/fleet/live`, `/trips`, `/alerts`, `/reports/trips` |
| Driver login | `POST /auth/driver/otp/request`, `/auth/driver/otp/verify` (phone + OTP; `DEV_MODE` returns the OTP) |
| Phone GPS capture (optional) | `app/static/driver/index.html` → `POST /ui/driver/location` (LTS contract) |
| Save in DB | `location_points` (history), `driver_last_locations` (latest) |
| Offline buffering + sync | Driver app queues points in `localStorage`, flushes in batches of 100 on reconnect; server sorts + de-duplicates |
| Last update timestamp | `last_location.ts / received_at / age_sec` on fleet, trip and public responses |
| Driver profile, vehicle assignment | `/drivers`, `/vehicles`, `PATCH /drivers/{id}` (`vehicle_id`, one driver per vehicle) |
| Trip create / assign / start / complete | `/trips`, `/trips/{id}/assign`, `/status`, `/cancel`, `/pod`; LTS `/internal/ride/{id}/start|end` |
| Multiple drivers, trucks, simultaneous trips | One on-ride trip per driver, any number of drivers |
| Shipments, pickup & destination | `/shipments` |
| Active / completed / cancelled, history, reports | `GET /trips?group=active|completed|cancelled`, `GET /reports/trips` (distance, duration, on-time) |
| Location history | `GET /trips/{id}/locations`, `GET /drivers/{id}/locations?start&end` |
| Unique tracking link, expiry, revocation | `/shipments/{id}/tracking-links`, `/tracking-links/{token}/revoke`; auto-created at trip start |
| Share via WhatsApp / SMS / Email | `share` links (wa.me, sms:, mailto:) on every link; automatic customer messages at trip start/delivery |
| Public tracking (no app) | `GET /public/track/{token}` → rendered by the dashboard at `/?track=<token>` |
| Status timeline | Picked Up → Trip Started → In Transit → Reached Destination → Delivered (In Transit / Reached are automatic from GPS) |
| ETA | Remaining distance × `ROAD_FACTOR` ÷ recent average speed (fallback `DEFAULT_SPEED_KMPH`) |
| Stop live tracking after delivery | Public API hides location once the trip is delivered or cancelled |
| POD: photo, receiver, optional OTP | `POST /driver/trips/{id}/pod` (multipart); OTP mandatory if `require_otp` on the shipment; drivers never see the OTP |
| Route deviation alert | Every point checked against the planned route (OSRM if `OSRM_URL` set, else straight line) |
| Delay alert / location stopped alert | Background check every `ALERT_CHECK_INTERVAL_SEC` (`/alerts/run-checks` to force) |
| Trip start & delivery notifications | `app/services/notify.py` — logged in `notifications`; email via SMTP |

## Not wired yet (needs your accounts)

* **SMS / WhatsApp sending**: `_send_sms` / `_send_whatsapp` in `app/services/notify.py` only log.
  Plug in Twilio / Gupshup / MSG91 / Meta WhatsApp Cloud API there. Until then, operators use the share
  buttons in the dashboard, and driver OTPs only work with `DEV_MODE=true`.
* **Road routing**: run [nammayatri/osrm-backend](https://github.com/nammayatri/osrm-backend) (or any OSRM)
  and set `OSRM_URL` for road routes and a tighter deviation threshold.
* **Background GPS**: a browser keeps GPS running only while the driver app is open on screen (it holds a
  screen wake lock). For screen-off tracking, wrap the same API in a native Android app.

## SIM tracking

### How it works

```
                 consent SMS ("Reply Y to allow ...")
 Ops adds driver ───────────────▶ Aggregator ─────────▶ Driver's SIM (Jio / Airtel / Vi / BSNL)
 (auto, or /sim/consent)              ▲   │                     │ replies Y
                                      │   ◀─────────────────────┘
          scheduled "locate" calls    │   │  consent + location events
   backend SIM poller ────────────────┘   │  (HTTPS webhook, HMAC signed)
   (10 min on trip, 60 min idle)          ▼
                                POST /integrations/sim/webhook
                                          │
                     services/location.py (same pipeline as phone GPS, source="sim")
                                          │
          trips auto In Transit / Reached · distance · ETA · deviation / stale / delay alerts
                                          │
                     dashboard live map · customer tracking link
```

* The SIM itself sends nothing. The **telecom network** locates it (cell tower / LBS), and a licensed
  **location aggregator** sells this to fleets. Fixes are **approximate (300 m to 3 km)** and come
  **every few minutes**, not every second.
* A number is located **only while its consent is ACTIVE**. Fixes for any other number are dropped, and
  every consent change is logged in `sim_events` (your proof of consent).
* Coarse fixes use wider radii: In Transit after leaving 3 km from pickup, Reached within 2 km of the
  drop, route deviation threshold plus fix accuracy, no distance added for movement inside the error
  circle, and stale after 2.5 missed pings (25 min) instead of 10 min.

### Protocol: HTTPS (not MQTT)

The other end is a telecom aggregator's server, not a device we control. Aggregators expose REST APIs
and HTTPS webhooks; none offer MQTT. So:

* **Inbound:** `POST /integrations/sim/webhook`, signed with `X-Signature: sha256=<HMAC-SHA256(body, SIM_WEBHOOK_SECRET)>`.
* **Outbound:** our scheduler calls the aggregator's REST API (consent, locate, revoke) as a fallback, and for
  providers that only support pull.

MQTT (or the AIS-140 TCP protocol) becomes useful only if we later fit our own GPS boxes in trucks. That
would be another `LocationSource` feeding the same pipeline.

### Data structure

| Table | Key fields |
|---|---|
| `companies` | `code` (unique, e.g. VFC), `name`, `gstin`, contact, `hub_city`, `hub_lat/lon` |
| `drivers` | `phone` (10-digit Indian mobile = the tracked SIM), `company_id`, `vehicle_id` |
| `vehicles` | `reg_no`, `company_id` (a driver can only drive their own company's trucks) |
| `sim_consents` | one per driver: `msisdn` (91XXXXXXXXXX), `provider`, `provider_ref`, `operator`, `status` (PENDING → ACTIVE → REVOKED / EXPIRED / FAILED), `requested_at`, `activated_at`, `expires_at`, `last_ping_at`, `last_fix_at`, `consecutive_failures`, `last_error` |
| `sim_events` | audit trail: `driver_id`, `msisdn`, `kind`, `detail`, `created_at` |
| `location_points` | `lat`, `lon`, `ts`, `acc` (m), `speed`, `source` (`sim` / `app`), `accurate`, `trip_id` |
| `driver_last_locations` | latest point per driver, with `source` |

### SIM webhook contract

```json
POST /integrations/sim/webhook
X-Signature: sha256=3f1c...            (hex HMAC-SHA256 of the raw body)

{"events": [
  {"type": "location", "msisdn": "919845000001", "lat": 12.9716, "lon": 77.5946,
   "accuracy_m": 1200, "ts": "2026-11-11T09:30:00Z", "operator": "JIO", "cell_id": "404-45-1234",
   "event_id": "evt_123"},
  {"type": "consent", "msisdn": "919845000001", "status": "ACTIVE"}
]}
```

The response is always `200` for a valid signed request, with a result per event (`accepted`,
`duplicate_or_old`, `unknown_msisdn`, `ignored_consent_pending`, ...), so the provider does not retry
events we ignored on purpose. Consent statuses are mapped loosely (`APPROVED`/`Y` → ACTIVE,
`OPTED_OUT`/`DENIED` → REVOKED, ...) in `providers.map_status`.

### Connecting a real SIM tracking provider

1. **Sign with a consent-based telecom location aggregator** that covers Jio, Airtel, Vi and BSNL. Ask for:
   API documentation, sandbox keys, the consent SMS / DLT template they send, price per ping, minimum
   ping interval, webhook support, and how drivers opt out.
2. **Compliance:** register your entity / sender ID on the operators' DLT platform if the provider asks you to.
   Add a driver consent clause to your driver onboarding (what is tracked, why, how long, how to stop).
   Keep the data only as long as you need it (see `sim_events` for the consent proof).
3. **Write the adapter:** in `app/services/sim/providers.py`, fill in `AggregatorProvider` (`request_consent`,
   `consent_status`, `locate`, `revoke`) with the provider's endpoints and field names, or copy it into a
   new class and register it in `_PROVIDERS`.
4. **Configure** `.env`: `SIM_PROVIDER=aggregator`, `SIM_API_BASE_URL`, `SIM_API_KEY`,
   `SIM_WEBHOOK_SECRET=$(openssl rand -hex 32)`, and `SIM_PING_ON_TRIP_MIN` / `SIM_PING_IDLE_MIN` to match your plan.
5. **Expose the webhook over HTTPS** (deployed URL, or `cloudflared tunnel --url http://localhost:8000` for
   testing) and give the provider `https://<host>/integrations/sim/webhook` plus the shared secret. If their
   payload differs from the contract above, map it in the webhook handler (`app/routers/sim.py`) or ask them for
   a custom template.
6. **Test with one real driver:** add the driver (consent SMS goes out), have them reply, and check the SIM
   tracking page shows *Tracking*. Then use *Locate now* and confirm the point on the live map. You can also
   send a signed test event yourself:
   `python scripts/send_sim_webhook.py --url https://<host> --secret <secret> --phone 98450xxxxx --lat 12.97 --lon 77.59`.
7. **Roll out:** *Send consent SMS to all unregistered drivers* per company, then follow up on anyone still
   *Awaiting consent* (pending requests expire after 48 h).
8. **Before go-live:** use Postgres (`DATABASE_URL`), drop the demo seed data, set `DEV_MODE=false`, and
   watch the stale-location alerts. They are the first sign of a provider or consent problem.

### Database changes

This version adds tables and columns. There are no migrations yet, so recreate a development database
(`python -m scripts.seed --reset`, or delete `tracking.db`). Add Alembic before the first production deploy.
