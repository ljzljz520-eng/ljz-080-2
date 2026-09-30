"""Monthly house/room energy and water comparison reports."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from typing import Any

from ..storage import Database
from ..timeutil import parse_ts


def month_bounds(year: int, month: int) -> tuple[datetime, datetime, datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_start = datetime(prev_year, prev_month, 1, tzinfo=timezone.utc)
    prev_end = start
    return start, end, prev_start, prev_end


def _aggregate_bills(db: Database, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bills = db.query_all(
        """
        SELECT b.*, h.name AS house_name, r.name AS room_name
          FROM bills b
          JOIN houses h ON h.id=b.house_id
          JOIN rooms r ON r.id=b.room_id
         WHERE b.check_out_at >= ? AND b.check_out_at < ?
         ORDER BY h.id, r.id, b.id
        """,
        (start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")),
    )
    houses: dict[int, dict[str, Any]] = {}
    rooms: dict[tuple[int, int], dict[str, Any]] = {}
    for bill in bills:
        check_in = parse_ts(bill["check_in_at"])
        check_out = parse_ts(bill["check_out_at"])
        stay_hours = max((check_out - check_in).total_seconds() / 3600, 1 / 24)
        booking = db.query_one("SELECT guest_count FROM bookings WHERE id=?", (bill["booking_id"],))
        guests = int(booking["guest_count"]) if booking else 1
        guest_nights = max(1, round(guests * stay_hours / 24, 2))
        house = houses.setdefault(bill["house_id"], {
            "house_id": bill["house_id"], "house_name": bill["house_name"],
            "bills": 0, "guest_nights": 0.0, "electricity_kwh": 0.0, "water_liters": 0.0,
            "pv_self_consumed_kwh": 0.0, "electricity_fee": 0.0, "water_fee": 0.0,
            "total_fee": 0.0, "saved_fee": 0.0,
        })
        room = rooms.setdefault((bill["house_id"], bill["room_id"]), {
            "house_id": bill["house_id"], "house_name": bill["house_name"],
            "room_id": bill["room_id"], "room_name": bill["room_name"],
            "bills": 0, "guest_nights": 0.0, "electricity_kwh": 0.0, "water_liters": 0.0,
            "pv_self_consumed_kwh": 0.0, "total_fee": 0.0, "saved_fee": 0.0,
        })
        for target in (house, room):
            target["bills"] += 1
            target["guest_nights"] += guest_nights
            target["electricity_kwh"] += float(bill["electricity_kwh"])
            target["water_liters"] += float(bill["water_liters"])
            target["pv_self_consumed_kwh"] += float(bill["pv_self_consumed_kwh"])
            target["total_fee"] += float(bill["total_fee"])
            target["saved_fee"] += float(bill["saved_fee"])
        house["electricity_fee"] += float(bill["electricity_fee"])
        house["water_fee"] += float(bill["water_fee"])
    return _finalize(list(houses.values()), list(rooms.values()))


def _finalize(houses: list[dict[str, Any]], rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in houses + rooms:
        nights = item["guest_nights"] or 1
        item["kwh_per_guest_night"] = round(item["electricity_kwh"] / nights, 2)
        item["liters_per_guest_night"] = round(item["water_liters"] / nights, 1)
        for key in ("electricity_kwh", "water_liters", "pv_self_consumed_kwh",
                    "total_fee", "saved_fee", "guest_nights", "electricity_fee", "water_fee"):
            if key in item:
                item[key] = round(float(item[key]), 2)
    houses.sort(key=lambda x: x["kwh_per_guest_night"], reverse=True)
    rooms.sort(key=lambda x: x["kwh_per_guest_night"], reverse=True)
    return houses, rooms


def monthly_report(db: Database, year: int, month: int) -> dict[str, Any]:
    if not 1 <= month <= 12:
        raise ValueError("month must be 1-12")
    start, end, prev_start, prev_end = month_bounds(year, month)
    houses, rooms = _aggregate_bills(db, start, end)
    prev_houses, _ = _aggregate_bills(db, prev_start, prev_end)
    previous = {h["house_id"]: h for h in prev_houses}
    for house in houses:
        prev = previous.get(house["house_id"])
        house["previous_month_kwh_per_guest_night"] = prev["kwh_per_guest_night"] if prev else None
        house["month_over_month_change_percent"] = (
            round((house["kwh_per_guest_night"] - prev["kwh_per_guest_night"])
                  / prev["kwh_per_guest_night"] * 100, 1)
            if prev and prev["kwh_per_guest_night"] else None
        )
    totals = {
        "houses": len(houses),
        "bills": sum(h["bills"] for h in houses),
        "electricity_kwh": round(sum(h["electricity_kwh"] for h in houses), 2),
        "water_liters": round(sum(h["water_liters"] for h in houses), 2),
        "pv_self_consumed_kwh": round(sum(h["pv_self_consumed_kwh"] for h in houses), 2),
        "total_fee": round(sum(h["total_fee"] for h in houses), 2),
        "saved_fee": round(sum(h["saved_fee"] for h in houses), 2),
    }
    return {
        "year": year, "month": month,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "totals": totals,
        "houses": houses,
        "rooms": rooms,
        "rank_by_electricity": [h["house_name"] for h in houses],
        "rank_by_water": [h["house_name"] for h in sorted(houses, key=lambda x: x["liters_per_guest_night"], reverse=True)],
    }
