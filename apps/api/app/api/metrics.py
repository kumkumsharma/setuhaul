from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import metrics as metrics_svc

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/summary")
def metrics_summary(db: Session = Depends(get_db)):
    return metrics_svc.summary(db)
