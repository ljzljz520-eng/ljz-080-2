"""Pure billing and energy-saving recommendation rules."""
from __future__ import annotations

from typing import Any, Iterable

AC_BASE_KWH_PER_GUEST_NIGHT = 1.8
WATER_BASE_LITERS_PER_GUEST_NIGHT = 110
HEATER_BASE_KWH_PER_GUEST_NIGHT = 0.75


def sum_positive_delta(rows: Iterable[dict[str, Any]], field: str) -> float:
    """Sum cumulative meter deltas; gaps and rollbacks are ignored conservatively."""
    previous = None
    total = 0.0
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        value = float(value)
        if previous is not None and value >= previous:
            total += value - previous
        previous = value
    return total


def average_setpoint(rows: Iterable[dict[str, Any]]) -> float | None:
    values = [float(r["set_temperature"]) for r in rows if r.get("set_temperature") is not None]
    return round(sum(values) / len(values), 2) if values else None


def guest_nights(guest_count: int, stay_hours: float) -> float:
    nights = max(stay_hours / 24, 1 / 24)
    return max(1, round(guest_count * nights, 2))


def allocatable_pv(pv_generated: float, electricity: float) -> float:
    """PV credit is limited by property self-consumption during the stay window."""
    return round(min(pv_generated, electricity), 2)


def build_bill_totals(
    electricity_kwh: float,
    water_liters: float,
    pv_self_consumed_kwh: float,
    electricity_price: float,
    water_price: float,
    guest_count: int,
    stay_hours: float,
    ac_avg_setpoint: float | None = None,
    open_anomaly_count: int = 0,
) -> dict[str, Any]:
    nights = guest_nights(guest_count, stay_hours)
    grid_kwh = max(electricity_kwh - pv_self_consumed_kwh, 0)
    electricity_fee = round(grid_kwh * electricity_price, 2)
    water_fee = round(water_liters / 1000 * water_price, 2)
    saved_fee = round(pv_self_consumed_kwh * electricity_price, 2)
    return {
        "guest_nights": nights,
        "grid_electricity_kwh": round(grid_kwh, 2),
        "electricity_fee": electricity_fee,
        "water_fee": water_fee,
        "total_fee": round(electricity_fee + water_fee, 2),
        "saved_fee": saved_fee,
        "recommendations": build_recommendations(
            electricity_kwh, water_liters, pv_self_consumed_kwh,
            nights, ac_avg_setpoint, open_anomaly_count
        ),
    }


def build_recommendations(
    electricity_kwh: float,
    water_liters: float,
    pv_self_consumed_kwh: float,
    guest_night_count: float,
    ac_avg_setpoint: float | None = None,
    open_anomaly_count: int = 0,
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    if ac_avg_setpoint is not None and ac_avg_setpoint < 25:
        save_kwh = max(electricity_kwh * 0.08, 0)
        recommendations.append({
            "code": "ac_setpoint_26",
            "title": "空调默认调到26℃并开启睡眠模式",
            "detail": "本次平均设定温度偏低；每提高1℃约可降低8%空调电耗。",
            "estimated_save_kwh": round(save_kwh, 2),
        })
    ac_benchmark = AC_BASE_KWH_PER_GUEST_NIGHT * guest_night_count
    if electricity_kwh > ac_benchmark + 1.0:
        recommendations.append({
            "code": "high_electricity",
            "title": "核查长开电器和异常耗电设备",
            "detail": f"本次用电{electricity_kwh:.1f}kWh，高于按{guest_night_count:.1f}人晚估算的基准{ac_benchmark:.1f}kWh。",
            "estimated_save_kwh": round(max(electricity_kwh - ac_benchmark, 0) * 0.2, 2),
        })
    water_benchmark = WATER_BASE_LITERS_PER_GUEST_NIGHT * guest_night_count
    if water_liters > water_benchmark * 1.15:
        recommendations.append({
            "code": "water_saving",
            "title": "提示短淋浴并检查花洒/马桶漏水",
            "detail": f"本次用水{water_liters:.0f}L，高于基准{water_benchmark:.0f}L。",
            "estimated_save_liters": round((water_liters - water_benchmark) * 0.15, 1),
        })
    if electricity_kwh > 0 and pv_self_consumed_kwh / max(electricity_kwh, 1) < 0.25:
        recommendations.append({
            "code": "shift_to_pv",
            "title": "把热水、洗衣等可延迟负荷调到中午光伏时段",
            "detail": "本次光伏自发自用占比较低，海岛日间光伏可替代更多市电。",
            "estimated_save_kwh": round(electricity_kwh * 0.06, 2),
        })
    if open_anomaly_count:
        recommendations.append({
            "code": "maintenance",
            "title": "优先处理未关闭的设备告警",
            "detail": f"账单周期内仍有{open_anomaly_count}条设备异常，可能影响舒适度和能耗。",
            "estimated_save_kwh": 0,
        })
    if not recommendations:
        recommendations.append({
            "code": "good",
            "title": "本次用能表现良好",
            "detail": "继续保持26℃空调、随人关电和错峰使用热水的习惯。",
            "estimated_save_kwh": 0,
        })
    return recommendations
