"""结账计费：读表差额 → 公用水按入住时分摊 → 光伏抵扣 → 账单 + 节能建议。"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Device, DeviceType, Booking, Room, Bill, Property, Reading,
)
from ..config import PV_FEEDIN_PRICE
from . import timeseries as ts


# ---------------- 公用水 / 光伏分摊 ----------------
def _intervals(start, end, step=timedelta(hours=1)):
    t = start
    while t < end:
        nxt = min(t + step, end)
        yield t, nxt
        t = nxt


def water_share(db: Session, booking: Booking, prop_id: int) -> tuple[float, float, float]:
    """
    按「在住时刻 1/同店在住房间数」的时间积分，分摊房源蓄水池总出水。
    若蓄水池带漏损故障（漏量不应由客人承担），改用住客用水模型估算。
    返回 (本单用水吨, 计入分摊的总出水L, 份额0-1)。
    """
    tank = db.scalar(select(Device).where(
        Device.property_id == prop_id, Device.type == DeviceType.TANK
    ))
    if tank is None:
        return 0.0, 0.0, 0.0

    if tank.fault_profile and "tank_leak" in tank.fault_profile:
        est_l = _estimate_guest_water_l(db, booking)
        return est_l / 1000.0, est_l, 1.0

    total_l = ts.delta_total(db, tank.id, "out_l", booking.check_in, booking.check_out)
    if total_l <= 0:
        return 0.0, 0.0, 0.0

    weight_hours = 0.0
    # 遍历同时段同店所有预订，按区间分段求并发
    others = list(db.scalars(
        select(Booking).join(Room, Room.id == Booking.room_id)
        .where(Room.property_id == prop_id, Booking.check_in < booking.check_out,
               Booking.check_out > booking.check_in)
    ))
    for a, b in _intervals(booking.check_in, booking.check_out):
        mid = a + (b - a) / 2
        conc = sum(1 for o in others if o.check_in <= mid < o.check_out)
        weight_hours += (b - a).total_seconds() / 3600.0 * (1.0 / conc if conc else 1.0)

    stay_hours = (booking.check_out - booking.check_in).total_seconds() / 3600.0
    share = weight_hours / stay_hours if stay_hours else 1.0
    return total_l * share / 1000.0, total_l, share


# 与模拟器一致的逐时用水基线 L/h
def _draw_baseline(hour: int) -> float:
    if 7 <= hour < 9:
        return 48.0
    if 19 <= hour < 23:
        return 58.0
    if 23 <= hour or hour < 6:
        return 2.0
    return 9.0


def _estimate_guest_water_l(db: Session, booking: Booking) -> float:
    """漏损房源：按在住人数 × 逐时基线估算正常用水（每人 0.55 系数）。"""
    from ..config import TIMEZONE
    total = 0.0
    t = booking.check_in
    while t < booking.check_out:
        hour = t.replace(tzinfo=timezone.utc).astimezone(TIMEZONE).hour
        total += _draw_baseline(hour) * 0.55 * booking.guest_count
        t += timedelta(hours=1)
    return total



def _hourly_deltas(db, device_id: int, metric: str, start: datetime, end: datetime,
                   step: timedelta = timedelta(hours=1)) -> list[tuple[datetime, float]]:
    """逐小时累计总表增量（用相邻读数差）。"""
    rows = db.execute(
        select(Reading.ts, Reading.value)
        .where(Reading.device_id == device_id, Reading.metric == metric,
               Reading.ts >= start - timedelta(hours=1), Reading.ts <= end)
        .order_by(Reading.ts.asc())
    ).all()
    if len(rows) < 2:
        return []
    out = []
    for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
        if start <= t1 <= end:
            out.append((t1, max(0.0, v1 - v0)))
    return out


def pv_share(db: Session, booking: Booking, prop: Property) -> float:
    """
    光伏自用抵扣：在本单在住的每个小时内，
    该小时光伏发电量 × 本单当前用电 / 同店同时在住订单总用电，逐小时求和封顶为本单用电。
    """
    pv = db.scalar(select(Device).where(
        Device.property_id == prop.id, Device.type == DeviceType.PV
    ))
    if pv is None:
        return 0.0
    overlaps = list(db.scalars(
        select(Booking).join(Room, Room.id == Booking.room_id)
        .where(Room.property_id == prop.id, Booking.check_in < booking.check_out,
               Booking.check_out > booking.check_in)
    ))
    # 每个重叠订单的逐小时用电序列（设备级求和）
    room_elec_rows = {}
    for b in overlaps:
        acc: dict[datetime, float] = {}
        for d in db.scalars(select(Device).where(
                Device.room_id == b.room_id,
                Device.type.in_([DeviceType.AC, DeviceType.HEATER]),
                Device.fault_profile.is_(None))):
            for t, dv in _hourly_deltas(db, d.id, "energy_kwh", booking.check_in, booking.check_out):
                acc[t] = acc.get(t, 0.0) + dv
        room_elec_rows[b.id] = acc

    allocated = 0.0
    for t, gen in _hourly_deltas(db, pv.id, "gen_kwh", booking.check_in, booking.check_out):
        active = {b.id: room_elec_rows[b.id].get(t, 0.0)
                  for b in overlaps if b.check_in <= t < b.check_out}
        total_use = sum(active.values())
        own_use = active.get(booking.id, 0.0)
        if total_use > 0 and gen > 0:
            allocated += min(gen, total_use) * own_use / total_use
    # 封顶不超过本单实际用电
    own_total, _ = room_elec(db, booking.room_id, booking.check_in, booking.check_out)
    return round(min(allocated, own_total), 2)


def room_elec(db: Session, room_id: int, start: datetime, end: datetime) -> tuple[float, dict]:
    """房间空调+热水器用电（读表差），返回 (kWh, 分项)。"""
    detail = {"ac": [], "heater": []}
    total = 0.0
    for d in db.scalars(select(Device).where(Device.room_id == room_id,
                          Device.type.in_([DeviceType.AC, DeviceType.HEATER]),
                          Device.active.is_(True))):
        # 故障画像设备（如卡滞/偷电）产生的能耗不计入客人账单
        if d.fault_profile:
            detail["ac" if d.type == DeviceType.AC else "heater"].append(
                {"device_id": d.id, "name": d.name, "kwh": 0.0,
                 "excluded": True, "reason": "设备故障能耗由房东承担"})
            continue
        kwh = ts.delta_total(db, d.id, "energy_kwh", start, end)
        detail["ac" if d.type == DeviceType.AC else "heater"].append(
            {"device_id": d.id, "name": d.name, "kwh": round(kwh, 2)})
        total += kwh
    return total, detail


# ---------------- 节能建议与评分 ----------------
def _advice(elec_kwh, water_t, pv_kwh, nights, guests, device_detail, mean_targets) -> tuple[list[str], int]:
    score = 100
    tips: list[str] = []
    per_night = elec_kwh / nights if nights else elec_kwh
    water_per_gn = water_t * 1000 / (nights * guests) if nights * guests else 0.0

    if per_night > 12:
        score -= 30
        tips.append(f"空调用电偏高（{per_night:.1f} 度/晚，建议值 ≤12）：外出请关闭空调，温度设 26℃，开启抽湿模式比强冷更省电。")
    elif per_night > 8:
        score -= 12
        tips.append(f"空调用电 {per_night:.1f} 度/晚，略高于同店均值，建议温度上调 1-2℃。")

    heater_kwh = sum(x["kwh"] for x in device_detail.get("heater", []))
    if heater_kwh / nights > 3:
        score -= 15
        tips.append(f"热水用电 {heater_kwh:.1f} 度偏高：建议洗澡前 30 分钟加热，水温 45℃ 即可，避免全天保温耗电。")

    if pv_kwh < 1 and elec_kwh > 3:
        score -= 10
        tips.append("本次入住几乎未使用光伏绿电：可提醒管家在日照时段（9:00-16:00）安排热水预加热，优先消纳光伏发电。")

    if water_per_gn > 220:
        score -= 20
        tips.append(f"人均用水 {water_per_gn:.0f} 升/晚偏高：海水资源紧张，建议缩短淋浴时间，重复利用冲厕与浇淋用水。")
    elif water_per_gn > 160:
        score -= 8
        tips.append(f"人均用水 {water_per_gn:.0f} 升/晚，略高，可放置节水提示卡。")

    if mean_targets and min(mean_targets) <= 20:
        score -= 8
        tips.append("检测到空调温度设定 ≤20℃，每调高 1℃ 约可省电 6%-8%。")

    if not tips:
        tips.append("本次入住能耗表现优秀，保持随手关空调、错峰用热水的习惯即可。")
    if pv_kwh >= max(2.0, elec_kwh * 0.2):
        score += 5
    return tips, max(40, min(100, score))


def _mean_targets(db: Session, room_id: int, start, end) -> list[float]:
    out = []
    for d in db.scalars(select(Device).where(Device.room_id == room_id, Device.type == DeviceType.AC)):
        m = ts.mean_value(db, d.id, "target_temp", start, end)
        if m is not None:
            out.append(m)
    return out


# ---------------- 出账 ----------------
def build_bill(db: Session, booking: Booking, now: datetime | None = None,
               checkout: bool = False) -> Bill:
    now = now or datetime.utcnow()
    room = db.get(Room, booking.room_id)
    prop = db.get(Property, room.property_id)
    end = min(booking.check_out, now) if not checkout else booking.check_out

    existing = db.scalar(select(Bill).where(Bill.booking_id == booking.id))
    if existing is not None and not checkout:
        return existing

    elec_kwh, device_detail = room_elec(db, booking.room_id, booking.check_in, end)
    water_t, total_out_l, share = water_share(db, booking, prop.id)
    pv_kwh = pv_share(db, booking, prop)

    nights = max(1, (booking.check_out.date() - booking.check_in.date()).days)
    mean_targets = _mean_targets(db, booking.room_id, booking.check_in, end)
    tips, score = _advice(elec_kwh, water_t, pv_kwh, nights, booking.guest_count,
                          device_detail, mean_targets)

    elec_fee = elec_kwh * prop.elec_price
    water_fee = water_t * prop.water_price
    # 光伏抵扣按购电电价计；若理论抵扣超过本单电费则按余电上网价折算剩余部分
    if pv_kwh <= elec_kwh:
        pv_offset = pv_kwh * prop.elec_price
    else:
        pv_offset = elec_kwh * prop.elec_price + (pv_kwh - elec_kwh) * PV_FEEDIN_PRICE
    total_fee = round(elec_fee + water_fee - pv_offset, 2)

    bill = existing or Bill(booking_id=booking.id, property_id=prop.id,
                            code=f"BL{booking.code[2:] if booking.code.startswith('BK') else booking.code}")
    bill.period_start = booking.check_in
    bill.period_end = end
    bill.elec_kwh = round(elec_kwh, 2)
    bill.water_t = round(water_t, 3)
    bill.pv_kwh = round(pv_kwh, 2)
    bill.elec_fee = round(elec_fee, 2)
    bill.water_fee = round(water_fee, 2)
    bill.pv_offset_fee = round(pv_offset, 2)
    bill.total_fee = total_fee
    bill.detail = {
        "nights": nights,
        "guest_count": booking.guest_count,
        "elec_price": prop.elec_price,
        "water_price": prop.water_price,
        "devices": device_detail,
        "tank_out_l": round(total_out_l, 1),
        "water_share": round(share, 4),
        "elec_per_night": round(elec_kwh / nights, 2),
        "water_l_per_guest_night": round(water_t * 1000 / (nights * booking.guest_count), 1),
    }
    bill.score = score
    bill.advice = tips
    db.add(bill)
    if checkout:
        booking.checked_out_at = now
    db.flush()
    return bill


def checkout(db: Session, booking_id: int, now: datetime | None = None) -> Bill:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise ValueError("预订不存在")
    return build_bill(db, booking, now=now, checkout=True)
