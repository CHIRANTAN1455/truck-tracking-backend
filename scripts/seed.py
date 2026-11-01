"""Create demo vehicles, drivers, shipments and trips.  Usage: python -m scripts.seed"""

import secrets
from datetime import timedelta

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import Driver, Shipment, Vehicle
from app.services import trips
from app.timeutil import utcnow

VEHICLES = [("KA01AB1234", "truck", 9000), ("KA02CD5678", "truck", 12000), ("MH12EF9012", "mini-truck", 3000)]
DRIVERS = [("Ravi Kumar", "9000000001", "KA0120190001234"), ("Suresh Patil", "9000000002", "KA0220180005678"),
           ("Imran Shaikh", "9000000003", "MH1220170009012")]
SHIPMENTS = [
    ("Acme Retail", "9800000001", "ops@acme.example", "Bengaluru Warehouse, Peenya", 13.0285, 77.5197,
     "Tumakuru Distribution Centre", 13.3409, 77.1010, False),
    ("Blue Foods", "9800000002", "logistics@bluefoods.example", "Hosur Road Hub, Bengaluru", 12.8456, 77.6603,
     "Krishnagiri Depot", 12.5186, 78.2137, True),
]


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(Driver).limit(1)):
            print("Database already has data; skipping seed.")
            return
        vehicles = [Vehicle(reg_no=r, vehicle_type=t, capacity_kg=c) for r, t, c in VEHICLES]
        db.add_all(vehicles)
        db.flush()
        drivers = [Driver(name=n, phone=p, license_no=lic, vehicle_id=v.id)
                   for (n, p, lic), v in zip(DRIVERS, vehicles)]
        db.add_all(drivers)
        db.flush()
        for i, (cust, phone, email, pa, plat, plon, da, dlat, dlon, otp) in enumerate(SHIPMENTS):
            s = Shipment(id=trips.new_id("SHP"), customer_name=cust, customer_phone=phone, customer_email=email,
                         description="Palletised goods", weight_kg=2500 + 1000 * i, pickup_address=pa,
                         pickup_lat=plat, pickup_lon=plon, drop_address=da, drop_lat=dlat, drop_lon=dlon,
                         require_otp=otp, delivery_otp=f"{secrets.randbelow(10_000):04d}")
            db.add(s)
            db.commit()
            t = trips.create_trip(db, s.id, drivers[i].id, None, utcnow() + timedelta(hours=4))
            print(f"Trip {t.id} for shipment {s.id} -> driver {drivers[i].name} ({drivers[i].phone})")
        db.commit()
    print("Seeded. Admin login: ADMIN_USERNAME / ADMIN_PASSWORD (default admin / admin123)")


if __name__ == "__main__":
    main()
