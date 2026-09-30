"""Checkout settlement: meter window aggregation, PV credit and bill persistence."""
from __future__ import annotations

from typing import Any

from ..storage import Database, dumps, loads
from ..timeutil import iso, parse_ts, utc_now
from .billing import allocatable_pv, average_setpoint, build_bill_totals, sum_positive_delta
from .telemetry import ValidationError


def _device_usage(db: Database, device: dict[str, Any], start, end) -> dict[str, Any]:
    rows = db.readings_between(device["id"], start, end)
    usage = {
        "device_id": device["id"], "device_name": device["name"], "type": device["type"],
        "electricity_kwh": round(sum_positive_delta(rows, "energy_kwh"), 3),
        "water_liters": round(sum_positive_delta(rows, "water_liters"), 1),
        "solar_generated_kwh": round(sum_positive_delta(rows, "solar_generated_kwh"), 3),
        "average_setpoint": average_setpoint(rows),
        "reading_count": len(rows),
    }
    return usage


def calculate_stay_usage(db: Database, booking: dict[str, Any]) -> dict[str, Any]:
    start, end = booking["check_in_at"], booking["check_out_at"]
    room_devices = db.query_all(
        "SELECT * FROM devices WHERE room_id=? AND type IN ('air_conditioner','water_heater') ORDER BY id",
        (booking["room_id"],),
    )
    details = [_device_usage(db, d, start, end) for d in room_devices]
    house_pvs = db.query_all(
        "SELECT * FROM devices WHERE house_id=? AND type='solar_pv' ORDER BY id",
        (booking["house_id"],),
    )
    pv_details = [_device_usage(db, d, start, end) for d in house_pvs]
    pv_generated = round(sum(d["solar_generated_kwh"] for d in pv_details), 3)
    electricity = round(sum(d["electricity_kwh"] for d in details), 3)
    water = round(sum(d["water_liters"] for d in details), 1)
    pv_credit = allocatable_pv(pv_generated, electricity)
    setpoints = [d["average_setpoint"] for d in details if d["average_setpoint"] is not None]
    avg_setpoint = round(sum(setpoints) / len(setpoints), 2) if setpoints else None
    anomalies = db.query_all(
        """
        SELECT COUNT(*) AS n FROM anomalies a JOIN devices d ON d.id=a.device_id
         WHERE d.room_id=? AND a.last_seen >= ? AND a.last_seen < ?
           AND a.acknowledged=0 AND a.resolved=0
        """,
        (booking["room_id"], iso(start), iso(end)),
    )[0]["n"]
    return {
        "electricity_kwh": electricity,
        "water_liters": water,
        "pv_generated_kwh": pv_generated,
        "pv_self_consumed_kwh": pv_credit,
        "average_setpoint": avg_setpoint,
        "open_anomaly_count": int(anomalies),
        "details": details + pv_details,
    }


def bill_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "recommendations": loads(row.get("recommendations_json", "[]"), []),
        "details": loads(row.get("details_json", "[]"), []),
    }


def checkout(db: Database, booking_id: int, check_out_at: str | None = None) -> dict[str, Any]:
    booking = db.booking_detail(booking_id)
    if not booking:
        raise KeyError(f"booking {booking_id} does not exist")
    existing = db.query_one("SELECT * FROM bills WHERE booking_id=?", (booking_id,))
    if existing:
        return bill_to_dict(existing)
    if check_out_at:
        booking["check_out_at"] = parse_ts(check_out_at)
    if booking["check_in_at"] >= booking["check_out_at"]:
        raise ValidationError("离店时间必须晚于入住时间")

    usage = calculate_stay_usage(db, booking)
    stay_hours = (booking["check_out_at"] - booking["check_in_at"]).total_seconds() / 3600
    totals = build_bill_totals(
        electricity_kwh=usage["electricity_kwh"],
        water_liters=usage["water_liters"],
        pv_self_consumed_kwh=usage["pv_self_consumed_kwh"],
        electricity_price=float(booking["electricity_price"]),
        water_price=float(booking["water_price"]),
        guest_count=int(booking["guest_count"]),
        stay_hours=stay_hours,
        ac_avg_setpoint=usage["average_setpoint"],
        open_anomaly_count=usage["open_anomaly_count"],
    )
    now = utc_now()
    db.begin()
    try:
        db.execute("UPDATE bookings SET status='checked_out', check_out_at=? WHERE id=?",
                   (iso(booking["check_out_at"]), booking_id))
        bill_id = db.insert("bills", {
            "booking_id": booking_id,
            "room_id": booking["room_id"],
            "house_id": booking["house_id"],
            "check_in_at": booking["check_in_at"],
            "check_out_at": booking["check_out_at"],
            "electricity_kwh": usage["electricity_kwh"],
            "water_liters": usage["water_liters"],
            "pv_self_consumed_kwh": usage["pv_self_consumed_kwh"],
            "electricity_fee": totals["electricity_fee"],
            "water_fee": totals["water_fee"],
            "total_fee": totals["total_fee"],
            "saved_fee": totals["saved_fee"],
            "recommendations_json": dumps(totals["recommendations"]),
            "details_json": dumps(usage["details"]),
            "created_at": now,
        })
        db.commit()
    except Exception:
        db.rollback()
        raise
    bill = db.get("bills", bill_id)
    result = bill_to_dict(bill)
    result["guest_name"] = booking["guest_name"]
    result["room_name"] = booking["room_name"]
    result["house_name"] = booking["house_name"]
    return result


def list_bills(db: Database, room_id: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT b.*, g.guest_name, r.name AS room_name, h.name AS house_name
          FROM bills b
          JOIN bookings g ON g.id=b.booking_id
          JOIN rooms r ON r.id=b.room_id
          JOIN houses h ON h.id=b.house_id
    """
    params: list[Any] = []
    if room_id:
        sql += " WHERE b.room_id=?"
        params.append(room_id)
    sql += " ORDER BY b.check_out_at DESC, b.id DESC"
    return [bill_to_dict(row) for row in db.query_all(sql, params)]


def get_bill(db: Database, bill_id: int) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT b.*, g.guest_name, r.name AS room_name, h.name AS house_name
          FROM bills b
          JOIN bookings g ON g.id=b.booking_id
          JOIN rooms r ON r.id=b.room_id
          JOIN houses h ON h.id=b.house_id
         WHERE b.id=?
        """,
        (bill_id,),
    )
    if not row:
        raise KeyError(f"bill {bill_id} does not exist")
    return bill_to_dict(row)
