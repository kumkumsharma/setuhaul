from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    AppointmentOut,
    DriverOut,
    ExceptionCreate,
    ExceptionOut,
    FacilityOut,
    ShipmentOut,
)
from app.services import domain
from app.services.feasibility import current_appointment
from app.models import Facility
import json

router = APIRouter(prefix="/api", tags=["domain"])


@router.get("/drivers/{driver_id}", response_model=DriverOut)
def read_driver(driver_id: str, db: Session = Depends(get_db)):
    driver = domain.get_driver(db, driver_id)
    if not driver:
        raise HTTPException(404, "Driver not found")
    return driver


@router.get("/drivers/{driver_id}/shipments", response_model=list[ShipmentOut])
def read_driver_shipments(driver_id: str, db: Session = Depends(get_db)):
    shipments = domain.list_active_shipments(db, driver_id)
    return [domain.shipment_to_dict(db, s) for s in shipments]


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut)
def read_shipment(shipment_id: str, db: Session = Depends(get_db)):
    shipment = domain.get_shipment(db, shipment_id)
    if not shipment:
        raise HTTPException(404, "Shipment not found")
    return domain.shipment_to_dict(db, shipment)


@router.get("/shipments/{shipment_id}/appointment", response_model=AppointmentOut | None)
def read_appointment(shipment_id: str, db: Session = Depends(get_db)):
    appt = current_appointment(db, shipment_id)
    return domain.appointment_to_dict(db, appt)


@router.get("/facilities/{facility_id}", response_model=FacilityOut)
def read_facility(facility_id: str, db: Session = Depends(get_db)):
    facility = db.get(Facility, facility_id)
    if not facility:
        raise HTTPException(404, "Facility not found")
    return facility


@router.post("/exceptions", response_model=ExceptionOut)
def create_exception(body: ExceptionCreate, db: Session = Depends(get_db)):
    shipment_id = body.shipment_id
    if not shipment_id:
        shipments = domain.list_active_shipments(db, body.driver_id)
        if len(shipments) != 1:
            raise HTTPException(400, "shipment_id required when driver has 0 or many active shipments")
        shipment_id = shipments[0].shipment_id
    exc = domain.create_exception(
        db,
        driver_id=body.driver_id,
        shipment_id=shipment_id,
        exception_type=body.exception_type,
        reported_delay_minutes=body.reported_delay_minutes,
        latest_declared_eta=body.latest_declared_eta,
        message=body.message,
    )
    return ExceptionOut(
        exception_id=exc.exception_id,
        driver_id=exc.driver_id,
        shipment_id=exc.shipment_id,
        exception_type=exc.exception_type,
        reported_delay_minutes=exc.reported_delay_minutes,
        latest_declared_eta=exc.latest_declared_eta,
        reported_at=exc.reported_at,
        status=exc.status,  # type: ignore[arg-type]
        conversation_state=json.loads(exc.conversation_state or "{}"),
    )


@router.get("/exceptions/{exception_id}", response_model=ExceptionOut)
def read_exception(exception_id: str, db: Session = Depends(get_db)):
    from app.models import DriverException

    exc = db.get(DriverException, exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    return ExceptionOut(
        exception_id=exc.exception_id,
        driver_id=exc.driver_id,
        shipment_id=exc.shipment_id,
        exception_type=exc.exception_type,
        reported_delay_minutes=exc.reported_delay_minutes,
        latest_declared_eta=exc.latest_declared_eta,
        reported_at=exc.reported_at,
        status=exc.status,  # type: ignore[arg-type]
        conversation_state=json.loads(exc.conversation_state or "{}"),
    )
