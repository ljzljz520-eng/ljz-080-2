"""设备遥测接入与查询。"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Device
from ..schemas import ReadingIn, ReadingBatchIn, ReadingOut
from ..services.ingest import ingest_reading

router = APIRouter(prefix="/api/telemetry", tags=["设备接入"])


@router.post("/readings", status_code=202)
def post_reading(payload: ReadingIn, db: Session = Depends(get_db)):
    try:
        device = ingest_reading(db, payload.device_code, payload.metric, payload.value, payload.ts)
    except LookupError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {"accepted": 1, "device_id": device.id}


@router.post("/readings/batch", status_code=202)
def post_readings(payload: ReadingBatchIn, db: Session = Depends(get_db)):
    accepted = 0
    for r in payload.readings:
        try:
            ingest_reading(db, r.device_code, r.metric, r.value, r.ts, evaluate=False)
            accepted += 1
        except (LookupError, ValueError) as e:
            raise HTTPException(400, f"第 {accepted + 1} 条数据非法: {e}")
    db.commit()
    return {"accepted": accepted, "total": len(payload.readings)}


@router.get("/devices/{device_id}/series", response_model=list[ReadingOut])
def device_series(device_id: int, metric: str, start: datetime, end: datetime,
                  limit: int = 500, db: Session = Depends(get_db)):
    if db.get(Device, device_id) is None:
        raise HTTPException(404, "设备不存在")
    from ..services import timeseries as ts
    return [ReadingOut(ts=t, metric=metric, value=v) for t, v in
            ts.series(db, device_id, metric, start, end, limit)]
