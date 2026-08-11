from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SlotLifecycle = Literal["shown", "held", "pending", "confirmed", "stale", "released", "expired"]
ExceptionStatus = Literal[
    "open",
    "awaiting_choice",
    "held",
    "pending",
    "confirmed",
    "escalated",
    "closed",
]


class DriverOut(BaseModel):
    driver_id: str
    name: str
    phone: str
    carrier_id: str
    status: str
    home_base: Optional[str] = None


class VehicleOut(BaseModel):
    vehicle_id: str
    vehicle_type: str
    length_ft: int
    refrigeration_required: bool
    status: str


class FacilityOut(BaseModel):
    facility_id: str
    name: str
    city: str
    open_time: str
    close_time: str


class ShipmentOut(BaseModel):
    shipment_id: str
    driver_id: str
    vehicle_id: str
    origin_id: str
    destination_id: str
    product_class: str
    priority: int
    planned_eta: datetime
    expected_unload_minutes: int
    status: str
    leave_by: Optional[datetime] = None
    latest_declared_eta: Optional[datetime] = None
    effective_eta: Optional[datetime] = None
    arrival_status: Optional[str] = None
    queue_status: Optional[str] = None


class AppointmentOut(BaseModel):
    appointment_id: str
    shipment_id: str
    slot_id: str
    status: str
    booked_at: datetime
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    slot_start: Optional[datetime] = None
    slot_end: Optional[datetime] = None
    dock_id: Optional[str] = None
    facility_id: Optional[str] = None
    is_feasible: Optional[bool] = None
    infeasible_reasons: list[str] = Field(default_factory=list)


class SlotOptionOut(BaseModel):
    slot_id: str
    facility_id: str
    dock_id: str
    dock_name: str
    start_time: datetime
    end_time: datetime
    rank: int
    lifecycle: SlotLifecycle = "shown"
    reasons: list[str] = Field(default_factory=list)
    buffer_minutes: Optional[int] = None


class ExceptionCreate(BaseModel):
    driver_id: str
    shipment_id: Optional[str] = None
    exception_type: str = "delay"
    reported_delay_minutes: Optional[int] = None
    latest_declared_eta: Optional[datetime] = None
    message: Optional[str] = None


class ExceptionOut(BaseModel):
    exception_id: str
    driver_id: str
    shipment_id: str
    exception_type: str
    reported_delay_minutes: Optional[int] = None
    latest_declared_eta: Optional[datetime] = None
    reported_at: datetime
    status: ExceptionStatus
    conversation_state: dict[str, Any] = Field(default_factory=dict)


class HoldCreate(BaseModel):
    exception_id: str
    slot_id: str
    idempotency_key: Optional[str] = None


class HoldOut(BaseModel):
    hold_id: str
    exception_id: str
    shipment_id: str
    slot_id: str
    status: SlotLifecycle
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None


class ConfirmOut(BaseModel):
    hold_id: str
    appointment_id: str
    status: Literal["confirmed"]
    slot_id: str
    shipment_id: str
    confirmed_at: datetime


class CancelOut(BaseModel):
    appointment_id: str
    status: Literal["cancelled"]
    cancelled_at: datetime
    freed_slot_id: str


class ChatRequest(BaseModel):
    driver_id: str
    message: str
    exception_id: Optional[str] = None
    shipment_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class ChatOptionCard(BaseModel):
    rank: int
    slot_id: str
    label: str
    lifecycle: SlotLifecycle
    start_time: datetime
    end_time: datetime
    dock_name: str
    buffer_minutes: Optional[int] = None


class ChatResponse(BaseModel):
    exception_id: str
    shipment_id: str
    reply: str
    status: ExceptionStatus
    options: list[ChatOptionCard] = Field(default_factory=list)
    hold: Optional[HoldOut] = None
    appointment: Optional[AppointmentOut] = None
    needs_shipment_choice: list[ShipmentOut] = Field(default_factory=list)
    escalated: bool = False
    tools_used: list[str] = Field(default_factory=list)
