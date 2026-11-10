"""Send a signed SIM webhook event, exactly as the aggregator would. Use it to test the integration end to end.

    python scripts/send_sim_webhook.py --url http://localhost:8000 --secret $SIM_WEBHOOK_SECRET \\
        --phone 9000000001 --lat 12.97 --lon 77.59 --acc 1200
    python scripts/send_sim_webhook.py ... --phone 9000000001 --consent ACTIVE
"""

import argparse
import hashlib
import hmac
import json
import urllib.request
import uuid
from datetime import datetime, timezone


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--phone", required=True, help="driver mobile, any format (+91 98450 00001)")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--acc", type=float, default=1000)
    ap.add_argument("--consent", help="send a consent event instead: ACTIVE | REVOKED | EXPIRED | FAILED")
    a = ap.parse_args()

    digits = "".join(c for c in a.phone if c.isdigit())[-10:]
    if a.consent:
        event = {"type": "consent", "msisdn": "91" + digits, "status": a.consent}
    else:
        event = {"type": "location", "msisdn": "91" + digits, "lat": a.lat, "lon": a.lon, "accuracy_m": a.acc,
                 "ts": datetime.now(timezone.utc).isoformat(), "operator": "JIO"}
    event["event_id"] = str(uuid.uuid4())
    raw = json.dumps({"events": [event]}).encode()
    sig = "sha256=" + hmac.new(a.secret.encode(), raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(a.url.rstrip("/") + "/integrations/sim/webhook", data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "X-Signature": sig})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    main()
