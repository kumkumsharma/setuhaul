from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Facility, SchedulingRun
from app.services.observability import log_event
from app.services.scheduler import propose_schedule, run_to_dict

router = APIRouter(prefix="/api/scheduling", tags=["scheduling"])


@router.post("/facilities/{facility_id}/run")
def run_facility_schedule(facility_id: str, db: Session = Depends(get_db)):
    facility = db.get(Facility, facility_id)
    if not facility:
        raise HTTPException(404, "Facility not found")
    run = propose_schedule(db, facility_id)
    log_event(
        "scheduling_run",
        facility_id=facility_id,
        run_id=run.run_id,
        status="ok",
    )
    return run_to_dict(run)


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(SchedulingRun, run_id)
    if not run:
        raise HTTPException(404, "Scheduling run not found")
    return run_to_dict(run)


@router.get("/facilities/{facility_id}/runs")
def list_runs(facility_id: str, db: Session = Depends(get_db), limit: int = 10):
    rows = (
        db.query(SchedulingRun)
        .filter(SchedulingRun.facility_id == facility_id)
        .order_by(SchedulingRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [run_to_dict(r) for r in rows]
