"""预订管理与管家看板。"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Booking, Room, DeviceCommand
from ..schemas import BookingIn, BookingOut, Dashboard, CommandOut
from ..services import butler

router = APIRouter(prefix="/api", tags=["预订与管家"])


@router.post("/bookings", response_model=BookingOut, status_code=201)
def create_booking(payload: BookingIn, db: Session = Depends(get_db)):
    if db.get(Room, payload.room_id) is None:
        raise HTTPException(404, "房间不存在")
    if payload.check_out <= payload.check_in:
        raise HTTPException(400, "离店时间必须晚于入住时间")
    if db.scalar(select(Booking).where(Booking.code == payload.code)):
        raise HTTPException(409, "预订号已存在")
    overlap = db.scalar(select(Booking).where(
        Booking.room_id == payload.room_id,
        Booking.check_in < payload.check_out, Booking.check_out > payload.check_in,
    ))
    if overlap:
        raise HTTPException(409, f"该房间时间与预订 {overlap.code} 冲突")
    b = Booking(**payload.model_dump())
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@router.get("/bookings", response_model=list[BookingOut])
def list_bookings(active: bool = False, db: Session = Depends(get_db)):
    stmt = select(Booking).order_by(Booking.check_in.desc())
    if active:
        now = datetime.utcnow()
        stmt = stmt.where(Booking.check_in <= now, Booking.check_out > now)
    return list(db.scalars(stmt))


@router.get("/butler/dashboard", response_model=Dashboard)
def butler_dashboard(db: Session = Depends(get_db)):
    return butler.dashboard(db)


@router.post("/bookings/{booking_id}/precool", status_code=202)
def precool(booking_id: int, target_temp: float = 24, db: Session = Depends(get_db)):
    try:
        n = butler.start_precool(db, booking_id, target_temp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if n == 0:
        raise HTTPException(409, "该房间没有在线空调")
    db.commit()
    return {"dispatched": n, "booking_id": booking_id, "target_temp": target_temp}


@router.post("/devices/{device_id}/command", response_model=CommandOut, status_code=202)
def device_command(device_id: int, command: str, target_temp: float | None = None,
                   db: Session = Depends(get_db)):
    params = {}
    if target_temp is not None:
        params["target_temp"] = target_temp
    try:
        cmd = butler.send_command(db, device_id, command, params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(cmd)
    return cmd
