"""Chat flow tests — capacity always from tools, never invented."""

from app.models import Appointment


def test_chat_report_options_hold_confirm(client):
    r1 = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Tyre damaged near Neemrana. Can I get a slot after 7 PM?",
        },
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["exception_id"]
    assert "list_feasible_slots" in body["tools_used"] or body["options"]
    assert all(o["lifecycle"] in {"shown", "stale", "held"} for o in body["options"])
    # Must not invent slots — options come from engine
    for o in body["options"]:
        assert o["slot_id"].startswith("SLOT-")

    if not body["options"]:
        # If scarce/consumed, still must escalate rather than invent
        assert body["escalated"] is True
        return

    rank = body["options"][0]["rank"]
    r2 = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "exception_id": body["exception_id"],
            "shipment_id": "SHP-1042",
            "message": str(rank),
        },
    )
    assert r2.status_code == 200
    held = r2.json()
    assert held["status"] == "held"
    assert held["hold"]["status"] == "held"
    assert "create_hold" in held["tools_used"]

    r3 = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "exception_id": body["exception_id"],
            "shipment_id": "SHP-1042",
            "message": "confirm",
        },
    )
    assert r3.status_code == 200
    confirmed = r3.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["appointment"]["status"] == "confirmed"


def test_stale_option_when_other_driver_holds(client, db_session):
    # Driver A takes first available for 1042 path via API
    a = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need options after 7 PM",
        },
    ).json()
    if not a["options"]:
        return
    slot_id = a["options"][0]["slot_id"]
    client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "exception_id": a["exception_id"],
            "shipment_id": "SHP-1042",
            "message": "1",
        },
    )

    # Free competing driver ETA and try same slot
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.models import EtaUpdate
    from app.services import domain, allocator
    from app.services.allocator import AllocationError

    IST = ZoneInfo("Asia/Kolkata")
    db_session.add(
        EtaUpdate(
            eta_update_id="ETA-BATTLE",
            shipment_id="SHP-EVE-05",
            declared_eta=datetime(2026, 8, 11, 18, 0, tzinfo=IST),
            source_type="driver",
            declared_at=datetime(2026, 8, 11, 17, 25, tzinfo=IST),
        )
    )
    db_session.commit()
    exc = domain.create_exception(
        db_session, driver_id="DRV-EVE-05", shipment_id="SHP-EVE-05", message="compete"
    )
    try:
        allocator.create_hold(db_session, exception_id=exc.exception_id, slot_id=slot_id)
        assert False, "Should not steal active hold"
    except AllocationError as err:
        assert err.code == "slot_held"
