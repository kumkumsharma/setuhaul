from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DriverException, Shipment
from app.schemas import AppointmentOut, ConfirmOut, CancelOut, HoldCreate, HoldOut, SlotOptionOut
from app.services import allocator, domain
from app.services.allocator import AllocationError

router = APIRouter(prefix="/api/allocation", tags=["allocation"])


class OptionsRequest(BaseModel):
    after: datetime | None = None
    limit: int = Field(default=5, ge=1, le=20)


@router.post("/exceptions/{exception_id}/options", response_model=list[SlotOptionOut])
def show_options(exception_id: str, body: OptionsRequest | None = None, db: Session = Depends(get_db)):
    body = body or OptionsRequest()
    exc = db.get(DriverException, exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    shipment = db.get(Shipment, exc.shipment_id)
    if not shipment:
        raise HTTPException(404, "Shipment not found")
    options = allocator.mark_options_shown(
        db, exc, shipment, after=body.after, limit=body.limit
    )
    return options


@router.post("/holds", response_model=HoldOut)
def create_hold(body: HoldCreate, db: Session = Depends(get_db)):
    try:
        hold = allocator.create_hold(
            db,
            exception_id=body.exception_id,
            slot_id=body.slot_id,
            idempotency_key=body.idempotency_key,
        )
    except AllocationError as exc:
        raise HTTPException(409 if exc.code in {"slot_held", "infeasible"} else 400, exc.message)
    return hold


@router.post("/holds/{hold_id}/confirm", response_model=ConfirmOut)
def confirm_hold(hold_id: str, idempotency_key: str | None = None, db: Session = Depends(get_db)):
    try:
        hold, appt = allocator.confirm_hold(db, hold_id, idempotency_key=idempotency_key)
    except AllocationError as exc:
        raise HTTPException(409, exc.message)
    return ConfirmOut(
        hold_id=hold.hold_id,
        appointment_id=appt.appointment_id,
        status="confirmed",
        slot_id=hold.slot_id,
        shipment_id=hold.shipment_id,
        confirmed_at=hold.confirmed_at or appt.confirmed_at,  # type: ignore[arg-type]
    )


@router.post("/holds/{hold_id}/release", response_model=HoldOut)
def release_hold(hold_id: str, db: Session = Depends(get_db)):
    try:
        hold = allocator.release_hold(db, hold_id)
    except AllocationError as exc:
        raise HTTPException(404, exc.message)
    return hold


@router.post("/appointments/{appointment_id}/cancel", response_model=CancelOut)
def cancel_appointment(appointment_id: str, db: Session = Depends(get_db)):
    try:
        appt = allocator.cancel_appointment(db, appointment_id)
    except AllocationError as exc:
        raise HTTPException(404, exc.message)
    return CancelOut(
        appointment_id=appt.appointment_id,
        status="cancelled",
        cancelled_at=appt.cancelled_at,  # type: ignore[arg-type]
        freed_slot_id=appt.slot_id,
    )


@router.get("/holds/{hold_id}", response_model=HoldOut)
def get_hold(hold_id: str, db: Session = Depends(get_db)):
    from app.models import SlotHold

    hold = db.get(SlotHold, hold_id)
    if not hold:
        raise HTTPException(404, "Hold not found")
    return hold


@router.get("/shipments/{shipment_id}/appointment", response_model=AppointmentOut | None)
def shipment_appointment(shipment_id: str, db: Session = Depends(get_db)):
    return domain.get_appointment_context(db, shipment_id)
