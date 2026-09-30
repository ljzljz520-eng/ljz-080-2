"""SQLite storage layer used by the standard-library API and all services."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .timeutil import iso, parse_ts, utc_now

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS houses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL DEFAULT '',
    electricity_price REAL NOT NULL DEFAULT 0.68,
    water_price REAL NOT NULL DEFAULT 4.50,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    floor TEXT NOT NULL DEFAULT '',
    target_temperature REAL NOT NULL DEFAULT 26.0,
    created_at TEXT NOT NULL,
    UNIQUE(house_id, name)
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    house_id INTEGER NOT NULL REFERENCES houses(id) ON DELETE CASCADE,
    room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('air_conditioner','water_heater','solar_pv','water_tank')),
    rated_power_kw REAL,
    capacity_liters REAL,
    pv_capacity_kwp REAL,
    created_at TEXT NOT NULL,
    UNIQUE(house_id, name)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    guest_name TEXT NOT NULL,
    guest_count INTEGER NOT NULL DEFAULT 1,
    check_in_at TEXT NOT NULL,
    check_out_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','checked_in','checked_out')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    power_kw REAL,
    energy_kwh REAL,
    water_flow_lpm REAL,
    water_liters REAL,
    room_temperature REAL,
    set_temperature REAL,
    water_temperature REAL,
    tank_level_percent REAL,
    solar_generation_kw REAL,
    solar_generated_kwh REAL,
    online INTEGER NOT NULL DEFAULT 1,
    UNIQUE(device_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_id, ts);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    message TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
    UNIQUE(device_id, rule)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_board ON anomalies(acknowledged, resolved, last_seen DESC);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL,
    house_id INTEGER NOT NULL,
    check_in_at TEXT NOT NULL,
    check_out_at TEXT NOT NULL,
    electricity_kwh REAL NOT NULL,
    water_liters REAL NOT NULL,
    pv_self_consumed_kwh REAL NOT NULL,
    electricity_fee REAL NOT NULL,
    water_fee REAL NOT NULL,
    total_fee REAL NOT NULL,
    saved_fee REAL NOT NULL DEFAULT 0,
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    details_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_device ON commands(device_id, id);
"""


def adapt_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


class Database:
    """Thin SQLite wrapper providing row dictionaries and application-specific queries."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    def init_db(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(adapt_value(p) for p in params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        adapted = ([adapt_value(v) for v in row] for row in rows)
        return self.conn.executemany(sql, adapted)

    def begin(self) -> sqlite3.Cursor:  # transactions are explicit in this wrapper
        return self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def get(self, table: str, item_id: int) -> dict[str, Any]:
        row = self.query_one(f"SELECT * FROM {table} WHERE id = ?", (item_id,))
        if not row:
            raise KeyError(f"{table} {item_id} does not exist")
        return row

    def insert(self, table: str, data: dict[str, Any]) -> int:
        if "created_at" not in data and table in {
            "houses", "rooms", "devices", "bookings", "bills", "commands"
        }:
            data = {**data, "created_at": utc_now()}
        columns = list(data)
        placeholders = ",".join("?" for _ in columns)
        cur = self.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            [data[c] for c in columns],
        )
        return int(cur.lastrowid)

    # Rich joins useful by multiple services/API handlers.
    def device_detail(self, device_id: int) -> dict[str, Any] | None:
        return self.query_one(
            """
            SELECT d.*, h.name AS house_name, r.name AS room_name
              FROM devices d
              JOIN houses h ON h.id = d.house_id
         LEFT JOIN rooms r ON r.id = d.room_id
             WHERE d.id = ?
            """,
            (device_id,),
        )

    def booking_detail(self, booking_id: int) -> dict[str, Any] | None:
        row = self.query_one(
            """
            SELECT b.*, r.name AS room_name, r.house_id, r.target_temperature,
                   h.name AS house_name, h.electricity_price, h.water_price
              FROM bookings b
              JOIN rooms r ON r.id = b.room_id
              JOIN houses h ON h.id = r.house_id
             WHERE b.id = ?
            """,
            (booking_id,),
        )
        if row:
            row["check_in_at"] = parse_ts(row["check_in_at"])
            row["check_out_at"] = parse_ts(row["check_out_at"])
        return row

    def latest_reading(self, device_id: int) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM readings WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (device_id,),
        )

    def previous_reading(self, device_id: int, before_id: int) -> dict[str, Any] | None:
        return self.query_one(
            """
            SELECT * FROM readings
             WHERE device_id = ? AND id < ?
             ORDER BY id DESC LIMIT 1
            """,
            (device_id, before_id),
        )

    def readings_between(self, device_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.query_all(
            """
            SELECT * FROM readings
             WHERE device_id = ? AND ts >= ? AND ts <= ?
             ORDER BY ts, id
            """,
            (device_id, iso(start), iso(end)),
        )
        for row in rows:
            row["ts"] = parse_ts(row["ts"])
        return rows


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
