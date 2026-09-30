"""API 层：接入、台账、预订、预冷指令、结账、报表的端到端验证。"""
from datetime import datetime, timedelta, timezone
from app.models import Device, Booking, DeviceCommand, CommandStatus
from app.simulator import DeviceSimulator


def _create_property(client, code="P1"):
    r = client.post("/api/properties", json={
        "code": code, "name": "测试海岛民宿", "address": "测试岛",
        "pv_capacity_kwp": 3, "tank_capacity_l": 1000})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_property_room_device_lifecycle(client):
    pid = _create_property(client)
    r = client.post(f"/api/properties/{pid}/rooms", json={"code": "101", "name": "海景房"})
    assert r.status_code == 201
    rid = r.json()["id"]
    r = client.post(f"/api/properties/{pid}/devices", json={
        "code": "AC-1", "name": "空调", "type": "ac", "room_id": rid})
    assert r.status_code == 201
    assert r.json()["rated_power_kw"] == 1.6  # 未填则按类型默认
    r = client.post(f"/api/properties/{pid}/devices", json={
        "code": "PV-1", "name": "光伏", "type": "pv"})
    assert r.status_code == 201 and r.json()["rated_power_kw"] == 3.0

    tree = client.get(f"/api/properties/{pid}").json()
    assert len(tree["rooms"]) == 1 and len(tree["property_devices"]) == 1

    # 重复编码冲突
    r = client.post("/api/properties", json={"code": "P1", "name": "x"})
    assert r.status_code == 409


def test_telemetry_ingest_validation(client):
    pid = _create_property(client, "P2")
    rid = client.post(f"/api/properties/{pid}/rooms", json={"code": "r", "name": "r"}).json()["id"]
    client.post(f"/api/properties/{pid}/devices",
                json={"code": "AC-X", "name": "a", "type": "ac", "room_id": rid})
    ok = client.post("/api/telemetry/readings",
                     json={"device_code": "AC-X", "metric": "room_temp", "value": 27.5})
    assert ok.status_code == 202
    bad_dev = client.post("/api/telemetry/readings",
                          json={"device_code": "GHOST", "metric": "x", "value": 1})
    assert bad_dev.status_code == 400
    bad_metric = client.post("/api/telemetry/readings",
                             json={"device_code": "AC-X", "metric": "gen_kwh", "value": 1})
    assert bad_metric.status_code == 422


def test_booking_overlap_and_precool_dispatch(client, db):
    pid = _create_property(client, "P3")
    rid = client.post(f"/api/properties/{pid}/rooms", json={"code": "r", "name": "r"}).json()["id"]
    client.post(f"/api/properties/{pid}/devices",
                json={"code": "AC-Z", "name": "a", "type": "ac", "room_id": rid})
    now = datetime.utcnow()
    ci = now + timedelta(hours=2)
    co = ci + timedelta(days=2)
    r = client.post("/api/bookings", json={
        "code": "BK1", "room_id": rid, "guest_name": "张三",
        "check_in": ci.isoformat(), "check_out": co.isoformat()})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]

    # 时间冲突
    r2 = client.post("/api/bookings", json={
        "code": "BK2", "room_id": rid, "guest_name": "李四",
        "check_in": (ci + timedelta(hours=1)).isoformat(),
        "check_out": (co).isoformat()})
    assert r2.status_code == 409

    # 下发预冷指令
    r = client.post(f"/api/bookings/{bid}/precool?target_temp=24")
    assert r.status_code == 202 and r.json()["dispatched"] == 1
    cmd = db.query(DeviceCommand).first()
    assert cmd.command == "precool" and cmd.status == CommandStatus.PENDING
    assert cmd.params["target_temp"] == 24.0

    # 模拟器一拍后指令被取走执行
    sim = DeviceSimulator()
    sim.step(now + timedelta(minutes=1), 0.25, db, commit=True)
    db.expire_all()
    assert db.get(DeviceCommand, cmd.id).status == CommandStatus.DELIVERED


def test_checkout_bill_and_monthly_report(client, db, world):
    # 造三天在住数据
    ci = datetime.utcnow() - timedelta(days=3)
    co = datetime.utcnow() - timedelta(minutes=1)
    b = Booking(code="BKT1", room_id=world["r1"].id, guest_name="测试客人",
                guest_count=2, check_in=ci, check_out=co)
    db.add(b); db.commit()
    sim = DeviceSimulator()
    sim.backfill(ci, co, guest_modes={b.id: "normal"}, step_minutes=60)

    r = client.post(f"/api/bookings/{b.id}/checkout")
    assert r.status_code == 200, r.text
    bill = r.json()
    assert bill["elec_kwh"] > 0
    assert bill["water_t"] >= 0
    assert bill["total_fee"] >= 0
    assert 40 <= bill["score"] <= 100
    assert isinstance(bill["advice"], list) and bill["advice"]

    rep = client.get(f"/api/reports/monthly?month={co.strftime('%Y-%m')}").json()
    row = next(x for x in rep["properties"] if x["property_code"] == "T01")
    assert row["bookings"] == 1 and row["meter_elec_kwh"] >= bill["elec_kwh"]
    csv_text = client.get(f"/api/reports/monthly.csv?month={co.strftime('%Y-%m')}").text
    assert "客人用电" in csv_text and "T01" in csv_text


def test_butler_dashboard_shape(client, db, world):
    now = datetime.utcnow()
    # 今日入住（东八区日历内）
    ci_local = now.astimezone(timezone(timedelta(hours=8))).replace(hour=15, minute=0, second=0, microsecond=0)
    ci = ci_local.astimezone(timezone.utc).replace(tzinfo=None)
    if ci < now:  # 若当前已过 15 点则放到明天，确保“今日”
        ci += timedelta(days=1)
    co = ci + timedelta(days=1)
    db.add(Booking(code="BKDASH", room_id=world["r1"].id, guest_name="看板客人",
                   check_in=ci, check_out=co))
    db.commit()
    d = client.get("/api/butler/dashboard").json()
    assert "precool_pending" in d and "open_alerts" in d and "tank_status" in d
