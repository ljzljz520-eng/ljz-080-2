"""规则引擎、计费、时序与预冷状态的服务级测试。"""
from datetime import datetime, timedelta

from app.models import Booking, Device, Alert, AlertStatus, DeviceCommand
from app.services import timeseries as ts
from app.services.alerts import evaluate_device, evaluate_all
from app.services import billing as billing_svc
from app.services import butler
from app.simulator import DeviceSimulator


def test_overpower_and_vacant_alerts_fire(db, world):
    sim = DeviceSimulator()
    now = datetime(2026, 9, 30, 4, 0)  # UTC 12:00 本地
    t = now - timedelta(hours=3)
    while t < now:
        sim.step(t, 1.0, db, commit=False)
        t += timedelta(hours=1)
    db.commit()
    alerts = evaluate_device(db, world["ac_fault"], now)
    rules = {a.rule for a in alerts}
    assert "ac_overpower" in rules      # 2.0x 额定 → 超功率
    assert "ac_vacant_running" in rules  # 二号房无人但空调常开


def test_vacant_alert_suppressed_by_precool_window(db, world):
    """入住前预冷窗口内空房开空调不应报「空房运行」。"""
    now = datetime(2026, 9, 30, 4, 0)
    ci = now + timedelta(minutes=30)
    db.add(Booking(code="BKPRE", room_id=world["r1"].id, guest_name="即将到店",
                   check_in=ci, check_out=ci + timedelta(days=1), precool_requested=True))
    db.commit()
    # 给一号房空调下发预冷并跑一拍
    butler.start_precool(db, db.query(Booking).filter_by(code="BKPRE").first().id, target_c=24, now=now)
    sim = DeviceSimulator()
    sim.step(now, 0.25, db, commit=True)
    alerts = evaluate_device(db, world["ac"], now)
    assert not any(a.rule == "ac_vacant_running" for a in alerts)


def test_precool_status_transitions(db, world):
    """pending（未开）→ cooling（开机中）→ done（室温达标）。"""
    now = datetime(2026, 9, 30, 4, 0)
    ci = now + timedelta(hours=2)
    b = Booking(code="BKP", room_id=world["r1"].id, guest_name="何先生",
                check_in=ci, check_out=ci + timedelta(days=1))
    db.add(b); db.commit()
    item = butler.precool_status(db, b, now)
    assert item.status in ("pending", "missed")

    # 下发预冷并连续推进到室温达标
    butler.start_precool(db, b.id, target_c=24, now=now)
    sim = DeviceSimulator()
    t = now
    for _ in range(30):  # 30 × 5 分钟 = 2.5 小时
        sim.step(t, 5 / 60, db, commit=False)
        t += timedelta(minutes=5)
    db.commit()
    item = butler.precool_status(db, b, t)
    assert item.ac_on and item.room_temp <= 25
    assert item.status in ("cooling", "done")


def test_alert_auto_resolves_when_condition_clears(db, world):
    now = datetime(2026, 9, 30, 4, 0)
    sim = DeviceSimulator()
    t = now - timedelta(hours=3)
    while t < now:
        sim.step(t, 1.0, db, commit=False); t += timedelta(hours=1)
    db.commit()
    alerts = evaluate_device(db, world["ac_fault"], now)
    assert any(a.rule == "ac_overpower" for a in alerts)
    # 解除故障画像 → 下一轮评估应自动 resolve
    world["ac_fault"].fault_profile = None
    db.commit()
    sim2 = DeviceSimulator()
    t2 = now
    for _ in range(4):
        sim2.step(t2, 1.0, db, commit=False); t2 += timedelta(hours=1)
    db.commit()
    evaluate_device(db, world["ac_fault"], t2)
    db.commit()
    open_rules = {a.rule for a in db.query(Alert).filter(
        Alert.device_id == world["ac_fault"].id,
        Alert.status != AlertStatus.RESOLVED)}
    assert "ac_overpower" not in open_rules


def test_billing_delta_and_fault_device_excluded(db, world):
    ci = datetime(2026, 9, 10, 6, 0)   # 本地 14:00
    co = datetime(2026, 9, 12, 3, 0)   # 本地 11:00
    b = Booking(code="BKB", room_id=world["r1"].id, guest_name="账单客人",
                guest_count=2, check_in=ci, check_out=co)
    db.add(b); db.commit()
    DeviceSimulator().backfill(ci - timedelta(days=1), co + timedelta(hours=1),
                               guest_modes={b.id: "eco"}, step_minutes=60)
    bill = billing_svc.checkout(db, b.id, now=co)
    assert bill.elec_kwh > 0
    assert bill.pv_kwh <= bill.elec_kwh + 1e-6      # 抵扣不超过自身用电
    # 故障设备（二号房空调）不体现在一号房账单
    devices_in_detail = {x["device_id"] for grp in bill.detail["devices"].values() for x in grp}
    assert world["ac_fault"].id not in devices_in_detail
    assert bill.total_fee == round(bill.elec_fee + bill.water_fee - bill.pv_offset_fee, 2)


def test_water_share_splits_concurrent_bookings(db, world):
    """两个同时在住订单，蓄水池出水应近似对半分。"""
    ci = datetime(2026, 9, 15, 6, 0)
    co = datetime(2026, 9, 16, 6, 0)
    b1 = Booking(code="BWS1", room_id=world["r1"].id, guest_name="A", guest_count=2,
                 check_in=ci, check_out=co)
    b2 = Booking(code="BWS2", room_id=world["r2"].id, guest_name="B", guest_count=2,
                 check_in=ci, check_out=co)
    db.add_all([b1, b2]); db.commit()
    DeviceSimulator().backfill(ci - timedelta(hours=2), co + timedelta(hours=2),
                               guest_modes={b1.id: "normal", b2.id: "normal"}, step_minutes=60)
    db.expire_all()
    w1, total1, s1 = billing_svc.water_share(db, db.get(Booking, b1.id), world["p"].id)
    w2, total2, s2 = billing_svc.water_share(db, db.get(Booking, b2.id), world["p"].id)
    assert abs(s1 - 0.5) < 0.05 and abs(s2 - 0.5) < 0.05
    assert abs(w1 - w2) / max(w1, 1e-9) < 0.02


def test_manual_command_turns_off_ac(db, world):
    now = datetime(2026, 9, 30, 4, 0)
    butler.send_command(db, world["ac_fault"].id, "turn_off", {})
    db.commit()
    sim = DeviceSimulator()
    out = sim.step(now, 0.25, db, commit=True)
    # 30 分钟人工关断窗口内功率应为 0
    assert out[world["ac_fault"].id]["power_kw"] == 0.0
