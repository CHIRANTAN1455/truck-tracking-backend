"""Demo data: 4 trucking companies, 100 SIM-tracked drivers, trucks, shipments and live trips.

    python -m scripts.seed            # only into an empty database
    python -m scripts.seed --reset    # drop all tables first

All companies, people, customers and phone numbers are fictional. Runs only with SIM_PROVIDER=mock so a
real aggregator never sends consent SMS to the demo numbers.
"""

import argparse
import random
import sys
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Company, Driver, Shipment, SimStatus, Trip, TripStatus, Vehicle
from app.services import sim, trips
from app.services.sim.providers import MockProvider
from app.timeutil import utcnow

rng = random.Random(1455)

# Uneven split on purpose: one large fleet, two mid-size, one small.
COMPANIES = [
    # code, name, state prefix, hub city, hub lat/lon, driver count, routes (destination, lat, lon)
    ("VFC", "Vayu Freight Carriers", "KA", "Bengaluru", 12.9716, 77.5946, 41, [
        ("Chennai, Sriperumbudur", 12.9675, 79.9419), ("Hyderabad, Shamshabad", 17.2403, 78.4294),
        ("Mysuru, Hebbal Industrial Area", 12.3460, 76.6180), ("Hubballi APMC Yard", 15.3647, 75.1240),
        ("Coimbatore, SIDCO", 11.0168, 76.9558), ("Tumakuru, Vasanthanarasapura", 13.3409, 77.1010),
        ("Hosur SIPCOT", 12.7409, 77.8253), ("Anantapur Bypass", 14.6819, 77.6006)]),
    ("SHC", "Sahyadri Haulage Co.", "MH", "Pune", 18.5204, 73.8567, 29, [
        ("Bhiwandi Warehousing Hub", 19.2813, 73.0483), ("Nashik, Ambad MIDC", 19.9975, 73.7898),
        ("Kolhapur, Gokul Shirgaon", 16.7050, 74.2433), ("Aurangabad, Waluj MIDC", 19.8762, 75.3433),
        ("Goa, Verna Industrial Estate", 15.3570, 73.9370), ("Surat, Sachin GIDC", 21.0850, 72.8800)]),
    ("ICM", "Indraprastha Cargo Movers", "DL", "Delhi", 28.6139, 77.2090, 18, [
        ("Jaipur, Sitapura", 26.7840, 75.8240), ("Agra, Sikandra", 27.2046, 77.9600),
        ("Chandigarh, Industrial Area", 30.7046, 76.8010), ("Lucknow, Transport Nagar", 26.8000, 80.8900),
        ("Dehradun, Selaqui", 30.3700, 77.8600), ("Ludhiana, Focal Point", 30.8800, 75.8500)]),
    ("KDT", "Kaveri Delta Transports", "TN", "Chennai", 13.0827, 80.2707, 12, [
        ("Puducherry, Mettupalayam", 11.9416, 79.8083), ("Vellore, Katpadi", 12.9692, 79.1559),
        ("Tiruchirappalli, Thuvakudi", 10.7905, 78.7047), ("Nellore, Autonagar", 14.4426, 79.9865),
        ("Madurai, Kappalur", 9.9252, 78.1198)]),
]
HUB_ADDRESS = {"VFC": "Peenya Industrial Area, Bengaluru", "SHC": "Chakan MIDC, Pune",
               "ICM": "Kundli Industrial Area, Delhi NCR", "KDT": "Ambattur Industrial Estate, Chennai"}
STATE_RTO = {"KA": (1, 2, 3, 4, 5, 51, 53), "MH": (12, 14, 42, 43, 46), "DL": (1, 3, 8, 12), "TN": (1, 2, 5, 9, 18)}
FIRST = ["Ravi", "Suresh", "Ramesh", "Manjunath", "Prakash", "Imran", "Mahesh", "Santosh", "Rajesh", "Venkatesh",
         "Gurpreet", "Harish", "Anil", "Sunil", "Vijay", "Ganesh", "Murugan", "Selvam", "Arjun", "Rahul",
         "Dinesh", "Sanjay", "Mohammed", "Abdul", "Kiran", "Naveen", "Balaji", "Kumar", "Sachin", "Tukaram",
         "Baldev", "Jaswinder", "Ashok", "Deepak", "Pradeep", "Raju", "Sekhar", "Yusuf", "Kartik", "Shankar"]
LAST = ["Kumar", "Patil", "Shaikh", "Gowda", "Reddy", "Singh", "Yadav", "Naik", "Pawar", "Jadhav", "Sharma",
        "Verma", "Khan", "Pillai", "Murthy", "Iyer", "Chauhan", "Gill", "Rathod", "Shetty", "Nair", "Mishra"]
CUSTOMERS = ["Acme Retail", "Blue Foods", "Sunrise FMCG", "Metro Pharma Distributors", "Nirmal Textiles",
             "Prime Auto Components", "GreenLeaf Agro", "Shakti Cement Dealers", "Orbit Electronics", "Annapurna Mills"]
GOODS = ["Palletised FMCG", "Auto parts", "Cement bags", "Packaged food", "Pharma (non-cold)", "Textile bales",
         "Electrical goods", "Steel coils", "Agri produce", "E-commerce parcels"]


def _reg(state: str, used: set) -> str:
    while True:
        rto = rng.choice(STATE_RTO[state])
        letters = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(2))
        reg = f"{state}{rto:02d}{letters}{rng.randint(1000, 9999)}"
        if reg not in used:
            used.add(reg)
            return reg


def seed(reset: bool = False) -> bool:
    """Load the demo data. Returns False (and changes nothing) if the database already has drivers."""
    if settings.sim_provider != "mock":
        raise RuntimeError("Refusing to seed with SIM_PROVIDER != mock: demo numbers must never get real consent SMS.")
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    mock = MockProvider()
    now = utcnow()
    with SessionLocal() as db:
        if db.scalar(select(Driver).limit(1)):
            print("Database already has data; use --reset to start over.")
            return False
        used_regs: set = set()
        phone_no = 9000000000
        summary = []
        for code, name, state, city, hlat, hlon, n_drivers, routes in COMPANIES:
            company = Company(code=code, name=name, hub_city=city, hub_lat=hlat, hub_lon=hlon,
                              contact_name=f"{name} control room", contact_email=f"ops@{code.lower()}.example")
            db.add(company)
            db.flush()
            # A few spare trucks per company: fleets always have more trucks than drivers on shift.
            vehicles = [Vehicle(reg_no=_reg(state, used_regs), company_id=company.id,
                                vehicle_type=rng.choice(["truck", "truck", "truck", "trailer", "container", "mini-truck"]),
                                capacity_kg=rng.choice([7000, 9000, 12000, 16000, 25000]))
                        for _ in range(n_drivers + max(2, n_drivers // 8))]
            db.add_all(vehicles)
            db.flush()

            counts = {"drivers": 0, "on_trip": 0, "assigned": 0, "idle": 0}
            for i in range(n_drivers):
                phone_no += 1
                d = Driver(name=f"{rng.choice(FIRST)} {rng.choice(LAST)}", phone=str(phone_no),
                           license_no=f"{state}{rng.randint(10, 99)}{rng.randint(2008, 2022)}{rng.randint(1000000, 9999999)}",
                           company_id=company.id, vehicle_id=vehicles[i].id)
                db.add(d)
                db.flush()
                counts["drivers"] += 1

                # Consent mix: most drivers consented; a few pending / revoked / failed, like a real rollout.
                consent = sim.request_consent(db, d)
                roll = rng.random()
                if roll < 0.90:
                    sim.set_status_from_provider(db, consent.msisdn, SimStatus.ACTIVE, "seed: driver replied Y")
                elif roll < 0.95:
                    pass  # still PENDING
                elif roll < 0.98:
                    sim.set_status_from_provider(db, consent.msisdn, SimStatus.ACTIVE, "seed: driver replied Y")
                    sim.revoke(db, d, "seed: driver withdrew consent")
                else:
                    sim.set_status_from_provider(db, consent.msisdn, SimStatus.FAILED, "seed",
                                                 error="Number not reachable on network")

                # Trip mix: ~60% on the road, ~12% assigned and waiting to load, rest idle at the hub.
                plan = rng.random()
                if plan > 0.72:
                    counts["idle"] += 1
                    continue
                dest, dlat, dlon = rng.choice(routes)
                s = Shipment(id=trips.new_id("SHP"), reference=f"{code}-{rng.randint(10000, 99999)}",
                             customer_name=rng.choice(CUSTOMERS), customer_phone=None,
                             customer_email=f"logistics{rng.randint(1, 99)}@customer.example",
                             description=rng.choice(GOODS), weight_kg=rng.choice([3000, 5000, 8000, 11000, 15000]),
                             pickup_address=HUB_ADDRESS[code], pickup_lat=hlat + rng.uniform(-0.05, 0.05),
                             pickup_lon=hlon + rng.uniform(-0.05, 0.05), drop_address=dest, drop_lat=dlat,
                             drop_lon=dlon, require_otp=rng.random() < 0.4,
                             delivery_otp=f"{rng.randint(0, 9999):04d}")
                db.add(s)
                db.flush()
                t = trips.create_trip(db, s.id, d.id, None, None)
                if plan > 0.60:
                    counts["assigned"] += 1
                    continue
                trips.transition(db, t, TripStatus.STARTED, "Seed")
                # Spread trucks along their routes: started between 10% and 95% of the planned duration ago.
                back = timedelta(seconds=(t.planned_duration_s or 3600) * rng.uniform(0.1, 0.95))
                t.started_at = now - back
                t.expected_delivery_at = t.started_at + timedelta(seconds=(t.planned_duration_s or 3600) * 1.2)
                t.last_lat, t.last_lon = s.pickup_lat, s.pickup_lon
                db.commit()
                counts["on_trip"] += 1

                # Backfill the trail: one network fix every SIM_PING_ON_TRIP_MIN since the trip started.
                if consent.status == SimStatus.ACTIVE:
                    step = timedelta(minutes=settings.sim_ping_on_trip_min)
                    at = t.started_at + step
                    while at < now - step:
                        fix = mock.fix_at(db, d, at, consent.operator)
                        if fix:
                            sim.ingest_fix(db, consent, fix, "seed backfill")
                        at += step
            db.commit()
            summary.append((code, name, city, counts))

        stats = sim.poll(db)  # first live ping for every consented SIM
        print(f"Initial SIM poll: {stats}")
        print(f"\n{'Code':5} {'Company':28} {'Hub':10} {'Drivers':>7} {'On trip':>8} {'Assigned':>9} {'Idle':>5}")
        for code, name, city, c in summary:
            print(f"{code:5} {name:28} {city:10} {c['drivers']:>7} {c['on_trip']:>8} {c['assigned']:>9} {c['idle']:>5}")
    print("\nSeeded. Admin login: ADMIN_USERNAME / ADMIN_PASSWORD (default admin / admin123)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    try:
        seed(ap.parse_args().reset)
    except RuntimeError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
