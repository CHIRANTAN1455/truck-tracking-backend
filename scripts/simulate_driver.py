"""Drive a truck from pickup to drop by posting GPS points, like the driver app would.

Demonstrates offline buffering: during the --offline window points are kept locally and sent as one
batch when the "network" comes back.

  python scripts/simulate_driver.py --phone 9000000001 --trip TRP-XXXXXX
  python scripts/simulate_driver.py --phone 9000000001 --trip TRP-XXXXXX --offline 10-25 --deviate 30-40
"""

import argparse
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def call(api, path, body=None, token=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(api + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path}: {e.code} {e.read().decode()}")


def window(spec):
    if not spec:
        return range(0)
    a, b = spec.split("-")
    return range(int(a), int(b) + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--phone", required=True)
    ap.add_argument("--trip", required=True)
    ap.add_argument("--steps", type=int, default=60, help="number of GPS points from pickup to drop")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between points")
    ap.add_argument("--speed-kmph", type=float, default=50)
    ap.add_argument("--offline", help="step range with no network, e.g. 10-25")
    ap.add_argument("--deviate", help="step range where the truck goes ~8 km off route, e.g. 30-40")
    ap.add_argument("--otp", help="delivery OTP the customer shares (needed if the shipment requires OTP)")
    ap.add_argument("--no-deliver", action="store_true", help="stop at destination without POD")
    args = ap.parse_args()

    otp = call(args.api, "/auth/driver/otp/request", {"phone": args.phone}).get("dev_otp")
    if not otp:
        raise SystemExit("Backend is not in DEV_MODE; cannot read OTP automatically")
    token = call(args.api, "/auth/driver/otp/verify", {"phone": args.phone, "otp": otp})["token"]

    trip = next((t for t in call(args.api, "/driver/trips", token=token) if t["id"] == args.trip), None)
    if trip is None:
        raise SystemExit(f"Trip {args.trip} is not an open trip of this driver")
    p, d = trip["shipment"]["pickup"], trip["shipment"]["drop"]

    def send(batch):
        r = call(args.api, "/ui/driver/location", batch, token)
        print(f"  sent {len(batch)} point(s): accepted={r['accepted']} dropped={r['dropped']}")

    # First fix at pickup, then start the trip.
    def fix(lat, lon):
        return {"pt": {"lat": lat, "lon": lon}, "ts": datetime.now(timezone.utc).isoformat(),
                "acc": round(random.uniform(5, 20), 1), "v": args.speed_kmph / 3.6,
                "bear": math.degrees(math.atan2(d["lon"] - p["lon"], d["lat"] - p["lat"])) % 360}

    send([fix(p["lat"], p["lon"])])
    if trip["status"] in ("ASSIGNED", "PICKED_UP"):
        call(args.api, f"/driver/trips/{args.trip}/status", {"status": "STARTED"}, token)
        print(f"Trip {args.trip} started")

    offline, deviate, buffer = window(args.offline), window(args.deviate), []
    for i in range(1, args.steps + 1):
        f = i / args.steps
        lat = p["lat"] + (d["lat"] - p["lat"]) * f + random.uniform(-0.0003, 0.0003)
        lon = p["lon"] + (d["lon"] - p["lon"]) * f + random.uniform(-0.0003, 0.0003)
        if i in deviate:
            lat += 0.07  # ~8 km north
        buffer.append(fix(lat, lon))
        if i in offline:
            print(f"step {i}: offline, buffered {len(buffer)}")
        else:
            print(f"step {i}: online")
            send(buffer)
            buffer = []
        time.sleep(args.interval)
    if buffer:
        send(buffer)

    if not args.no_deliver:
        body = urllib.parse.urlencode({"receiver_name": "Store Manager", **({"otp": args.otp} if args.otp else {})})
        req = urllib.request.Request(f"{args.api}/driver/trips/{args.trip}/pod", data=body.encode(), method="POST")
        req.add_header("token", token)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print("Delivered:", json.load(r)["status"])
        except urllib.error.HTTPError as e:
            print("POD failed:", e.code, e.read().decode())


if __name__ == "__main__":
    main()
