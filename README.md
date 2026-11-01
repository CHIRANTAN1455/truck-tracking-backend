# truck-tracking-backend

FastAPI backend for truck tracking, shipment management and customer tracking links.

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
.venv/bin/python -m scripts.seed   # optional demo data
.venv/bin/uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

* API docs: http://localhost:8000/docs
* Driver app (phone): http://<host>:8000/driver-app/
* Tests: `.venv/bin/python -m pytest`

Phones only allow GPS on **HTTPS** (or localhost). For a phone test, expose port 8000 through an HTTPS
tunnel (e.g. `cloudflared tunnel --url http://localhost:8000` or ngrok) and open `/driver-app/` there.

### Simulate a truck

```sh
.venv/bin/python scripts/simulate_driver.py --phone 9000000001 --trip TRP-XXXXXX \
    --offline 10-25 --deviate 30-40 --otp 1234
```

`--offline` holds points locally and sends them as one batch when the network comes back;
`--deviate` pushes the truck ~8 km off route to trigger a route-deviation alert.

## Requirement → implementation

| Requirement | Where |
|---|---|
| Driver login | `POST /auth/driver/otp/request`, `/auth/driver/otp/verify` (phone + OTP; `DEV_MODE` returns the OTP) |
| Phone GPS capture, send to backend | `app/static/driver/index.html` → `POST /ui/driver/location` (LTS contract) |
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
