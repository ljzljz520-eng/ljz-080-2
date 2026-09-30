"""管家服务：入住预冷看板、设备遥控指令。"""
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import TIMEZONE, DEFAULT_PRECOOL_LEAD_MIN, DEFAULT_PRECOOL_TARGET_C
from ..models import (
    Device, DeviceType, Booking, Room, Property, Alert, AlertStatus, DeviceCommand, CommandStatus,
)
from . import timeseries as ts
from ..schemas import PrecoolItem, TankStatus


def _local_day_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    from datetime import timezone
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(TIMEZONE)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def today_checkins(db: Session, now: datetime) -> list[Booking]:
    start, end = _local_day_bounds(now)
    return list(db.scalars(
        select(Booking).where(Booking.check_in >= start, Booking.check_in < end)
        .order_by(Booking.check_in.asc())
    ))


def room_ac_devices(db: Session, room_id: int) -> list[Device]:
    return list(db.scalars(
        select(Device).where(Device.room_id == room_id, Device.type == DeviceType.AC, Device.active.is_(True))
    ))


def precool_status(db: Session, booking: Booking, now: datetime) -> PrecoolItem:
    room = db.get(Room, booking.room_id)
    prop = db.get(Property, room.property_id)
    acs = room_ac_devices(db, booking.room_id)

    on_temps, targets, ac_on = [], [], False
    for ac in acs:
        p = ts.state_value(db, ac.id, "power_kw")
        if p is not None and p > 0.1:
            ac_on = True
        rt = ts.state_value(db, ac.id, "room_temp")
        if rt is not None:
            on_temps.append(rt)
        tg = ts.state_value(db, ac.id, "target_temp")
        if tg is not None:
            targets.append(tg)

    room_temp = round(sum(on_temps) / len(on_temps), 1) if on_temps else None
    target = round(sum(targets) / len(targets), 1) if targets else None

    if not booking.precool_requested:
        status = "not_requested"
    elif room_temp is not None and ac_on and room_temp <= DEFAULT_PRECOOL_TARGET_C + 1:
        status = "done"
    elif ac_on:
        status = "cooling"
    elif now >= booking.check_in:
        status = "missed"
    else:
        status = "pending"

    return PrecoolItem(
        booking_id=booking.id, code=booking.code, guest_name=booking.guest_name,
        check_in=booking.check_in, room_id=room.id, room_name=room.name,
        property_name=prop.name, guest_count=booking.guest_count,
        precool_requested=booking.precool_requested, status=status,
        target_temp=target, room_temp=room_temp, ac_on=ac_on,
    )


def dashboard(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    items = [precool_status(db, b, now) for b in today_checkins(db, now)]
    pending = [i for i in items if i.status in ("pending", "cooling", "missed")]
    done = sum(1 for i in items if i.status == "done")

    open_alerts = list(db.scalars(
        select(Alert).where(Alert.status != AlertStatus.RESOLVED)
        .order_by(Alert.last_seen.desc()).limit(50)
    ))

    tanks = []
    for t in db.scalars(select(Device).where(Device.type == DeviceType.TANK, Device.active.is_(True))):
        tanks.append(TankStatus(
            device_id=t.id, property_id=t.property_id,
            property_name=db.get(Property, t.property_id).name,
            level_pct=ts.state_value(db, t.id, "level_pct", 0) or 0,
            inflow_lh=ts.state_value(db, t.id, "inflow_lh", 0) or 0,
            outflow_lh=ts.state_value(db, t.id, "outflow_lh", 0) or 0,
        ))

    return {
        "now": now, "today_arrivals": len(items), "precool_done": done,
        "precool_pending": pending, "open_alerts": open_alerts, "tank_status": tanks,
    }


def start_precool(db: Session, booking_id: int, target_c: float = DEFAULT_PRECOOL_TARGET_C,
                  now: datetime | None = None) -> int:
    """对预订房间的全部空调下发预冷指令；返回下发设备数。"""
    now = now or datetime.utcnow()
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise ValueError("预订不存在")
    if booking.checked_out_at or booking.check_out <= now:
        raise ValueError("预订已结束，无法预冷")
    count = 0
    for ac in room_ac_devices(db, booking.room_id):
        db.add(DeviceCommand(device_id=ac.id, command="precool",
                             params={"target_temp": target_c, "lead_min": DEFAULT_PRECOOL_LEAD_MIN}))
        count += 1
    if booking.precool_started_at is None:
        booking.precool_started_at = now
    db.flush()
    return count


def send_command(db: Session, device_id: int, command: str, params: dict) -> DeviceCommand:
    device = db.get(Device, device_id)
    if device is None:
        raise ValueError("设备不存在")
    if command not in ("turn_on", "turn_off", "set_target", "precool"):
        raise ValueError("不支持的指令")
    cmd = DeviceCommand(device_id=device_id, command=command, params=params or {})
    db.add(cmd)
    db.flush()
    return cmd
