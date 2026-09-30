"""时序数据读写：读数入库、状态快照、区间差值。"""
from datetime import datetime
from sqlalchemy import select, dialects
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import Reading, DeviceState


# 各设备类型的「累计总表」指标
TOTALIZER_METRICS = {
    "ac": "energy_kwh",
    "heater": "energy_kwh",
    "pv": "gen_kwh",
    "tank": "out_l",
}


def insert_reading(db: Session, device_id: int, ts: datetime, metric: str, value: float):
    """写入一条读数并 upsert 最新状态快照。"""
    db.add(Reading(device_id=device_id, ts=ts, metric=metric, value=float(value)))
    db.execute(
        sqlite_insert(DeviceState)
        .values(device_id=device_id, metric=metric, value=float(value), ts=ts)
        .on_conflict_do_update(
            index_elements=["device_id", "metric"],
            set_={"value": float(value), "ts": ts},
        )
    )


def state_map(db: Session, device_id: int) -> dict[str, DeviceState]:
    return {s.metric: s for s in db.scalars(select(DeviceState).where(DeviceState.device_id == device_id))}


def state_value(db: Session, device_id: int, metric: str, default: float | None = None) -> float | None:
    s = db.scalar(
        select(DeviceState).where(DeviceState.device_id == device_id, DeviceState.metric == metric)
    )
    return s.value if s else default


def _value_at(db: Session, device_id: int, metric: str, at: datetime, *, after: bool) -> float | None:
    stmt = select(Reading.value).where(
        Reading.device_id == device_id, Reading.metric == metric
    )
    stmt = stmt.where(Reading.ts <= at).order_by(Reading.ts.desc()) if not after else \
           stmt.where(Reading.ts >= at).order_by(Reading.ts.asc())
    return db.scalar(stmt.limit(1))


def delta_total(db: Session, device_id: int, metric: str, start: datetime, end: datetime) -> float:
    """累计总表在 [start, end] 区间的增量；端点缺读数时向边界外取最近一条。"""
    v_start = _value_at(db, device_id, metric, start, after=False)
    if v_start is None:
        v_start = _value_at(db, device_id, metric, start, after=True)
    v_end = _value_at(db, device_id, metric, end, after=False)
    if v_end is None:
        v_end = _value_at(db, device_id, metric, end, after=True)
    if v_start is None or v_end is None:
        return 0.0
    return max(0.0, v_end - v_start)


def series(db: Session, device_id: int, metric: str, start: datetime, end: datetime, limit: int = 500):
    rows = db.execute(
        select(Reading.ts, Reading.value)
        .where(Reading.device_id == device_id, Reading.metric == metric,
               Reading.ts >= start, Reading.ts <= end)
        .order_by(Reading.ts.asc())
    ).all()
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return [(r[0], r[1]) for r in rows]


def mean_value(db: Session, device_id: int, metric: str, start: datetime, end: datetime) -> float | None:
    rows = db.scalars(
        select(Reading.value).where(
            Reading.device_id == device_id, Reading.metric == metric,
            Reading.ts >= start, Reading.ts <= end
        )
    ).all()
    return sum(rows) / len(rows) if rows else None


def active_bookings(db: Session, room_id: int, at: datetime):
    from ..models import Booking
    return db.scalars(
        select(Booking).where(
            Booking.room_id == room_id,
            Booking.check_in <= at,
            Booking.check_out > at,
        )
    ).all()


def occupied_room_count(db: Session, property_id: int, at: datetime) -> int:
    from ..models import Booking, Room
    return len(
        db.execute(
            select(Booking.id)
            .join(Room, Room.id == Booking.room_id)
            .where(
                Room.property_id == property_id,
                Booking.check_in <= at,
                Booking.check_out > at,
            )
        ).all()
    )
