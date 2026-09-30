"""规则引擎：根据设备最新状态评估异常，维护告警生命周期。

告警只与 (设备, 规则) 绑定：持续命中 → 更新 last_seen；不再命中 → 自动解除。
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..config import TIMEZONE, DEFAULT_WATER_LOW_PCT
from ..models import Device, DeviceType, Alert, AlertSeverity, AlertStatus, Reading, Booking
from . import timeseries as ts

# 规则中文名 / 默认严重级别（用于 UI 与提示）
RULE_LABELS = {
    "ac_overpower": "空调功率超限",
    "ac_vacant_running": "空房空调运行",
    "ac_low_target": "制冷设定过低",
    "heater_overpower": "热水器功率超限",
    "heater_vacant_running": "空房热水器运行",
    "heater_max_temp": "热水器高温运行",
    "pv_zero_daytime": "白天光伏无出力",
    "pv_overrated": "光伏出力超装机",
    "tank_low": "蓄水液位低",
    "tank_leak": "疑似管网漏损",
    "tank_idle_outflow": "无在住持续出水",
}

# 预冷前置窗口：入住前该时段内开启空调不算空房浪费
PRECOOL_WINDOW_MIN = 120


def _local_hour(utc_dt: datetime) -> int:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(TIMEZONE).hour


def _expect_occupied(db: Session, device: Device, now: datetime) -> bool:
    """房间在住，或入住预冷窗口内（含管家预冷）。"""
    if device.room_id is None:
        return True  # 房源级设备不受空房规则约束
    active = ts.active_bookings(db, device.room_id, now)
    if active:
        return True
    upcoming = db.scalar(
        select(Booking).where(
            Booking.room_id == device.room_id,
            Booking.precool_requested.is_(True),
            Booking.check_in > now,
            Booking.check_in <= now + timedelta(minutes=PRECOOL_WINDOW_MIN),
        )
    )
    return upcoming is not None


def _recent_power(db: Session, device_id: int, now: datetime, minutes: int = 90) -> tuple[float, int]:
    """近 N 分钟平均功率与样本数。"""
    since = now - timedelta(minutes=minutes)
    rows = db.scalars(
        select(Reading.value).where(
            Reading.device_id == device_id, Reading.metric == "power_kw",
            Reading.ts >= since, Reading.ts <= now,
        )
    ).all()
    return (sum(rows) / len(rows), len(rows)) if rows else (0.0, 0)


def _recent_max(db: Session, device_id: int, now: datetime, metric: str = "power_peak_kw",
                minutes: int = 180) -> tuple[float, int]:
    """近 N 窗口内最大瞬时值与样本数（捕捉循环停机的超功率/大流量）。"""
    since = now - timedelta(minutes=minutes)
    rows = db.scalars(
        select(Reading.value).where(
            Reading.device_id == device_id, Reading.metric == metric,
            Reading.ts >= since, Reading.ts <= now,
        )
    ).all()
    return (max(rows), len(rows)) if rows else (0.0, 0)


def upsert_alert(db: Session, device: Device, rule: str, severity: AlertSeverity,
                 message: str, value: float | None, now: datetime) -> Alert:
    alert = db.scalar(
        select(Alert).where(
            Alert.device_id == device.id, Alert.rule == rule,
            Alert.status.in_([AlertStatus.OPEN, AlertStatus.ACK]),
        )
    )
    if alert is None:
        alert = Alert(device_id=device.id, rule=rule, severity=severity,
                      message=message, value=value, first_seen=now, last_seen=now)
        db.add(alert)
    else:
        alert.severity = severity
        alert.message = message
        alert.value = value
        alert.last_seen = now
        if alert.status == AlertStatus.ACK:
            pass  # 管家已知晓，保持 ACK
    return alert


def _resolve_missing(db: Session, device: Device, fired: set[str], now: datetime):
    open_alerts = db.scalars(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.status.in_([AlertStatus.OPEN, AlertStatus.ACK]),
        )
    ).all()
    for a in open_alerts:
        if a.rule not in fired:
            a.status = AlertStatus.RESOLVED
            a.resolved_at = now


def evaluate_device(db: Session, device: Device, now: datetime) -> list[Alert]:
    """评估单台设备的全部规则，返回当前未解除告警。"""
    if not device.active:
        return []
    states = ts.state_map(db, device.id)

    def val(metric):
        return states[metric].value if metric in states else None

    fired: dict[str, tuple[AlertSeverity, str, float | None]] = {}

    if device.type == DeviceType.AC:
        power, target, room_t = val("power_kw"), val("target_temp"), val("room_temp")
        mode = val("mode")
        max_p, nmax = _recent_max(db, device.id, now, "power_peak_kw", minutes=180)
        if device.rated_power_kw > 0 and nmax >= 1 and max_p > device.rated_power_kw * 1.5:
            fired["ac_overpower"] = (AlertSeverity.CRITICAL,
                f"{device.name} 近 3h 峰值功率 {max_p:.2f}kW，超额定 {device.rated_power_kw:.2f}kW 的 50%，疑似故障或卡滞", max_p)
        mean_p, nmean = _recent_power(db, device.id, now, minutes=60)
        if mean_p is not None and mean_p > 0.15 and not _expect_occupied(db, device, now):
            fired["ac_vacant_running"] = (AlertSeverity.WARNING,
                f"{device.name} 在空房状态下运行（近1h均值 {mean_p:.2f}kW），疑似未关机或偷电", mean_p)
        if power is not None and target is not None and mean_p > 0.1 and target <= 20:
            fired["ac_low_target"] = (AlertSeverity.INFO,
                f"{device.name} 制冷设定 {target:.0f}℃ 过低，建议 24-26℃", target)

    elif device.type == DeviceType.HEATER:
        power, water_t = val("power_kw"), val("water_temp")
        max_p, nmax = _recent_max(db, device.id, now, "power_peak_kw", minutes=120)
        if device.rated_power_kw > 0 and nmax >= 1 and max_p > device.rated_power_kw * 1.5:
            fired["heater_overpower"] = (AlertSeverity.CRITICAL,
                f"{device.name} 近 2h 峰值功率 {max_p:.2f}kW 超额定 50%，疑似加热管卡滞", max_p)
        mean_p, _n = _recent_power(db, device.id, now, minutes=120)
        if mean_p is not None and mean_p > 0.2 and not _expect_occupied(db, device, now):
            fired["heater_vacant_running"] = (AlertSeverity.WARNING,
                f"{device.name} 空房持续加热（近2h均值 {mean_p:.2f}kW），建议改为入住前定时启动", mean_p)
        if water_t is not None and water_t >= 53 and (mean_p or 0) > 0.1:
            fired["heater_max_temp"] = (AlertSeverity.INFO,
                f"{device.name} 水温 {water_t:.0f}℃ 且持续加热，高温既费电又易结垢", water_t)

    elif device.type == DeviceType.PV:
        hour = _local_hour(now)
        if 8 <= hour <= 17:
            mean_p, n = _recent_power(db, device.id, now)
            if n >= 3 and device.rated_power_kw > 0 and mean_p < 0.05 * device.rated_power_kw:
                fired["pv_zero_daytime"] = (AlertSeverity.WARNING,
                    f"{device.name} 白天近 1.5h 平均出力仅 {mean_p:.2f}kW（装机 {device.rated_power_kw:.1f}kWp），请检查遮挡/逆变器", mean_p)
        power = val("power_kw")
        if power is not None and device.rated_power_kw > 0 and power > device.rated_power_kw * 1.1:
            fired["pv_overrated"] = (AlertSeverity.INFO,
                f"{device.name} 出力 {power:.2f}kW 超过装机，数据异常", power)

    elif device.type == DeviceType.TANK:
        level, inflow, outflow = val("level_pct"), val("inflow_lh"), val("outflow_lh")
        if level is not None and level < DEFAULT_WATER_LOW_PCT:
            sev = AlertSeverity.CRITICAL if level < 10 else AlertSeverity.WARNING
            fired["tank_low"] = (sev, f"{device.name} 液位仅 {level:.0f}%，请尽快补水", level)
        if inflow is not None and outflow is not None and inflow < 50 and outflow > 1500:
            fired["tank_leak"] = (AlertSeverity.CRITICAL,
                f"{device.name} 进水中断仍以 {outflow:.0f}L/h 出水，疑似管网漏损", outflow)
        occupied = ts.occupied_room_count(db, device.property_id, now)
        if occupied == 0 and outflow is not None and outflow > 200:
            fired["tank_idle_outflow"] = (AlertSeverity.WARNING,
                f"{device.name} 无在住仍持续出水 {outflow:.0f}L/h，疑似暗漏或未关阀", outflow)

    result = [
        upsert_alert(db, device, rule, sev, msg, value, now)
        for rule, (sev, msg, value) in fired.items()
    ]
    _resolve_missing(db, device, set(fired), now)
    return result


def evaluate_all(db: Session, now: datetime | None = None) -> list[Alert]:
    """对全部在线设备执行一轮评估（模拟器每拍 / 种子回放后调用）。"""
    now = now or datetime.utcnow()
    out: list[Alert] = []
    for device in db.scalars(select(Device).where(Device.active.is_(True))):
        out.extend(evaluate_device(db, device, now))
    return out
