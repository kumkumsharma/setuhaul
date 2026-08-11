"""Seed realistic classroom data aligned to SetuHaul PDF scenarios.

Scenario clock: 2026-08-11 17:25 IST (Jaipur evening contention snapshot).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine, init_db
from app.models import (
    Appointment,
    AppointmentSlot,
    Contact,
    Dock,
    Driver,
    EtaUpdate,
    Facility,
    FacilityCheckin,
    FacilityRule,
    Shipment,
    Vehicle,
)
from app.services import redis_client as rc

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 8, 11, tzinfo=IST)


def dt(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=0, microsecond=0)


def reset_database() -> None:
    Base.metadata.drop_all(bind=engine)
    init_db()
    # Clear fakeredis / redis keys used for holds
    redis = rc.get_redis()
    try:
        redis.flushdb()
    except Exception:
        pass


def seed(db: Session | None = None) -> None:
    own_session = db is None
    if own_session:
        reset_database()
        db = SessionLocal()
    assert db is not None

    # --- Facilities (6) ---
    facilities = [
        Facility(
            facility_id="FAC-JPR-01",
            name="Jaipur DC",
            city="Jaipur",
            timezone="Asia/Kolkata",
            open_time="06:00",
            close_time="23:00",
            contact_id="CON-JPR",
            latitude=26.9124,
            longitude=75.7873,
        ),
        Facility(
            facility_id="FAC-DEL-01",
            name="Delhi Hub",
            city="Delhi",
            timezone="Asia/Kolkata",
            open_time="05:00",
            close_time="22:00",
            contact_id="CON-DEL",
        ),
        Facility(
            facility_id="FAC-GUR-01",
            name="Gurugram Crossdock",
            city="Gurugram",
            timezone="Asia/Kolkata",
            open_time="06:00",
            close_time="23:00",
        ),
        Facility(
            facility_id="FAC-MUM-01",
            name="Mumbai West DC",
            city="Mumbai",
            timezone="Asia/Kolkata",
            open_time="06:00",
            close_time="22:00",
        ),
        Facility(
            facility_id="FAC-AMD-01",
            name="Ahmedabad DC",
            city="Ahmedabad",
            timezone="Asia/Kolkata",
            open_time="06:00",
            close_time="23:00",
        ),
        Facility(
            facility_id="FAC-PUN-01",
            name="Pune DC",
            city="Pune",
            timezone="Asia/Kolkata",
            open_time="06:00",
            close_time="22:30",
        ),
    ]
    db.add_all(facilities)

    # Jaipur docks D1/D2 + extras (PDF competition uses two doors)
    docks = [
        Dock(
            dock_id="DOCK-JPR-D1",
            facility_id="FAC-JPR-01",
            dock_name="D1",
            supported_vehicle_type="dry_van|any",
            supported_product_class="dry|fmcg|any",
            max_length_ft=32,
            active_flag=True,
        ),
        Dock(
            dock_id="DOCK-JPR-D2",
            facility_id="FAC-JPR-01",
            dock_name="D2",
            supported_vehicle_type="dry_van",
            supported_product_class="dry|fmcg",
            max_length_ft=32,
            active_flag=True,
        ),
        Dock(
            dock_id="DOCK-JPR-04",
            facility_id="FAC-JPR-01",
            dock_name="Dock 04",
            supported_vehicle_type="dry_van",
            supported_product_class="dry",
            max_length_ft=32,
            active_flag=True,
        ),
        Dock(
            dock_id="DOCK-JPR-REEFER",
            facility_id="FAC-JPR-01",
            dock_name="Reefer 1",
            supported_vehicle_type="reefer",
            supported_product_class="cold|perishable",
            max_length_ft=32,
            active_flag=True,
        ),
        # Capacity-reduction scenario: this dock can be blocked later in tests
        Dock(
            dock_id="DOCK-JPR-05",
            facility_id="FAC-JPR-01",
            dock_name="Dock 05",
            supported_vehicle_type="dry_van",
            supported_product_class="dry|fmcg",
            max_length_ft=32,
            active_flag=True,
        ),
    ]
    # Other facility docks (minimal)
    for fac, n in [("FAC-DEL-01", 4), ("FAC-GUR-01", 3), ("FAC-MUM-01", 4), ("FAC-AMD-01", 3), ("FAC-PUN-01", 3)]:
        for i in range(1, n + 1):
            docks.append(
                Dock(
                    dock_id=f"DOCK-{fac[-6:]}-{i}",
                    facility_id=fac,
                    dock_name=f"Dock {i}",
                    supported_vehicle_type="dry_van|reefer",
                    supported_product_class="dry|cold|fmcg|any",
                    max_length_ft=40,
                )
            )
    db.add_all(docks)

    db.add(
        FacilityRule(
            rule_id="RULE-JPR-LEN",
            facility_id="FAC-JPR-01",
            rule_type="max_vehicle_length_ft",
            rule_value="32",
            effective_from=None,
            effective_to=None,
        )
    )
    db.add(
        FacilityRule(
            rule_id="RULE-JPR-DRY",
            facility_id="FAC-JPR-01",
            rule_type="allowed_product_class",
            rule_value="dry|fmcg|cold|perishable",
            effective_from="06:00",
            effective_to="23:00",
        )
    )

    # --- Drivers & vehicles ---
    # PDF example: DRV-027 Ravi Kumar / VEH-031
    drivers = [
        Driver(driver_id="DRV-027", name="Ravi Kumar", phone="+919800000027", carrier_id="CAR-08", status="active", home_base="Neemrana"),
        Driver(driver_id="DRV-201", name="Asha Verma", phone="+919800000201", carrier_id="CAR-01", status="active"),
        Driver(driver_id="DRV-202", name="Imran Khan", phone="+919800000202", carrier_id="CAR-02", status="active"),
        Driver(driver_id="DRV-203", name="Suresh Patel", phone="+919800000203", carrier_id="CAR-03", status="active"),
        Driver(driver_id="DRV-204", name="Deepa Nair", phone="+919800000204", carrier_id="CAR-01", status="active"),
        Driver(driver_id="DRV-MULTI", name="Karan Singh", phone="+919800000999", carrier_id="CAR-08", status="active"),
        Driver(driver_id="DRV-NOP", name="No Slot Driver", phone="+919800000500", carrier_id="CAR-09", status="active"),
        Driver(driver_id="DRV-HIPRI", name="Priority Late", phone="+919800000700", carrier_id="CAR-01", status="active"),
    ]
    # 10+ competing evening drivers
    for i in range(10):
        drivers.append(
            Driver(
                driver_id=f"DRV-EVE-{i:02d}",
                name=f"Evening Driver {i}",
                phone=f"+9198111100{i:02d}",
                carrier_id=f"CAR-{(i % 5) + 1:02d}",
                status="active",
            )
        )
    db.add_all(drivers)

    vehicles = [
        Vehicle(vehicle_id="VEH-031", carrier_id="CAR-08", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-201", carrier_id="CAR-01", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-202", carrier_id="CAR-02", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-203", carrier_id="CAR-03", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-204", carrier_id="CAR-01", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-M1", carrier_id="CAR-08", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-M2", carrier_id="CAR-08", vehicle_type="dry_van", length_ft=28, refrigeration_required=False),
        Vehicle(vehicle_id="VEH-NOP", carrier_id="CAR-09", vehicle_type="dry_van", length_ft=40, refrigeration_required=False),  # too long for Jaipur rule
        Vehicle(vehicle_id="VEH-HIPRI", carrier_id="CAR-01", vehicle_type="dry_van", length_ft=32, refrigeration_required=False),
    ]
    for i in range(10):
        vehicles.append(
            Vehicle(
                vehicle_id=f"VEH-EVE-{i:02d}",
                carrier_id=f"CAR-{(i % 5) + 1:02d}",
                vehicle_type="dry_van",
                length_ft=32,
                refrigeration_required=False,
            )
        )
    db.add_all(vehicles)

    # --- Core PDF narrative shipments ---
    shipments = [
        # Ravi tyre example SHP-1042
        Shipment(
            shipment_id="SHP-1042",
            driver_id="DRV-027",
            vehicle_id="VEH-031",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(17, 20),
            expected_unload_minutes=40,
            status="in_transit",
            leave_by=dt(21, 0),
        ),
        # Competition snapshot SHP-201..204
        Shipment(
            shipment_id="SHP-201",
            driver_id="DRV-201",
            vehicle_id="VEH-201",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(17, 0),
            expected_unload_minutes=40,
            status="arrived",
        ),
        Shipment(
            shipment_id="SHP-202",
            driver_id="DRV-202",
            vehicle_id="VEH-202",
            origin_id="FAC-GUR-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(16, 45),
            expected_unload_minutes=30,
            status="arrived",
        ),
        Shipment(
            shipment_id="SHP-203",
            driver_id="DRV-203",
            vehicle_id="VEH-203",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(17, 40),
            expected_unload_minutes=45,
            status="in_transit",
        ),
        Shipment(
            shipment_id="SHP-204",
            driver_id="DRV-204",
            vehicle_id="VEH-204",
            origin_id="FAC-AMD-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=2,
            planned_eta=dt(16, 50),
            expected_unload_minutes=40,
            status="arrived",
        ),
        # Multi-shipment driver disambiguation
        Shipment(
            shipment_id="SHP-MULTI-A",
            driver_id="DRV-MULTI",
            vehicle_id="VEH-M1",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(18, 0),
            expected_unload_minutes=30,
            status="in_transit",
        ),
        Shipment(
            shipment_id="SHP-MULTI-B",
            driver_id="DRV-MULTI",
            vehicle_id="VEH-M2",
            origin_id="FAC-GUR-01",
            destination_id="FAC-DEL-01",
            product_class="fmcg",
            priority=4,
            planned_eta=dt(20, 0),
            expected_unload_minutes=30,
            status="in_transit",
        ),
        # No feasible same-day (40ft vs 32ft rule)
        Shipment(
            shipment_id="SHP-NOP",
            driver_id="DRV-NOP",
            vehicle_id="VEH-NOP",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="dry",
            priority=3,
            planned_eta=dt(18, 0),
            expected_unload_minutes=40,
            status="in_transit",
        ),
        # High priority late entrant
        Shipment(
            shipment_id="SHP-HIPRI",
            driver_id="DRV-HIPRI",
            vehicle_id="VEH-HIPRI",
            origin_id="FAC-DEL-01",
            destination_id="FAC-JPR-01",
            product_class="fmcg",
            priority=1,
            planned_eta=dt(17, 30),
            expected_unload_minutes=30,
            status="in_transit",
        ),
    ]

    for i in range(10):
        shipments.append(
            Shipment(
                shipment_id=f"SHP-EVE-{i:02d}",
                driver_id=f"DRV-EVE-{i:02d}",
                vehicle_id=f"VEH-EVE-{i:02d}",
                origin_id="FAC-DEL-01",
                destination_id="FAC-JPR-01",
                product_class="dry",
                priority=3 if i else 2,
                planned_eta=dt(17, 30) + timedelta(minutes=i * 5),
                expected_unload_minutes=30,
                status="in_transit",
            )
        )
    db.add_all(shipments)

    # ETA updates
    etas = [
        EtaUpdate(
            eta_update_id="ETA-880",
            shipment_id="SHP-1042",
            declared_eta=dt(19, 10),
            source_type="driver",
            declared_at=dt(16, 8),
            confidence_note="tyre repair near Neemrana",
        ),
        EtaUpdate(
            eta_update_id="ETA-203",
            shipment_id="SHP-203",
            declared_eta=dt(18, 35),
            source_type="driver",
            declared_at=dt(17, 10),
            confidence_note="updated ETA 6:35 PM",
        ),
        EtaUpdate(
            eta_update_id="ETA-HIPRI",
            shipment_id="SHP-HIPRI",
            declared_eta=dt(18, 10),
            source_type="driver",
            declared_at=dt(17, 20),
            confidence_note="late priority entrant",
        ),
    ]
    for i in range(10):
        etas.append(
            EtaUpdate(
                eta_update_id=f"ETA-EVE-{i:02d}",
                shipment_id=f"SHP-EVE-{i:02d}",
                declared_eta=dt(18, 0) + timedelta(minutes=i * 3),
                source_type="driver",
                declared_at=dt(17, 0),
                confidence_note="evening contention",
            )
        )
    db.add_all(etas)

    # Facility check-ins for competition snapshot
    db.add_all(
        [
            FacilityCheckin(
                checkin_id="CHK-201",
                shipment_id="SHP-201",
                facility_id="FAC-JPR-01",
                gate_in_at=dt(17, 5),
                arrival_status="arrived",
                queue_status="waiting_yard",
            ),
            FacilityCheckin(
                checkin_id="CHK-202",
                shipment_id="SHP-202",
                facility_id="FAC-JPR-01",
                gate_in_at=dt(17, 25),
                arrival_status="arrived",
                queue_status="waiting_gate",
            ),
            FacilityCheckin(
                checkin_id="CHK-204",
                shipment_id="SHP-204",
                facility_id="FAC-JPR-01",
                gate_in_at=dt(16, 55),
                arrival_status="unloading",
                queue_status="docked",
                dock_in_at=dt(17, 0),
                expected_finish_at=dt(17, 40),
            ),
        ]
    )

    # --- Appointment slots: scarce evening capacity at Jaipur ---
    # Only 3-4 compatible open evening slots for contention tests
    slots: list[AppointmentSlot] = []

    def add_slot(slot_id: str, dock_id: str, start: datetime, end: datetime, status: str = "open"):
        slots.append(
            AppointmentSlot(
                slot_id=slot_id,
                facility_id="FAC-JPR-01",
                dock_id=dock_id,
                start_time=start,
                end_time=end,
                capacity_units=1,
                slot_status=status,
            )
        )

    # Original booked windows for competition trucks
    add_slot("SLOT-201", "DOCK-JPR-D1", dt(17, 30), dt(18, 10))
    add_slot("SLOT-202", "DOCK-JPR-D2", dt(17, 0), dt(17, 30))
    add_slot("SLOT-203", "DOCK-JPR-D1", dt(17, 45), dt(18, 30))
    add_slot("SLOT-204", "DOCK-JPR-D1", dt(17, 0), dt(17, 40), status="open")  # occupied by unloading via appointment

    # Ravi original 17:30-18:00 on Dock 04 (PDF SLOT-1938)
    add_slot("SLOT-1938", "DOCK-JPR-04", dt(17, 30), dt(18, 0))

    # Scarce evening alternatives (compatible dry van) — intentionally few
    add_slot("SLOT-EVE-1", "DOCK-JPR-D2", dt(18, 0), dt(18, 30))
    add_slot("SLOT-EVE-2", "DOCK-JPR-04", dt(19, 0), dt(19, 40))
    add_slot("SLOT-EVE-3", "DOCK-JPR-D1", dt(19, 30), dt(20, 10))
    add_slot("SLOT-EVE-4", "DOCK-JPR-05", dt(20, 0), dt(20, 40))
    # Later safer options for Ravi after 19:10 ETA
    add_slot("SLOT-EVE-5", "DOCK-JPR-04", dt(19, 30), dt(20, 10))
    add_slot("SLOT-EVE-6", "DOCK-JPR-D2", dt(20, 0), dt(20, 30))
    # Capacity reduction target
    add_slot("SLOT-BLOCK-1", "DOCK-JPR-05", dt(18, 30), dt(19, 10))
    # Infeasible early slot (before ETAs)
    add_slot("SLOT-EARLY", "DOCK-JPR-D2", dt(17, 30), dt(18, 0))
    # Reefer-only (incompatible for dry vans in competition)
    add_slot("SLOT-REEFER", "DOCK-JPR-REEFER", dt(19, 0), dt(19, 40))

    # Evening competitors originally booked early slots that become infeasible
    appointments: list[Appointment] = []
    for i in range(10):
        sid = f"SLOT-COMP-{i:02d}"
        add_slot(sid, "DOCK-JPR-04" if i % 2 == 0 else "DOCK-JPR-D2", dt(17, 30), dt(18, 0))
        appointments.append(
            Appointment(
                appointment_id=f"APT-EVE-{i:02d}",
                shipment_id=f"SHP-EVE-{i:02d}",
                slot_id=sid,
                status="confirmed",
                booked_at=dt(9, 0),
                confirmed_at=dt(9, 0),
            )
        )

    db.add_all(slots)

    appointments.extend(
        [
            Appointment(
                appointment_id="APT-552",
                shipment_id="SHP-1042",
                slot_id="SLOT-1938",
                status="confirmed",
                booked_at=dt(9, 0),
                confirmed_at=dt(9, 0),
            ),
            Appointment(
                appointment_id="APT-201",
                shipment_id="SHP-201",
                slot_id="SLOT-201",
                status="confirmed",
                booked_at=dt(8, 0),
                confirmed_at=dt(8, 0),
            ),
            Appointment(
                appointment_id="APT-202",
                shipment_id="SHP-202",
                slot_id="SLOT-202",
                status="confirmed",
                booked_at=dt(8, 0),
                confirmed_at=dt(8, 0),
            ),
            Appointment(
                appointment_id="APT-203",
                shipment_id="SHP-203",
                slot_id="SLOT-203",
                status="confirmed",
                booked_at=dt(8, 0),
                confirmed_at=dt(8, 0),
            ),
            Appointment(
                appointment_id="APT-204",
                shipment_id="SHP-204",
                slot_id="SLOT-204",
                status="confirmed",
                booked_at=dt(8, 0),
                confirmed_at=dt(8, 0),
            ),
            Appointment(
                appointment_id="APT-MULTI-A",
                shipment_id="SHP-MULTI-A",
                slot_id="SLOT-EVE-1",
                status="confirmed",
                booked_at=dt(10, 0),
                confirmed_at=dt(10, 0),
            ),
            Appointment(
                appointment_id="APT-NOP",
                shipment_id="SHP-NOP",
                slot_id="SLOT-EVE-2",
                status="confirmed",
                booked_at=dt(10, 0),
                confirmed_at=dt(10, 0),
            ),
        ]
    )
    db.add_all(appointments)

    db.add(
        Contact(
            contact_id="CON-JPR",
            party_type="warehouse",
            name="Jaipur Planner",
            email="planner.jpr@setuhaul.example",
            phone="+911412000001",
            facility_id="FAC-JPR-01",
        )
    )

    db.commit()
    if own_session:
        db.close()


if __name__ == "__main__":
    seed()
    print("Seed complete.")
