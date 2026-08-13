"""Seed realistic classroom data aligned to SetuHaul PDF scenarios.

Scenario clock: 2026-08-11 17:25 IST (Jaipur evening contention snapshot).

Paths:
  - seed(db): used by tests; when called without a session, performs a
    destructive local reset (drop_all) — never use against RDS.
  - seed_demo(): production-safe insert-only path (no drop, no flushdb).
  - demo_seed_plan(): inventory of rows that seed_demo would insert.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine, init_db
from app.models import (
    Appointment,
    AppointmentSlot,
    BaselineMetric,
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
    """DESTRUCTIVE: drops all tables and flushes Redis. Local/tests only."""
    Base.metadata.drop_all(bind=engine)
    init_db()
    redis = rc.get_redis()
    try:
        redis.flushdb()
    except Exception:
        pass


def build_demo_entities() -> dict[str, list[Any]]:
    """Construct all demo ORM objects (not yet attached to a session)."""

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
    for fac, n in [
        ("FAC-DEL-01", 4),
        ("FAC-GUR-01", 3),
        ("FAC-MUM-01", 4),
        ("FAC-AMD-01", 3),
        ("FAC-PUN-01", 3),
    ]:
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

    facility_rules = [
        FacilityRule(
            rule_id="RULE-JPR-LEN",
            facility_id="FAC-JPR-01",
            rule_type="max_vehicle_length_ft",
            rule_value="32",
            effective_from=None,
            effective_to=None,
        ),
        FacilityRule(
            rule_id="RULE-JPR-DRY",
            facility_id="FAC-JPR-01",
            rule_type="allowed_product_class",
            rule_value="dry|fmcg|cold|perishable",
            effective_from="06:00",
            effective_to="23:00",
        ),
    ]

    drivers = [
        Driver(
            driver_id="DRV-027",
            name="Ravi Kumar",
            phone="+919800000027",
            carrier_id="CAR-08",
            status="active",
            home_base="Neemrana",
        ),
        Driver(
            driver_id="DRV-201",
            name="Asha Verma",
            phone="+919800000201",
            carrier_id="CAR-01",
            status="active",
        ),
        Driver(
            driver_id="DRV-202",
            name="Imran Khan",
            phone="+919800000202",
            carrier_id="CAR-02",
            status="active",
        ),
        Driver(
            driver_id="DRV-203",
            name="Suresh Patel",
            phone="+919800000203",
            carrier_id="CAR-03",
            status="active",
        ),
        Driver(
            driver_id="DRV-204",
            name="Deepa Nair",
            phone="+919800000204",
            carrier_id="CAR-01",
            status="active",
        ),
        Driver(
            driver_id="DRV-MULTI",
            name="Karan Singh",
            phone="+919800000999",
            carrier_id="CAR-08",
            status="active",
        ),
        Driver(
            driver_id="DRV-NOP",
            name="No Slot Driver",
            phone="+919800000500",
            carrier_id="CAR-09",
            status="active",
        ),
        Driver(
            driver_id="DRV-HIPRI",
            name="Priority Late",
            phone="+919800000700",
            carrier_id="CAR-01",
            status="active",
        ),
    ]
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

    vehicles = [
        Vehicle(
            vehicle_id="VEH-031",
            carrier_id="CAR-08",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-201",
            carrier_id="CAR-01",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-202",
            carrier_id="CAR-02",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-203",
            carrier_id="CAR-03",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-204",
            carrier_id="CAR-01",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-M1",
            carrier_id="CAR-08",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-M2",
            carrier_id="CAR-08",
            vehicle_type="dry_van",
            length_ft=28,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-NOP",
            carrier_id="CAR-09",
            vehicle_type="dry_van",
            length_ft=40,
            refrigeration_required=False,
        ),
        Vehicle(
            vehicle_id="VEH-HIPRI",
            carrier_id="CAR-01",
            vehicle_type="dry_van",
            length_ft=32,
            refrigeration_required=False,
        ),
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

    shipments = [
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

    checkins = [
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

    slots: list[AppointmentSlot] = []

    def add_slot(
        slot_id: str,
        dock_id: str,
        start: datetime,
        end: datetime,
        status: str = "open",
    ) -> None:
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

    add_slot("SLOT-201", "DOCK-JPR-D1", dt(17, 30), dt(18, 10))
    add_slot("SLOT-202", "DOCK-JPR-D2", dt(17, 0), dt(17, 30))
    add_slot("SLOT-203", "DOCK-JPR-D1", dt(17, 45), dt(18, 30))
    add_slot("SLOT-204", "DOCK-JPR-D1", dt(17, 0), dt(17, 40), status="open")
    add_slot("SLOT-1938", "DOCK-JPR-04", dt(17, 30), dt(18, 0))
    add_slot("SLOT-EVE-1", "DOCK-JPR-D2", dt(18, 0), dt(18, 30))
    add_slot("SLOT-EVE-2", "DOCK-JPR-04", dt(19, 0), dt(19, 40))
    add_slot("SLOT-EVE-3", "DOCK-JPR-D1", dt(19, 30), dt(20, 10))
    add_slot("SLOT-EVE-4", "DOCK-JPR-05", dt(20, 0), dt(20, 40))
    add_slot("SLOT-EVE-5", "DOCK-JPR-04", dt(19, 30), dt(20, 10))
    add_slot("SLOT-EVE-6", "DOCK-JPR-D2", dt(20, 0), dt(20, 30))
    add_slot("SLOT-BLOCK-1", "DOCK-JPR-05", dt(18, 30), dt(19, 10))
    add_slot("SLOT-EARLY", "DOCK-JPR-D2", dt(17, 30), dt(18, 0))
    add_slot("SLOT-REEFER", "DOCK-JPR-REEFER", dt(19, 0), dt(19, 40))

    appointments: list[Appointment] = []
    for i in range(10):
        sid = f"SLOT-COMP-{i:02d}"
        add_slot(
            sid,
            "DOCK-JPR-04" if i % 2 == 0 else "DOCK-JPR-D2",
            dt(17, 30),
            dt(18, 0),
        )
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

    contacts = [
        Contact(
            contact_id="CON-JPR",
            party_type="warehouse",
            name="Jaipur Planner",
            email="planner.jpr@setuhaul.example",
            phone="+911412000001",
            facility_id="FAC-JPR-01",
        )
    ]

    baselines = [
        BaselineMetric(
            baseline_id="BASE-MANUAL-1",
            label="manual_coordinator",
            avg_resolution_minutes=18.0,
            human_help_rate=0.7,
            avg_eta_error_minutes=29.0,
            sample_size=10,
            notes="Classroom baseline for similar evening delay cases before the solution.",
        )
    ]

    # FK-safe insert order
    return {
        "facilities": facilities,
        "docks": docks,
        "facility_rules": facility_rules,
        "drivers": drivers,
        "vehicles": vehicles,
        "shipments": shipments,
        "eta_updates": etas,
        "facility_checkins": checkins,
        "appointment_slots": slots,
        "appointments": appointments,
        "contacts": contacts,
        "baseline_metrics": baselines,
    }


_PK_ATTR = {
    "facilities": "facility_id",
    "docks": "dock_id",
    "facility_rules": "rule_id",
    "drivers": "driver_id",
    "vehicles": "vehicle_id",
    "shipments": "shipment_id",
    "eta_updates": "eta_update_id",
    "facility_checkins": "checkin_id",
    "appointment_slots": "slot_id",
    "appointments": "appointment_id",
    "contacts": "contact_id",
    "baseline_metrics": "baseline_id",
}


def demo_seed_plan() -> dict[str, Any]:
    """Exact inventory seed_demo would attempt to insert (no DB writes)."""
    entities = build_demo_entities()
    plan: dict[str, Any] = {
        "destructive": False,
        "drops_tables": False,
        "flushes_redis": False,
        "scenario_day": DAY.date().isoformat(),
        "tables": {},
        "highlights": {
            "primary_driver": "DRV-027 Ravi Kumar / SHP-1042 → FAC-JPR-01 / APT-552 on SLOT-1938",
            "multi_shipment": "DRV-MULTI → SHP-MULTI-A, SHP-MULTI-B",
            "no_feasible": "DRV-NOP / SHP-NOP (40ft vehicle vs 32ft Jaipur rule)",
            "evening_slots": [
                "SLOT-EVE-1",
                "SLOT-EVE-2",
                "SLOT-EVE-3",
                "SLOT-EVE-4",
                "SLOT-EVE-5",
                "SLOT-EVE-6",
            ],
        },
    }
    total = 0
    for name, rows in entities.items():
        pk = _PK_ATTR[name]
        ids = [getattr(r, pk) for r in rows]
        plan["tables"][name] = {"count": len(ids), "ids": ids}
        total += len(ids)
    plan["total_rows"] = total
    plan["not_inserted"] = [
        "driver_exceptions",
        "chat_messages",
        "option_views",
        "slot_holds",
        "location_shares",
        "route_etas",
        "scheduling_runs",
        "case_metrics",
    ]
    return plan


def populate_demo_data(db: Session, *, skip_existing: bool = False) -> dict[str, int]:
    """Insert demo rows. Never drops tables or flushes Redis."""
    entities = build_demo_entities()
    inserted: dict[str, int] = {}
    skipped: dict[str, int] = {}

    for name, rows in entities.items():
        pk_attr = _PK_ATTR[name]
        model = type(rows[0])
        n_ins = 0
        n_skip = 0
        for row in rows:
            pk = getattr(row, pk_attr)
            if skip_existing and db.get(model, pk) is not None:
                n_skip += 1
                continue
            db.add(row)
            n_ins += 1
        inserted[name] = n_ins
        skipped[name] = n_skip

    db.commit()
    return {"inserted": inserted, "skipped": skipped}  # type: ignore[return-value]


def seed(db: Session | None = None) -> None:
    """Test/local helper. Destructive reset only when called without a session."""
    own_session = db is None
    if own_session:
        reset_database()
        db = SessionLocal()
    assert db is not None
    populate_demo_data(db, skip_existing=False)
    if own_session:
        db.close()


def seed_demo(*, skip_existing: bool = True) -> dict[str, Any]:
    """Production-safe demo seed: insert-only, skip existing PKs, no drop/flush."""
    db = SessionLocal()
    try:
        result = populate_demo_data(db, skip_existing=skip_existing)
        return {
            "ok": True,
            "destructive": False,
            "skip_existing": skip_existing,
            **result,
        }
    finally:
        db.close()


def _print_plan(plan: dict[str, Any]) -> None:
    print("=== seed_demo plan (no writes) ===")
    print(f"destructive={plan['destructive']} drops_tables={plan['drops_tables']} flushes_redis={plan['flushes_redis']}")
    print(f"scenario_day={plan['scenario_day']} total_rows={plan['total_rows']}")
    print("\nHighlights:")
    for k, v in plan["highlights"].items():
        print(f"  - {k}: {v}")
    print("\nTables to insert:")
    for name, info in plan["tables"].items():
        print(f"  {name}: {info['count']}")
        print(f"    ids: {', '.join(info['ids'])}")
    print("\nTables NOT seeded (created at runtime by the app):")
    for t in plan["not_inserted"]:
        print(f"  - {t}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SetuHaul demo data loader")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print exact seed_demo inventory and exit (no DB writes)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Insert-only seed_demo (no drop_all / no Redis flush)",
    )
    parser.add_argument(
        "--destructive-reset",
        action="store_true",
        help="LOCAL ONLY: drop_all + flush Redis + full seed",
    )
    args = parser.parse_args()

    if args.plan:
        _print_plan(demo_seed_plan())
    elif args.demo:
        summary = seed_demo(skip_existing=True)
        print("seed_demo complete:", summary)
    elif args.destructive_reset:
        seed()
        print("Destructive seed complete.")
    else:
        parser.error("Choose --plan, --demo, or --destructive-reset")
