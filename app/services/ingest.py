"""设备接入：遥测上报入库（单条/批量）。设备编码找不到时抛错，不静默丢数。"""
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device
from . import timeseries as ts

ALLOWED_METRICS = {
    "ac": {"power_kw", "power_peak_kw", "room_temp", "target_temp", "mode", "energy_kwh"},
    "heater": {"power_kw", "power_peak_kw", "water_temp", "target_temp", "energy_kwh"},
    "pv": {"power_kw", "gen_kwh"},
    "tank": {"level_pct", "inflow_lh", "outflow_lh", "outflow_peak_lh", "out_l"},
}


def ingest_reading(db: Session, device_code: str, metric: str, value: float,
                   at: datetime | None = None, evaluate: bool = True) -> Device:
    at = at or datetime.utcnow()
    device = db.scalar(select(Device).where(Device.code == device_code))
    if device is None:
        raise LookupError(f"未知设备编码: {device_code}")
    if metric not in ALLOWED_METRICS[device.type.value]:
        raise ValueError(f"设备 {device_code}({device.type.value}) 不支持指标 {metric}，"
                         f"允许: {sorted(ALLOWED_METRICS[device.type.value])}")
    ts.insert_reading(db, device.id, at, metric, value)
    return device
