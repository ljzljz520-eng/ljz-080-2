"""Pydantic 请求/响应模型。"""
from datetime import datetime, date
from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 基础资源 ----------
class PropertyIn(BaseModel):
    code: str
    name: str
    address: str = ""
    pv_capacity_kwp: float = 5.0
    tank_capacity_l: float = 2000.0
    elec_price: float = 1.2
    water_price: float = 4.0


class PropertyOut(ORMModel):
    id: int
    code: str
    name: str
    address: str
    pv_capacity_kwp: float
    tank_capacity_l: float
    elec_price: float
    water_price: float


class RoomIn(BaseModel):
    code: str
    name: str


class RoomOut(ORMModel):
    id: int
    property_id: int
    code: str
    name: str


class DeviceIn(BaseModel):
    code: str
    name: str
    type: str
    room_id: int | None = None
    rated_power_kw: float = 0.0
    fault_profile: str | None = None


class DeviceOut(ORMModel):
    id: int
    code: str
    name: str
    type: str
    property_id: int
    room_id: int | None
    rated_power_kw: float
    fault_profile: str | None
    active: bool


# ---------- 遥测 ----------
class ReadingIn(BaseModel):
    device_code: str
    ts: datetime | None = None
    metric: str
    value: float


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn]


class ReadingOut(ORMModel):
    ts: datetime
    metric: str
    value: float


class DeviceStateOut(BaseModel):
    metric: str
    value: float
    ts: datetime


# ---------- 预订 ----------
class BookingIn(BaseModel):
    code: str
    room_id: int
    guest_name: str
    guest_phone: str = ""
    guest_count: int = 2
    check_in: datetime
    check_out: datetime
    precool_requested: bool = True


class BookingOut(ORMModel):
    id: int
    code: str
    room_id: int
    guest_name: str
    guest_phone: str
    guest_count: int
    check_in: datetime
    check_out: datetime
    precool_requested: bool
    precool_started_at: datetime | None
    checked_out_at: datetime | None


# ---------- 指令 ----------
class CommandIn(BaseModel):
    command: str = Field(description="turn_on/turn_off/set_target/precool")
    params: dict = {}


class CommandOut(ORMModel):
    id: int
    device_id: int
    command: str
    params: dict
    status: str
    created_at: datetime


# ---------- 看板 ----------
class PrecoolItem(BaseModel):
    booking_id: int
    code: str
    guest_name: str
    check_in: datetime
    room_id: int
    room_name: str
    property_name: str
    guest_count: int
    precool_requested: bool
    status: str          # done / cooling / pending / missed / not_requested
    target_temp: float | None
    room_temp: float | None
    ac_on: bool


class TankStatus(BaseModel):
    device_id: int
    property_id: int
    property_name: str
    level_pct: float
    inflow_lh: float
    outflow_lh: float


class Dashboard(BaseModel):
    now: datetime
    today_arrivals: int
    precool_done: int
    precool_pending: list[PrecoolItem]
    open_alerts: list["AlertOut"]
    tank_status: list[TankStatus]


# ---------- 告警 ----------
class AlertOut(ORMModel):
    id: int
    device_id: int
    rule: str
    severity: str
    status: str
    message: str
    value: float | None
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None


# ---------- 账单 ----------
class BillOut(ORMModel):
    id: int
    booking_id: int
    property_id: int
    code: str
    issued_at: datetime
    period_start: datetime
    period_end: datetime
    elec_kwh: float
    water_t: float
    pv_kwh: float
    elec_fee: float
    water_fee: float
    pv_offset_fee: float
    total_fee: float
    detail: dict
    score: int
    advice: list


# ---------- 月报 ----------
class MonthPropertyRow(BaseModel):
    property_id: int
    property_code: str
    property_name: str
    bookings: int
    guest_nights: int
    elec_kwh: float
    meter_elec_kwh: float
    loss_elec_kwh: float
    pv_kwh: float
    water_t: float
    meter_water_t: float
    loss_water_t: float
    elec_per_night: float
    water_per_night: float
    total_fee: float
    pv_self_ratio: float
    open_alerts: int


class MonthReport(BaseModel):
    month: str
    properties: list[MonthPropertyRow]


Dashboard.model_rebuild()
