"""月度报表：按房源汇总用水用电、光伏自用率、间夜指标。"""
import csv
import io
from datetime import datetime, date, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Property, Room, Booking, Device, DeviceType, Bill, Alert, AlertStatus
from . import timeseries as ts


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """month 格式 YYYY-MM，返回 UTC 朴素时间边界（按日历月）。"""
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1)
    if m == 12:
        end = datetime(y + 1, 1, 1)
    else:
        end = datetime(y, m + 1, 1)
    return start, end


def _overlap_sec(a0, a1, b0, b1) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def property_month_row(db: Session, prop: Property, start: datetime, end: datetime) -> dict:
    devices = list(db.scalars(select(Device).where(
        Device.property_id == prop.id, Device.active.is_(True)
    )))
    ac_heater = [d for d in devices if d.type in (DeviceType.AC, DeviceType.HEATER)]
    pvs = [d for d in devices if d.type == DeviceType.PV]
    tanks = [d for d in devices if d.type == DeviceType.TANK]

    # 设备总表在该月的增量（含故障/暗漏等房东侧损耗）
    meter_elec_kwh = sum(ts.delta_total(db, d.id, "energy_kwh", start, end) for d in ac_heater)
    pv_kwh = sum(ts.delta_total(db, d.id, "gen_kwh", start, end) for d in pvs)
    meter_water_l = sum(ts.delta_total(db, d.id, "out_l", start, end) for d in tanks)
    meter_water_t = meter_water_l / 1000.0

    # 与本月有重叠的预订 → 间夜数（按重叠秒折算）、账单汇总
    bookings = list(db.scalars(
        select(Booking).join(Room, Room.id == Booking.room_id)
        .where(Room.property_id == prop.id, Booking.check_in < end, Booking.check_out > start)
    ))
    guest_nights = 0.0
    total_fee = 0.0
    billed_elec = 0.0
    billed_water = 0.0
    for b in bookings:
        secs = _overlap_sec(b.check_in, b.check_out, start, end)
        guest_nights += (secs / 86400.0) * b.guest_count
        bill = db.scalar(select(Bill).where(Bill.booking_id == b.id))
        if bill:
            total_fee += bill.total_fee
            # 账单完整落在本月才计入账单用量，避免跨月重复
            if start <= bill.period_start < end:
                billed_elec += bill.elec_kwh
                billed_water += bill.water_t

    gn = round(guest_nights)
    open_alerts = len(db.scalars(
        select(Alert).join(Device, Device.id == Alert.device_id)
        .where(Device.property_id == prop.id, Alert.status != AlertStatus.RESOLVED,
               Alert.last_seen >= start, Alert.last_seen < end)
    ).all())
    loss_elec = max(0.0, meter_elec_kwh - billed_elec)
    loss_water = max(0.0, meter_water_t - billed_water)

    return {
        "property_id": prop.id,
        "property_code": prop.code,
        "property_name": prop.name,
        "bookings": len(bookings),
        "guest_nights": gn,
        "elec_kwh": round(billed_elec, 1),                    # 计入账单的客人用电
        "meter_elec_kwh": round(meter_elec_kwh, 1),           # 抄表总用电（含损耗）
        "loss_elec_kwh": round(loss_elec, 1),
        "pv_kwh": round(pv_kwh, 1),
        "water_t": round(billed_water, 3),                    # 计入账单的客人用水
        "meter_water_t": round(meter_water_t, 3),             # 蓄水池总出水（含暗漏）
        "loss_water_t": round(loss_water, 3),
        "elec_per_night": round(billed_elec / gn, 2) if gn else 0.0,
        "water_per_night": round(billed_water / gn, 3) if gn else 0.0,
        "total_fee": round(total_fee, 2),
        "pv_self_ratio": round(min(1.0, meter_elec_kwh and pv_kwh / meter_elec_kwh), 3) if meter_elec_kwh else 0.0,
        "open_alerts": open_alerts,
    }


def monthly_report(db: Session, month: str) -> dict:
    start, end = month_bounds(month)
    props = list(db.scalars(select(Property).order_by(Property.id)))
    return {"month": month, "properties": [property_month_row(db, p, start, end) for p in props]}


def monthly_report_csv(db: Session, month: str) -> str:
    report = monthly_report(db, month)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["房源编号", "房源名称", "订单数", "人晚数", "客人用电kWh", "抄表总用电kWh",
                     "异常耗电kWh", "发电kWh", "客人用水吨", "蓄水池总出水吨", "漏损水吨",
                     "度/人晚", "吨/人晚", "光伏自用率", "账单总额(元)", "期间未恢复告警"])
    for r in report["properties"]:
        writer.writerow([r["property_code"], r["property_name"], r["bookings"], r["guest_nights"],
                         r["elec_kwh"], r["meter_elec_kwh"], r["loss_elec_kwh"], r["pv_kwh"],
                         r["water_t"], r["meter_water_t"], r["loss_water_t"],
                         r["elec_per_night"], r["water_per_night"], r["pv_self_ratio"],
                         r["total_fee"], r["open_alerts"]])
    return buf.getvalue()
