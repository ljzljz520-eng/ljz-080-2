"""Housekeeper arrival board, pre-cool commands, device and tank overview."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..storage import Database
from ..timeutil import iso, parse_ts, utc_now
from .telemetry import ValidationError


def _ts(row: dict[str, Any], key: str) -> str:
    value = row[key]
    return value if isinstance(value, str) else iso(value)


def arrival_board(db: Database, hours: int = 24, now=None) -> list[dict[str, Any]]:
    now = now or utc_now()
    hours = max(1, min(int(hours), 72))
    bookings = db.query_all(
        """
        SELECT b.*, r.name AS room_name, r.house_id, r.target_temperature,
               h.name AS house_name
          FROM bookings b
          JOIN rooms r ON r.id = b.room_id
          JOIN houses h ON h.id = r.house_id
         WHERE b.status IN ('reserved','checked_in')
           AND b.check_in_at <= ?
         ORDER BY b.check_in_at
        """,
        (iso(now + timedelta(hours=hours)),),
    )
    result = []
    for booking in bookings:
        check_in = parse_ts(booking["check_in_at"])
        acs = db.query_all(
            "SELECT * FROM devices WHERE room_id=? AND type='air_conditioner'",
            (booking["room_id"],),
        )
        ac_states = [_ac_precool_state(db, ac, check_in, booking["target_temperature"], now) for ac in acs]
        if not ac_states:
            status = "no_ac"
        elif now > check_in:
            status = "overdue" if booking["status"] == "reserved" else "checked_in"
        elif any(s["status"] == "ready" for s in ac_states):
            status = "ready"
        elif any(s["status"] == "precooling" for s in ac_states):
            status = "precooling"
        elif check_in - now <= timedelta(minutes=30):
            status = "not_ready"
        else:
            status = "scheduled"
        result.append({
            "booking_id": booking["id"],
            "house_id": booking["house_id"],
            "house_name": booking["house_name"],
            "room_id": booking["room_id"],
            "room_name": booking["room_name"],
            "guest_name": booking["guest_name"],
            "guest_count": booking["guest_count"],
            "status": booking["status"],
            "precool_status": status,
            "check_in_at": iso(check_in),
            "minutes_to_arrival": max(0, round((check_in - now).total_seconds() / 60)),
            "target_temperature": booking["target_temperature"],
            "air_conditioners": ac_states,
        })
    return result


def _ac_precool_state(db: Database, ac: dict[str, Any], arrival, target: float, now) -> dict[str, Any]:
    latest = db.latest_reading(ac["id"])
    base = {
        "device_id": ac["id"], "device_name": ac["name"],
        "rated_power_kw": ac["rated_power_kw"],
        "latest_ts": latest["ts"] if latest else None,
        "room_temperature": latest["room_temperature"] if latest else None,
        "set_temperature": latest["set_temperature"] if latest else None,
        "power_kw": latest["power_kw"] if latest else None,
        "online": bool(latest and latest["online"]),
    }
    if not latest or not latest["online"]:
        state = "offline"
    elif now > arrival:
        state = "arrival_passed"
    elif arrival - now > timedelta(minutes=30):
        state = "waiting"
    elif (
        latest["power_kw"] is not None and latest["power_kw"] > 0.05
        and latest["set_temperature"] is not None
        and abs(float(latest["set_temperature"]) - float(target)) <= 1
        and latest["room_temperature"] is not None
        and float(latest["room_temperature"]) <= float(target) + 1
    ):
        state = "ready"
    elif latest["power_kw"] is not None and latest["power_kw"] > 0.05:
        state = "precooling"
    else:
        state = "not_ready"
    return {**base, "status": state}


def precool_booking(db: Database, booking_id: int) -> dict[str, Any]:
    booking = db.booking_detail(booking_id)
    if not booking:
        raise KeyError(f"booking {booking_id} does not exist")
    if booking["status"] == "checked_out":
        raise ValidationError("订单已离店，不能预冷")
    acs = db.query_all(
        "SELECT * FROM devices WHERE room_id=? AND type='air_conditioner' ORDER BY id",
        (booking["room_id"],),
    )
    if not acs:
        raise ValidationError("房间未绑定空调")
    commands = []
    states = []
    for ac in acs:
        command_id = db.insert("commands", {
            "device_id": ac["id"], "booking_id": booking_id, "action": "turn_on",
            "payload_json": '{"mode":"cool","temperature":%.1f}' % float(booking["target_temperature"]),
            "status": "pending",
        })
        commands.append(command_id)
        states.append({
            "device_id": ac["id"], "device_name": ac["name"],
            "command_id": command_id, "action": "turn_on",
            "set_temperature": booking["target_temperature"], "status": "command_sent",
        })
    return {
        "booking_id": booking_id,
        "message": "预冷指令已下发，设备回传后看板状态将更新",
        "commands": states,
    }


def tank_board(db: Database, house_id: int | None = None) -> list[dict[str, Any]]:
    where = "WHERE d.type='water_tank'"
    params: list[Any] = []
    if house_id:
        where += " AND d.house_id=?"
        params.append(house_id)
    tanks = db.query_all(
        f"""
        SELECT d.*, h.name AS house_name FROM devices d
        JOIN houses h ON h.id=d.house_id {where} ORDER BY d.id
        """,
        params,
    )
    result = []
    for tank in tanks:
        reading = db.latest_reading(tank["id"])
        level = reading["tank_level_percent"] if reading else None
        if not reading or not reading["online"]:
            state = "offline"
        elif level is None:
            state = "unknown"
        elif level < 10:
            state = "critical"
        elif level < 20:
            state = "low"
        else:
            state = "normal"
        liters = None
        if level is not None and tank.get("capacity_liters"):
            liters = round(level / 100 * float(tank["capacity_liters"]), 1)
        result.append({
            "device_id": tank["id"], "house_id": tank["house_id"], "house_name": tank["house_name"],
            "device_name": tank["name"], "capacity_liters": tank["capacity_liters"],
            "level_percent": level, "available_liters": liters, "state": state,
            "latest_ts": reading["ts"] if reading else None,
        })
    return result


def device_overview(db: Database, house_id: int | None = None) -> list[dict[str, Any]]:
    where, params = "", []
    if house_id:
        where, params = "WHERE d.house_id=?", [house_id]
    devices = db.query_all(
        f"""
        SELECT d.*, h.name AS house_name, r.name AS room_name
          FROM devices d JOIN houses h ON h.id=d.house_id
     LEFT JOIN rooms r ON r.id=d.room_id {where}
         ORDER BY d.house_id, d.type, d.id
        """,
        params,
    )
    out = []
    for device in devices:
        latest = db.latest_reading(device["id"])
        out.append({
            "id": device["id"], "house_id": device["house_id"], "house_name": device["house_name"],
            "room_id": device["room_id"], "room_name": device["room_name"],
            "name": device["name"], "type": device["type"],
            "rated_power_kw": device["rated_power_kw"],
            "capacity_liters": device["capacity_liters"],
            "pv_capacity_kwp": device["pv_capacity_kwp"],
            "latest": latest,
            "online": bool(latest and latest["online"]),
        })
    return out


def board_overview(db: Database, now=None) -> dict[str, Any]:
    arrivals = arrival_board(db, hours=24, now=now)
    anomalies = []
    from .telemetry import list_anomalies
    anomalies = list_anomalies(db, active_only=True)
    tanks = tank_board(db)
    counts = {"ready": 0, "precooling": 0, "not_ready": 0, "scheduled": 0, "offline": 0, "other": 0}
    for item in arrivals:
        counts[item["precool_status"] if item["precool_status"] in counts else "other"] += 1
    return {
        "arrivals_24h": len(arrivals),
        "precool": counts,
        "open_anomalies": len(anomalies),
        "critical_anomalies": sum(1 for a in anomalies if a["severity"] == "critical"),
        "tank_alerts": sum(1 for t in tanks if t["state"] in {"low", "critical", "offline"}),
        "devices": len(device_overview(db)),
    }
