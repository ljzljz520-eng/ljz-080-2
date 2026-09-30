"""异常告警：查询、确认、解除、立即重评。"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Alert, AlertStatus, Device
from ..schemas import AlertOut
from ..services.alerts import evaluate_device, evaluate_all, RULE_LABELS

router = APIRouter(prefix="/api/alerts", tags=["告警"])


@router.get("", response_model=list[AlertOut])
def list_alerts(status: str | None = None, device_id: int | None = None,
                limit: int = 100, db: Session = Depends(get_db)):
    stmt = select(Alert).order_by(Alert.last_seen.desc()).limit(limit)
    if status:
        try:
            stmt = stmt.where(Alert.status == AlertStatus(status))
        except ValueError:
            raise HTTPException(400, "status 必须是 open/ack/resolved")
    if device_id:
        stmt = stmt.where(Alert.device_id == device_id)
    return list(db.scalars(stmt))


@router.get("/rules")
def rule_catalog():
    return {"rules": [{"rule": k, "label": v} for k, v in RULE_LABELS.items()]}


@router.post("/{alert_id}/ack", response_model=AlertOut)
def ack_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "告警不存在")
    a.status = AlertStatus.ACK
    db.commit()
    db.refresh(a)
    return a


@router.post("/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "告警不存在")
    a.status = AlertStatus.RESOLVED
    a.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(a)
    return a


@router.post("/evaluate", status_code=202)
def reevaluate(device_id: int | None = None, db: Session = Depends(get_db)):
    if device_id is not None:
        d = db.get(Device, device_id)
        if d is None:
            raise HTTPException(404, "设备不存在")
        res = evaluate_device(db, d, datetime.utcnow())
    else:
        res = evaluate_all(db)
    db.commit()
    return {"active_alerts": len(res)}
