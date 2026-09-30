"""Deterministic demo data: island houses, devices, bookings and four days of telemetry."""
from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

from .services import telemetry
from .services.checkout import checkout
from .storage import Database
from .timeutil import iso, utc_now


def _create_model(db: Database, now) -> dict[str, int]:
    house_a = telemetry.create_house(db, {
        "name": "听海一号院", "address": "东澳岛珊瑚湾8号",
        "electricity_price": 0.72, "water_price": 5.20,
    })
    house_b = telemetry.create_house(db, {
        "name": "椰风二号院", "address": "东澳岛椰林路21号",
        "electricity_price": 0.66, "water_price": 4.80,
    })
    rooms_a, rooms_b, devices = [], [], {}
    room_specs_a = [("海景大床房A", "2F"), ("亲子双床房A", "2F"), ("庭院家庭房A", "1F")]
    room_specs_b = [("日出露台房B", "3F"), ("沙滩套房B", "1F")]
    for name, floor in room_specs_a:
        rooms_a.append(telemetry.create_room(db, house_a, {"name": name, "floor": floor}))
    for name, floor in room_specs_b:
        rooms_b.append(telemetry.create_room(db, house_b, {"name": name, "floor": floor}))

    for house_id, rooms in ((house_a, rooms_a), (house_b, rooms_b)):
        for idx, room_id in enumerate(rooms, 1):
            ac = telemetry.create_device(db, {
                "house_id": house_id, "room_id": room_id,
                "name": f"{idx:02d}空调", "type": "air_conditioner", "rated_power_kw": 1.2,
            })
            heater = telemetry.create_device(db, {
                "house_id": house_id, "room_id": room_id,
                "name": f"{idx:02d}热水器", "type": "water_heater", "rated_power_kw": 2.0,
                "capacity_liters": 60,
            })
            devices[room_id] = (ac, heater)
        pv = telemetry.create_device(db, {
            "house_id": house_id, "name": "屋顶光伏", "type": "solar_pv",
            "pv_capacity_kwp": 5.0 if house_id == house_a else 6.0,
        })
        tank = telemetry.create_device(db, {
            "house_id": house_id, "name": "高位蓄水池", "type": "water_tank",
            "capacity_liters": 10000 if house_id == house_a else 12000,
        })
        devices[f"pv{house_id}"] = pv
        devices[f"tank{house_id}"] = tank

    b1 = telemetry.create_booking(db, {
        "room_id": rooms_b[0], "guest_name": "林女士", "guest_count": 2,
        "check_in_at": now + timedelta(minutes=20), "check_out_at": now + timedelta(days=2, hours=10),
    })
    b2 = telemetry.create_booking(db, {
        "room_id": rooms_a[1], "guest_name": "陈先生一家", "guest_count": 3,
        "check_in_at": now + timedelta(hours=4), "check_out_at": now + timedelta(days=3),
    })
    b3 = telemetry.create_booking(db, {
        "room_id": rooms_a[2], "guest_name": "周先生", "guest_count": 2,
        "check_in_at": now + timedelta(minutes=25), "check_out_at": now + timedelta(days=1, hours=14),
    })
    b4 = telemetry.create_booking(db, {
        "room_id": rooms_a[0], "guest_name": "赵女士", "guest_count": 2,
        "check_in_at": now + timedelta(minutes=15), "check_out_at": now + timedelta(days=2, hours=12),
    })
    active = telemetry.create_booking(db, {
        "room_id": rooms_b[1], "guest_name": "吴先生", "guest_count": 2,
        "check_in_at": now - timedelta(hours=20), "check_out_at": now + timedelta(hours=28),
        "status": "checked_in",
    })
    completed = telemetry.create_booking(db, {
        "room_id": rooms_b[1], "guest_name": "上一批游客", "guest_count": 3,
        "check_in_at": now - timedelta(hours=46), "check_out_at": now - timedelta(hours=22),
        "status": "checked_out",
    })
    return {
        "houses": [house_a, house_b], "rooms_a": rooms_a, "rooms_b": rooms_b,
        "devices": devices, "bookings": {
            "ready": b1, "scheduled": b2, "precooling": b3, "not_ready": b4,
            "active": active, "completed": completed,
        },
    }


def _ac_plan(room_slot: int, hours_from_now: float, room_index: int, is_b2: bool) -> tuple[float, float, float]:
    if is_b2 and -46 <= hours_from_now < -22:
        return 1.08, 22.0, 27.0
    if is_b2 and -20 <= hours_from_now <= 0:
        return 0.55, 25.0, 26.0
    if room_index in (2, 3) and -0.25 <= hours_from_now <= 0:  # A2 historical and A3 pre-cooling
        return 0.92, 26.0, 28.0
    if room_index == 11 and -0.25 <= hours_from_now <= 0:  # B1 ready
        return 0.78, 26.0, 25.4
    # Tiny standby keeps demonstration realistic; latest non-B rooms are visibly off.
    if hours_from_now > -1:
        return 0.0, 26.0, 30.2 + (room_index % 2)
    ambient = 29.5 + 1.2 * math.sin(room_slot / 7.0)
    power = 0.12 + 0.18 * (1 + math.sin(room_slot / 9 + room_index)) / 2
    return round(power, 2), 26.0, ambient


def _heater_plan(hours_from_now: float, slot: int, is_b2: bool) -> tuple[float, float, float]:
    if is_b2 and -46 <= hours_from_now < -22:
        flow = 7.0 if slot % 4 == 0 else 0.0
        return 1.15, 58.0, flow
    if is_b2 and -20 <= hours_from_now <= 0:
        flow = 4.0 if slot % 8 == 0 else 0.0
        return 0.45, 48.0, flow
    flow = 3.5 if slot % 12 == 0 and hours_from_now < -2 else 0.0
    return (0.35 if slot % 10 in (0, 1) else 0.0), 46.0, flow


def _generate_readings(db: Database, ids: dict[str, int], now) -> None:
    devices = ids["devices"]
    interval = timedelta(minutes=15)
    start = now - timedelta(hours=50)
    slots = int((now - start).total_seconds() / 900)
    cumulative: dict[int, tuple[float, float]] = {}
    for room_devices in [devices[r] for r in ids["rooms_a"] + ids["rooms_b"]]:
        for device_id in room_devices:
            cumulative[device_id] = (8.0, 0.0)
    for house_id in ids["houses"]:
        cumulative[devices[f"pv{house_id}"]] = (0.0, 0.0)

    all_rooms = [(r, i + 1, False) for i, r in enumerate(ids["rooms_a"])]
    all_rooms += [(r, 10 + i + 1, r == ids["rooms_b"][1]) for i, r in enumerate(ids["rooms_b"])]
    for slot in range(slots + 1):
        ts = start + slot * interval
        hours = (ts - now).total_seconds() / 3600
        daylight = max(0.0, math.sin(math.pi * (hours % 24 - 7) / 10)) if 7 <= (hours % 24) <= 17 else 0
        for room_id, idx, is_b2 in all_rooms:
            ac_id, heater_id = devices[room_id]
            ac_power, setpoint, temp = _ac_plan(slot, hours, idx, is_b2)
            ac_energy, _ = cumulative[ac_id]
            ac_energy += ac_power * 0.25
            cumulative[ac_id] = (ac_energy, 0)
            telemetry.record_reading(db, ac_id, {
                "ts": iso(ts), "power_kw": ac_power, "energy_kwh": round(ac_energy, 3),
                "room_temperature": round(temp, 1), "set_temperature": setpoint, "online": True,
            })
            heater_power, water_temp, flow = _heater_plan(hours, slot, is_b2)
            heater_energy, water = cumulative[heater_id]
            heater_energy += heater_power * 0.25
            water += flow * 15
            cumulative[heater_id] = (heater_energy, water)
            telemetry.record_reading(db, heater_id, {
                "ts": iso(ts), "power_kw": heater_power, "energy_kwh": round(heater_energy, 3),
                "water_temperature": round(water_temp, 1), "water_flow_lpm": flow,
                "water_liters": round(water, 1), "online": True,
            })
        for house_index, house_id in enumerate(ids["houses"], 1):
            pv_id = devices[f"pv{house_id}"]
            capacity = 5.0 if house_index == 1 else 6.0
            generation = round(capacity * daylight * (0.88 + 0.04 * math.sin(slot)), 3)
            pv_energy, _ = cumulative[pv_id]
            pv_energy += generation * 0.25
            cumulative[pv_id] = (pv_energy, 0)
            telemetry.record_reading(db, pv_id, {
                "ts": iso(ts), "solar_generation_kw": generation,
                "solar_generated_kwh": round(pv_energy, 3), "online": True,
            })
        # Tank levels descend gently; house A is deliberately low for the housekeeper.
        telemetry.record_reading(db, devices[f"tank{ids['houses'][0]}"], {
            "ts": iso(ts), "tank_level_percent": round(48 - 39 * slot / max(slots, 1), 1), "online": True,
        })
        telemetry.record_reading(db, devices[f"tank{ids['houses'][1]}"], {
            "ts": iso(ts), "tank_level_percent": round(78 - 6 * slot / max(slots, 1), 1), "online": True,
        })


def seed_demo(db: Database) -> dict[str, int]:
    now = utc_now().replace(second=0, microsecond=0)
    minute = now.minute - now.minute % 15
    now = now.replace(minute=minute)
    ids = _create_model(db, now)
    _generate_readings(db, ids, now)
    bill = checkout(db, ids["bookings"]["completed"])
    ids["completed_bill"] = bill["id"]
    return ids


def reset_and_seed(path: str | Path) -> dict[str, int]:
    path = Path(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    with Database(path) as db:
        db.init_db()
        return seed_demo(db)
