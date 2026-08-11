from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ChatRequest, ChatResponse
from app.services.chat import handle_chat

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, db: Session = Depends(get_db)):
    result = handle_chat(
        db,
        driver_id=body.driver_id,
        message=body.message,
        exception_id=body.exception_id,
        shipment_id=body.shipment_id,
        idempotency_key=body.idempotency_key,
    )
    return result
