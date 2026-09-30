"""Dependency-free HTTP API and static file hosting."""
from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .demo import seed_demo
from .services import board, checkout as checkout_service, monthly, telemetry
from .storage import Database
from .timeutil import iso

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if hasattr(value, "hex"):
        return str(value)
    return str(value)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AppState:
    def __init__(self, db: Database):
        self.db = db


def create_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def route(method: str, pattern: str):
        def decorator(func):
            routes.append((method, re.compile("^" + pattern + "$"), func))
            return func
        return decorator

    db = state.db

    @route("GET", r"/health")
    def health(request):
        return {"ok": True, "service": "island-energy-host", "time": iso(datetime.now().astimezone())}

    @route("POST", r"/api/houses")
    def create_house(request):
        house_id = telemetry.create_house(db, request.json())
        return db.get("houses", house_id), 201

    @route("GET", r"/api/houses")
    def list_houses(request):
        return db.query_all("SELECT * FROM houses ORDER BY id")

    @route("POST", r"/api/houses/(?P<house_id>\d+)/rooms")
    def create_room(request, house_id: str):
        room_id = telemetry.create_room(db, int(house_id), request.json())
        return db.get("rooms", room_id), 201

    @route("GET", r"/api/rooms")
    def list_rooms(request):
        house_id = request.query.get("house_id", [None])[0]
        if house_id:
            return db.query_all(
                """
                SELECT r.*, h.name AS house_name FROM rooms r
                JOIN houses h ON h.id=r.house_id
                 WHERE r.house_id=? ORDER BY r.id
                """,
                (int(house_id),),
            )
        return db.query_all(
            """
            SELECT r.*, h.name AS house_name FROM rooms r
            JOIN houses h ON h.id=r.house_id ORDER BY r.house_id, r.id
            """
        )

    @route("POST", r"/api/devices")
    def create_device(request):
        device_id = telemetry.create_device(db, request.json())
        return db.device_detail(device_id), 201

    @route("GET", r"/api/devices")
    def list_devices(request):
        house_id = request.query.get("house_id", [None])[0]
        return board.device_overview(db, int(house_id) if house_id else None)

    @route("GET", r"/api/devices/(?P<device_id>\d+)/readings")
    def device_readings(request, device_id: str):
        limit = int(request.query.get("limit", ["100"])[0])
        return telemetry.list_readings(db, int(device_id), limit)

    @route("POST", r"/api/bookings")
    def create_booking(request):
        booking_id = telemetry.create_booking(db, request.json())
        return db.booking_detail(booking_id), 201

    @route("GET", r"/api/bookings")
    def list_bookings(request):
        rows = db.query_all(
            """
            SELECT b.*, r.name room_name, h.name house_name, h.id house_id
              FROM bookings b JOIN rooms r ON r.id=b.room_id
              JOIN houses h ON h.id=r.house_id
             ORDER BY b.check_in_at DESC
            """
        )
        return rows

    @route("POST", r"/api/bookings/(?P<booking_id>\d+)/checkin")
    def check_in(request, booking_id: str):
        booking = db.get("bookings", int(booking_id))
        db.execute("UPDATE bookings SET status='checked_in' WHERE id=?", (booking["id"],))
        return db.booking_detail(booking["id"])

    @route("POST", r"/api/bookings/(?P<booking_id>\d+)/precool")
    def precool(request, booking_id: str):
        return board.precool_booking(db, int(booking_id))

    @route("POST", r"/api/bookings/(?P<booking_id>\d+)/checkout")
    def do_checkout(request, booking_id: str):
        return checkout_service.checkout(db, int(booking_id), request.json().get("check_out_at"))

    @route("POST", r"/api/telemetry")
    def post_reading(request):
        payload = request.json()
        device_id = payload.pop("device_id", None)
        if device_id is None:
            raise ApiError(400, "device_id 为必填项")
        return telemetry.record_reading(db, int(device_id), payload), 201

    @route("POST", r"/api/telemetry/batch")
    def post_batch(request):
        return telemetry.record_batch(db, request.json())

    @route("GET", r"/api/board/overview")
    def overview(request):
        return board.board_overview(db)

    @route("GET", r"/api/board/arrivals")
    def arrivals(request):
        hours = int(request.query.get("hours", ["24"])[0])
        return board.arrival_board(db, hours=hours)

    @route("GET", r"/api/board/tanks")
    def tanks(request):
        house_id = request.query.get("house_id", [None])[0]
        return board.tank_board(db, int(house_id) if house_id else None)

    @route("GET", r"/api/anomalies")
    def anomalies(request):
        active = request.query.get("active", ["1"])[0] != "0"
        house_id = request.query.get("house_id", [None])[0]
        return telemetry.list_anomalies(db, active, int(house_id) if house_id else None)

    @route("POST", r"/api/anomalies/(?P<anomaly_id>\d+)/ack")
    def ack_anomaly(request, anomaly_id: str):
        return telemetry.acknowledge_anomaly(db, int(anomaly_id), request.json().get("note"))

    @route("GET", r"/api/bills")
    def bills(request):
        room_id = request.query.get("room_id", [None])[0]
        return checkout_service.list_bills(db, int(room_id) if room_id else None)

    @route("GET", r"/api/bills/(?P<bill_id>\d+)")
    def get_bill(request, bill_id: str):
        return checkout_service.get_bill(db, int(bill_id))

    @route("GET", r"/api/reports/monthly")
    def monthly_report(request):
        today = datetime.now().astimezone()
        year = int(request.query.get("year", [str(today.year)])[0])
        month = int(request.query.get("month", [str(today.month)])[0])
        return monthly.monthly_report(db, year, month)

    class Request:
        def __init__(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]):
            self.handler = handler
            self.query = query
            self._body: dict[str, Any] | None = None

        def json(self) -> dict[str, Any]:
            if self._body is None:
                length = int(self.handler.headers.get("Content-Length", "0"))
                raw = self.handler.rfile.read(length) if length else b"{}"
                try:
                    parsed = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError as exc:
                    raise ApiError(400, f"JSON 格式错误: {exc.msg}") from exc
                if not isinstance(parsed, dict):
                    raise ApiError(400, "请求体必须是 JSON 对象")
                self._body = parsed
            return self._body

    class Handler(BaseHTTPRequestHandler):
        server_version = "IslandEnergy/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False, default=json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self._send_json(404, {"error": "not found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.end_headers()

        def _handle(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path in {"/", "/index.html"}:
                self._send_static(WEB_ROOT / "index.html")
                return
            if path.startswith("/static/"):
                relative = path.removeprefix("/static/")
                target = (WEB_ROOT / relative).resolve()
                if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
                    raise ApiError(403, "forbidden")
                self._send_static(target)
                return
            query = parse_qs(parsed_url.query)
            for method, pattern, func in routes:
                match = pattern.match(path)
                if method != self.command or not match:
                    continue
                result = func(Request(self, query), **match.groupdict())
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
                    data, status = result
                else:
                    data, status = result, 200
                self._send_json(status, data)
                return
            raise ApiError(404, f"找不到接口: {self.command} {path}")

        def do_GET(self) -> None:
            self._guard()

        def do_POST(self) -> None:
            self._guard()

        def _guard(self) -> None:
            try:
                self._handle()
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except KeyError as exc:
                self._send_json(404, {"error": f"资源不存在: {exc.args[0]}"})
            except (ValueError, telemetry.ValidationError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # keep the API responsive during development
                self._send_json(500, {"error": f"服务器内部错误: {exc}"})

    return Handler


def create_db(path: str | Path, auto_seed: bool = False) -> Database:
    db = Database(path)
    db.init_db()
    if auto_seed and not db.query_one("SELECT id FROM houses LIMIT 1"):
        seed_demo(db)
    return db
