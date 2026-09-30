"""一键初始化演示数据：
3 个海岛房源 / 6 间房 / 15 台设备（含故障画像）/ 7 个预订，
并用模拟器从本月 1 日前两天逐小时回放到当前，再为已离店订单出账。
"""
import sys
from datetime import datetime, timedelta, timezone

from .database import Base, engine, SessionLocal
from .models import Property, Room, Device, DeviceType, Booking
from .simulator import DeviceSimulator
from .services.billing import build_bill

CST = timezone(timedelta(hours=8))


def local_dt(y, m, d, hh, mm=0) -> datetime:
    """本地时间(东八区) → 存储用 UTC naive。"""
    return datetime(y, m, d, hh, mm, tzinfo=CST).astimezone(timezone.utc).replace(tzinfo=None)


def run(progress: bool = True):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    guest_modes = {}
    try:
        # ---------------- 房源 ----------------
        p1 = Property(code="ISL01", name="听涛海岛小院", address="东澳岛南澳湾 1 号",
                      pv_capacity_kwp=5.0, tank_capacity_l=2000, elec_price=1.20, water_price=4.0)
        p2 = Property(code="ISL02", name="椰风海景民宿", address="外伶仃岛天祥路 8 号",
                      pv_capacity_kwp=6.0, tank_capacity_l=2500, elec_price=1.35, water_price=4.5)
        p3 = Property(code="ISL03", name="珊瑚湾石屋", address="桂山岛珊瑚湾 3 号",
                      pv_capacity_kwp=4.0, tank_capacity_l=1500, elec_price=1.10, water_price=3.8)
        db.add_all([p1, p2, p3]); db.flush()

        def room(p, code, name):
            r = Room(property_id=p.id, code=code, name=name)
            db.add(r); db.flush(); return r

        r101 = room(p1, "101", "海院大床房"); r102 = room(p1, "102", "海院双床房")
        r201 = room(p2, "201", "海景亲子房"); r202 = room(p2, "202", "海景露台房")
        r301 = room(p3, "301", "石屋家庭房"); r302 = room(p3, "302", "石屋阁楼")
        rooms = [r101, r102, r201, r202, r301, r302]

        # ---------------- 设备 ----------------
        def dev(p, dtype, code, name, room_obj=None, rated=None, fault=None):
            if rated is None:
                rated = {"ac": 1.6, "heater": 2.0, "pv": p.pv_capacity_kwp, "tank": 0.0}[dtype.value]
            d = Device(code=code, name=name, type=dtype, property_id=p.id,
                       room_id=room_obj.id if room_obj else None,
                       rated_power_kw=rated, fault_profile=fault)
            db.add(d)
            return d

        for r in rooms:
            dev(r.property, DeviceType.AC, f"AC-{r.property.code}-{r.code}", f"{r.name}空调", room_obj=r)
            dev(r.property, DeviceType.HEATER, f"WH-{r.property.code}-{r.code}", f"{r.name}热水器", room_obj=r)
        # 故障演示设备
        dev(p1, DeviceType.AC, "AC-ISL01-FAULT", "海院备用空调(故障)", room_obj=r102,
            fault="always_on,overpower_fault")
        dev(p2, DeviceType.HEATER, "WH-ISL02-FAULT", "201备用热水器(卡滞)", room_obj=r201,
            fault="heater_stuck")
        dev(p3, DeviceType.AC, "AC-ISL03-FAULT", "302空调(设定异常)", room_obj=r302,
            fault="always_on,low_target")
        dev(p1, DeviceType.PV, "PV-ISL01", "听涛小院光伏", rated=5.0)
        dev(p2, DeviceType.PV, "PV-ISL02", "椰风光伏(检修)", rated=6.0, fault="pv_fault")
        dev(p3, DeviceType.PV, "PV-ISL03", "珊瑚湾光伏", rated=4.0)
        dev(p1, DeviceType.TANK, "TK-ISL01", "听涛蓄水池")
        dev(p2, DeviceType.TANK, "TK-ISL02", "椰风蓄水池")
        dev(p3, DeviceType.TANK, "TK-ISL03", "珊瑚湾蓄水池(漏损)", fault="tank_leak")
        db.flush()

        # ---------------- 预订 ----------------
        now = datetime.now(timezone.utc)
        local_now = now.astimezone(CST)
        y, m = local_now.year, local_now.month
        first = datetime(y, m, 1, tzinfo=CST)

        def bk(room_obj, code, name, ci_day, co_day, guests=2, mode=None,
               ci_month=None, co_month=None):
            ci_month = ci_month or m; co_month = co_month or m
            b = Booking(code=code, room_id=room_obj.id, guest_name=name, guest_count=guests,
                        check_in=local_dt(y, ci_month, ci_day, 14),
                        check_out=local_dt(y, co_month, co_day, 11),
                        guest_phone="138****0000")
            db.add(b); db.flush()
            if mode:
                guest_modes[b.id] = mode
            return b

        b1 = bk(r101, "BK26090101", "周先生", 2, 4, 2, "wasteful")
        b2 = bk(r201, "BK26090201", "林女士一家", 4, 6, 3, "normal")
        b3 = bk(r301, "BK26090301", "陈同学", 8, 9, 1, "eco")
        b4 = bk(r102, "BK26090401", "吴女士一家", 12, 16, 4, "normal")
        b5 = bk(r202, "BK26090501", "黄先生", 18, 21, 2, "eco")
        b6 = bk(r302, "BK26090601", "郑女士", 22, 25, 2, "wasteful")
        # 今日入住（管家待预冷），离店 = 3 天后（正确处理跨月）
        ci_local = local_dt(y, m, local_now.day, 14)
        co_local_dt = (ci_local + timedelta(days=3)).astimezone(CST)
        active = Booking(code="BK26093001", room_id=r201.id, guest_name="今日到店·何先生",
                         guest_count=2, check_in=ci_local,
                         check_out=local_dt(co_local_dt.year, co_local_dt.month,
                                            co_local_dt.day, 11),
                         guest_phone="139****8888", precool_requested=True)
        db.add(active); db.commit()
        guest_modes[active.id] = "normal"

        # ---------------- 历史回放 ----------------
        start = (first - timedelta(days=2)).astimezone(timezone.utc).replace(tzinfo=None)
        end = now.replace(tzinfo=None)
        if progress:
            print(f"回放 {start} ~ {end}（小时级）...")
        sim = DeviceSimulator()
        sim.backfill(start, end, guest_modes=guest_modes, step_minutes=60, progress=progress)

        # ---------------- 已离店订单出账 ----------------
        if progress:
            print("生成历史账单 ...")
        for b in [b1, b2, b3, b4, b5, b6]:
            build_bill(db, b, checkout=True)
        db.commit()
        if progress:
            print("种子数据完成。今日待入住:", active.code, "入住时间(本地):",
                  active.check_in.astimezone(CST).strftime("%Y-%m-%d %H:%M"))
    finally:
        db.close()


if __name__ == "__main__":
    run(progress="--quiet" not in sys.argv)
