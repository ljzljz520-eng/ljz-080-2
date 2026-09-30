"""Telemetry ingestion and automatic energy/water anomaly detection."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..storage import Database
from ..timeutil import iso, parse_ts, utc_now

READING_FIELDS = (
    "power_kw", "energy_kwh", "water_flow_lpm", "water_liters",
    "room_temperature", "set_temperature", "water_temperature",
    "tank_level_percent", "solar_generation_kw", "solar_generated_kwh", "online",
)


class ValidationError(ValueError):
    pass


def _require_positive_number(payload: dict[str, Any], field: str, label: str) -> float:
    value = payload.get(field)
    if value is None:
        raise ValidationError(f"{label}为必填项")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是数字") from exc
    if number <= 0:
        raise ValidationError(f"{label}必须大于0")
    return number


def _normalize_reading(device: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        ts = parse_ts(payload.get("ts") or utc_now())
    except (TypeError, ValueError) as exc:
        raise ValidationError("ts 必须是有效的 ISO 时间") from exc

    values: dict[str, Any] = {"ts": ts}
    online = bool(payload.get("online", True))
    values["online"] = online
    for field in READING_FIELDS[:-1]:
        value = payload.get(field)
        values[field] = None if value is None else float(value)

    dtype = device["type"]
    allowed = {
        "air_conditioner": {"power_kw", "energy_kwh", "room_temperature", "set_temperature"},
        "water_heater": {"power_kw", "energy_kwh", "water_temperature", "water_flow_lpm"},
        "solar_pv": {"solar_generation_kw", "solar_generated_kwh"},
        "water_tank": {"tank_level_percent"},
    }[dtype]
    provided = {f for f in allowed if values[f] is not None}
    if not online:
        return values
    if not provided:
        raise ValidationError(f"{dtype} 设备至少需要上报一个有效指标: {', '.join(sorted(allowed))}")

    if dtype == "water_tank":
        level = values["tank_level_percent"]
        if level is None or not 0 <= level <= 100:
            raise ValidationError("水箱液位必须在 0-100 之间")
    for f in ("power_kw", "solar_generation_kw"):
        if values[f] is not None and values[f] < 0:
            raise ValidationError(f"{f} 不能为负数")
    return values


def record_reading(db: Database, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    device = db.device_detail(device_id)
    if not device:
        raise KeyError(f"device {device_id} does not exist")
    values = _normalize_reading(device, payload)
    columns = ["device_id", *values.keys()]
    placeholders = ",".join("?" for _ in columns)
    try:
        reading_id = db.insert("readings", {"device_id": device_id, **values})
    except Exception as exc:  # SQLite UNIQUE(device_id, ts)
        raise ValidationError("同一设备同一时间的遥测已存在") from exc
    detect_anomalies(db, device, reading_id, values)
    return db.get("readings", reading_id)


def record_batch(db: Database, payload: dict[str, Any]) -> dict[str, int]:
    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        raise ValidationError("readings 必须是非空数组")
    accepted = 0
    db.begin()
    try:
        for item in readings:
            if not isinstance(item, dict) or "device_id" not in item:
                raise ValidationError("每条批量数据都必须包含 device_id")
            device_id = int(item["device_id"])
            device = db.device_detail(device_id)
            if not device:
                raise ValidationError(f"设备 {device_id} 不存在")
            values = _normalize_reading(device, item)
            try:
                reading_id = db.insert("readings", {"device_id": device_id, **values})
            except Exception:
                continue  # duplicate interval telemetry is idempotent in a batch
            detect_anomalies(db, device, reading_id, values)
            accepted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"accepted": accepted, "received": len(readings)}


def _upsert_anomaly(
    db: Database,
    reading_id: int,
    device: dict[str, Any],
    rule: str,
    severity: str,
    message: str,
    active: bool,
    now: datetime,
) -> None:
    existing = db.query_one(
        "SELECT id, acknowledged FROM anomalies WHERE device_id=? AND rule=?",
        (device["id"], rule),
    )
    if active:
        if existing:
            db.execute(
                "UPDATE anomalies SET severity=?, message=?, last_seen=?, resolved=0, resolved_at=NULL, reading_id=? WHERE id=?",
                (severity, message, iso(now), reading_id, existing["id"]),
            )
        else:
            db.insert("anomalies", {
                "device_id": device["id"], "rule": rule, "severity": severity,
                "message": message, "first_seen": now, "last_seen": now,
                "acknowledged": 0, "resolved": 0, "reading_id": reading_id,
            })
    elif existing and not existing["acknowledged"]:
        db.execute(
            "UPDATE anomalies SET resolved=1, resolved_at=?, last_seen=? WHERE id=? AND resolved=0",
            (iso(now), iso(now), existing["id"]),
        )


def _signed_delta(prev: dict[str, Any] | None, current: dict[str, Any], field: str) -> float | None:
    if prev is None or current.get(field) is None or prev.get(field) is None:
        return None
    return float(current[field]) - float(prev[field])


def detect_anomalies(db: Database, device: dict[str, Any], reading_id: int, values: dict[str, Any]) -> None:
    now = values["ts"]
    prev = db.previous_reading(device["id"], reading_id)
    if prev:
        prev["ts"] = parse_ts(prev["ts"])
    dtype = device["type"]
    online = bool(values["online"])

    if not online:
        _upsert_anomaly(db, reading_id, device, "offline", "warning",
                        f"{device['name']} 通信离线，管家需检查网关或设备电源", True, now)
        return
    _upsert_anomaly(db, reading_id, device, "offline", "warning",
                    f"{device['name']} 通信离线", False, now)

    power = values.get("power_kw")
    if power is not None:
        rated = device.get("rated_power_kw")
        if rated:
            over = power > rated * 1.2
            _upsert_anomaly(
                db, reading_id, device, "over_power", "critical",
                f"{device['name']} 实时功率 {power:.2f}kW，超过额定 {rated:.2f}kW 的120%",
                over, now,
            )

    energy_delta = _signed_delta(prev, values, "energy_kwh")
    if energy_delta is not None and prev:
        hours = max((values["ts"] - prev["ts"]).total_seconds() / 3600, 1 / 3600)
        rated = device.get("rated_power_kw")
        jump = bool(rated and energy_delta > rated * hours * 1.25)
        stall = bool(
            energy_delta == 0
            and values.get("power_kw") is not None and values["power_kw"] > 0.05
        )
        rollback = energy_delta < 0
        if rated:
            _upsert_anomaly(
                db, reading_id, device, "energy_jump", "warning",
                f"{device['name']} 区间用电 {energy_delta:.2f}kWh，高于理论最大值 {rated * hours * 1.25:.2f}kWh",
                jump, now,
            )
        _upsert_anomaly(db, reading_id, device, "meter_stall", "info",
                        f"{device['name']} 设备运行但电表读数未变化", stall, now)
        _upsert_anomaly(db, reading_id, device, "meter_rollback", "critical",
                        f"{device['name']} 电表累计读数回退", rollback, now)

    if dtype == "air_conditioner":
        room = values.get("room_temperature")
        setpoint = values.get("set_temperature")
        power = values.get("power_kw")
        cooling_on = bool(power is not None and power > 0.05)
        bad = bool(room is not None and setpoint is not None and room > setpoint + 4 and cooling_on)
        message = (
            f"{device['name']} 正在运行，但室温 {room:.1f}℃ 明显高于设定 {setpoint:.1f}℃"
            if room is not None and setpoint is not None
            else f"{device['name']} 制冷效果异常"
        )
        _upsert_anomaly(
            db, reading_id, device, "ac_not_cooling", "warning",
            message,
            bad, now,
        )
    elif dtype == "water_heater":
        temp = values.get("water_temperature")
        power = values.get("power_kw")
        if temp is not None and temp > 75 and power is not None and power > 0.05:
            _upsert_anomaly(db, reading_id, device, "heater_overheat", "critical",
                            f"{device['name']} 加热中水温 {temp:.1f}℃，存在过热风险", True, now)
        elif temp is not None:
            _upsert_anomaly(db, reading_id, device, "heater_overheat", "critical",
                            f"{device['name']} 加热中水温过高", False, now)
    elif dtype == "water_tank":
        level = values.get("tank_level_percent")
        if level is not None:
            low = level < 20
            critical = level < 10
            _upsert_anomaly(
                db, reading_id, device, "tank_low", "critical" if critical else "warning",
                f"{device['name']} 蓄水液位 {level:.1f}%，低于安全水位", low, now,
            )
            if prev and prev.get("tank_level_percent") is not None:
                hours = max((values["ts"] - prev["ts"]).total_seconds() / 3600, 1 / 3600)
                drop = float(prev["tank_level_percent"]) - float(level)
                leakage = drop / hours > 10 and (values.get("water_flow_lpm") or 0) <= 0
                _upsert_anomaly(
                    db, reading_id, device, "tank_fast_drop", "warning",
                    f"{device['name']} 液位下降 {drop:.1f}%/{hours:.2f}h，疑似漏损", leakage, now,
                )
    elif dtype == "solar_pv":
        gen = values.get("solar_generation_kw")
        capacity = device.get("pv_capacity_kwp")
        if gen is not None and capacity and gen > capacity * 1.1:
            _upsert_anomaly(db, reading_id, device, "pv_over_generation", "warning",
                            f"{device['name']} 实时发电 {gen:.2f}kW 高于装机容量", True, now)


def list_readings(db: Database, device_id: int, limit: int = 100) -> list[dict[str, Any]]:
    if not db.query_one("SELECT id FROM devices WHERE id=?", (device_id,)):
        raise KeyError(f"device {device_id} does not exist")
    limit = min(max(limit, 1), 1000)
    return db.query_all(
        "SELECT * FROM readings WHERE device_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (device_id, limit),
    )


def list_anomalies(db: Database, active_only: bool = True, house_id: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT a.*, d.name AS device_name, d.type AS device_type, d.house_id,
               r.name AS room_name, h.name AS house_name
          FROM anomalies a
          JOIN devices d ON d.id = a.device_id
          JOIN houses h ON h.id = d.house_id
     LEFT JOIN rooms r ON r.id = d.room_id
    """
    clauses: list[str] = []
    params: list[Any] = []
    if active_only:
        clauses.append("a.acknowledged=0 AND a.resolved=0")
    if house_id:
        clauses.append("d.house_id=?")
        params.append(house_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.last_seen DESC"
    return db.query_all(sql, params)


def acknowledge_anomaly(db: Database, anomaly_id: int, note: str | None = None) -> dict[str, Any]:
    anomaly = db.get("anomalies", anomaly_id)
    db.execute(
        "UPDATE anomalies SET acknowledged=1, resolved=1, resolved_at=? WHERE id=?",
        (iso(utc_now()), anomaly_id),
    )
    return db.get("anomalies", anomaly_id)


def create_house(db: Database, payload: dict[str, Any]) -> int:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValidationError("房源名称不能为空")
    return db.insert("houses", {
        "name": name,
        "address": str(payload.get("address", "")),
        "electricity_price": _require_positive_number(payload, "electricity_price", "电价")
        if payload.get("electricity_price") is not None else 0.68,
        "water_price": _require_positive_number(payload, "water_price", "水价")
        if payload.get("water_price") is not None else 4.50,
    })


def create_room(db: Database, house_id: int, payload: dict[str, Any]) -> int:
    db.get("houses", house_id)
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValidationError("房间名称不能为空")
    return db.insert("rooms", {
        "house_id": house_id,
        "name": name,
        "floor": str(payload.get("floor", "")),
        "target_temperature": float(payload.get("target_temperature", 26.0)),
    })


def create_device(db: Database, payload: dict[str, Any]) -> int:
    house_id = int(payload["house_id"])
    db.get("houses", house_id)
    dtype = payload.get("type")
    if dtype not in {"air_conditioner", "water_heater", "solar_pv", "water_tank"}:
        raise ValidationError("设备类型必须是 air_conditioner/water_heater/solar_pv/water_tank")
    room_id = payload.get("room_id")
    if room_id is not None:
        room_id = int(room_id)
        room = db.query_one("SELECT id FROM rooms WHERE id=? AND house_id=?", (room_id, house_id))
        if not room:
            raise ValidationError("房间不属于该房源")
    if dtype in {"air_conditioner", "water_heater"} and not room_id:
        raise ValidationError("空调和热水器必须绑定房间")
    if dtype in {"solar_pv", "water_tank"} and room_id:
        raise ValidationError("光伏和蓄水设备属于整栋房源，不绑定房间")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValidationError("设备名称不能为空")
    data = {
        "house_id": house_id, "room_id": room_id, "name": name, "type": dtype,
        "rated_power_kw": payload.get("rated_power_kw"),
        "capacity_liters": payload.get("capacity_liters"),
        "pv_capacity_kwp": payload.get("pv_capacity_kwp"),
    }
    for key in ("rated_power_kw", "capacity_liters", "pv_capacity_kwp"):
        if data[key] is not None:
            data[key] = float(data[key])
    return db.insert("devices", data)


def create_booking(db: Database, payload: dict[str, Any]) -> int:
    room_id = int(payload["room_id"])
    db.get("rooms", room_id)
    name = str(payload.get("guest_name", "")).strip()
    if not name:
        raise ValidationError("游客姓名不能为空")
    check_in = parse_ts(payload.get("check_in_at"))
    check_out = parse_ts(payload.get("check_out_at"))
    if check_out <= check_in:
        raise ValidationError("离店时间必须晚于入住时间")
    guest_count = int(payload.get("guest_count", 1))
    if guest_count < 1:
        raise ValidationError("入住人数至少为1")
    return db.insert("bookings", {
        "room_id": room_id, "guest_name": name, "guest_count": guest_count,
        "check_in_at": check_in, "check_out_at": check_out,
        "status": payload.get("status", "reserved"),
    })
