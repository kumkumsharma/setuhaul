from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Driver(Base):
    __tablename__ = "drivers"

    driver_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32))
    carrier_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active")
    home_base: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    shipments: Mapped[list["Shipment"]] = relationship(back_populates="driver")


class Vehicle(Base):
    __tablename__ = "vehicles"

    vehicle_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    carrier_id: Mapped[str] = mapped_column(String(32))
    vehicle_type: Mapped[str] = mapped_column(String(64))
    length_ft: Mapped[int] = mapped_column(Integer)
    refrigeration_required: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")


class Facility(Base):
    __tablename__ = "facilities"

    facility_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    open_time: Mapped[str] = mapped_column(String(8))  # HH:MM
    close_time: Mapped[str] = mapped_column(String(8))
    contact_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    docks: Mapped[list["Dock"]] = relationship(back_populates="facility")


class Dock(Base):
    __tablename__ = "docks"

    dock_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    dock_name: Mapped[str] = mapped_column(String(64))
    supported_vehicle_type: Mapped[str] = mapped_column(String(64))
    supported_product_class: Mapped[str] = mapped_column(String(64))
    max_length_ft: Mapped[int] = mapped_column(Integer, default=32)
    active_flag: Mapped[bool] = mapped_column(Boolean, default=True)

    facility: Mapped[Facility] = relationship(back_populates="docks")
    slots: Mapped[list["AppointmentSlot"]] = relationship(back_populates="dock")


class Shipment(Base):
    __tablename__ = "shipments"

    shipment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.driver_id"))
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.vehicle_id"))
    origin_id: Mapped[str] = mapped_column(String(32))
    destination_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    product_class: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1=highest
    planned_eta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expected_unload_minutes: Mapped[int] = mapped_column(Integer, default=40)
    status: Mapped[str] = mapped_column(String(32), default="in_transit")
    leave_by: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    driver: Mapped[Driver] = relationship(back_populates="shipments")
    vehicle: Mapped[Vehicle] = relationship()
    destination: Mapped[Facility] = relationship()
    eta_updates: Mapped[list["EtaUpdate"]] = relationship(back_populates="shipment")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="shipment")
    checkins: Mapped[list["FacilityCheckin"]] = relationship(back_populates="shipment")
    exceptions: Mapped[list["DriverException"]] = relationship(back_populates="shipment")


class EtaUpdate(Base):
    __tablename__ = "eta_updates"

    eta_update_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"))
    declared_eta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_type: Mapped[str] = mapped_column(String(32))  # driver | operations | planned
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confidence_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    shipment: Mapped[Shipment] = relationship(back_populates="eta_updates")


class FacilityCheckin(Base):
    __tablename__ = "facility_checkins"

    checkin_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"))
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    gate_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    arrival_status: Mapped[str] = mapped_column(String(32), default="en_route")
    queue_status: Mapped[str] = mapped_column(String(32), default="none")
    dock_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_finish_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    shipment: Mapped[Shipment] = relationship(back_populates="checkins")


class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"

    slot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    dock_id: Mapped[str] = mapped_column(ForeignKey("docks.dock_id"))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity_units: Mapped[int] = mapped_column(Integer, default=1)
    slot_status: Mapped[str] = mapped_column(String(32), default="open")  # open|blocked|closed

    dock: Mapped[Dock] = relationship(back_populates="slots")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="slot")


class Appointment(Base):
    __tablename__ = "appointments"

    appointment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"))
    slot_id: Mapped[str] = mapped_column(ForeignKey("appointment_slots.slot_id"))
    status: Mapped[str] = mapped_column(String(32), default="confirmed")
    # confirmed | cancelled | pending | superseded
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    shipment: Mapped[Shipment] = relationship(back_populates="appointments")
    slot: Mapped[AppointmentSlot] = relationship(back_populates="appointments")


class FacilityRule(Base):
    __tablename__ = "facility_rules"

    rule_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    facility_id: Mapped[str] = mapped_column(ForeignKey("facilities.facility_id"))
    rule_type: Mapped[str] = mapped_column(String(64))
    rule_value: Mapped[str] = mapped_column(String(255))
    effective_from: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # HH:MM
    effective_to: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)


class DriverException(Base):
    __tablename__ = "driver_exceptions"

    exception_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.driver_id"))
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"))
    exception_type: Mapped[str] = mapped_column(String(64))
    reported_delay_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latest_declared_eta: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="open")
    # open | awaiting_choice | held | pending | confirmed | escalated | closed
    conversation_state: Mapped[str] = mapped_column(Text, default="{}")

    shipment: Mapped[Shipment] = relationship(back_populates="exceptions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="exception")
    option_views: Mapped[list["OptionView"]] = relationship(back_populates="exception")
    holds: Mapped[list["SlotHold"]] = relationship(back_populates="exception")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(32))
    exception_id: Mapped[str] = mapped_column(ForeignKey("driver_exceptions.exception_id"))
    sender_type: Mapped[str] = mapped_column(String(32))  # driver | agent | system
    message_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    exception: Mapped[DriverException] = relationship(back_populates="messages")


class OptionView(Base):
    """Explicit 'shown' state — showing is NOT a reservation."""

    __tablename__ = "option_views"

    view_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exception_id: Mapped[str] = mapped_column(ForeignKey("driver_exceptions.exception_id"))
    slot_id: Mapped[str] = mapped_column(ForeignKey("appointment_slots.slot_id"))
    shown_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rank: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="shown")  # shown | stale
    reason_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    exception: Mapped[DriverException] = relationship(back_populates="option_views")
    slot: Mapped[AppointmentSlot] = relationship()


class SlotHold(Base):
    """DB mirror of Redis holds for audit; Redis is the concurrency authority."""

    __tablename__ = "slot_holds"
    __table_args__ = (UniqueConstraint("hold_id", name="uq_hold_id"),)

    hold_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exception_id: Mapped[str] = mapped_column(ForeignKey("driver_exceptions.exception_id"))
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.shipment_id"))
    slot_id: Mapped[str] = mapped_column(ForeignKey("appointment_slots.slot_id"))
    status: Mapped[str] = mapped_column(String(32), default="held")
    # held | pending | confirmed | released | expired | superseded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    exception: Mapped[DriverException] = relationship(back_populates="holds")
    slot: Mapped[AppointmentSlot] = relationship()


class Contact(Base):
    __tablename__ = "contacts"

    contact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    party_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    facility_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    shipment_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
