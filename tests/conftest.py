import os
import tempfile

# 在导入应用前切换到独立测试数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["ENERGY_DATABASE_URL"] = f"sqlite:///{_tmp.name}"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal
from app import models  # noqa: F401
from app.main import app
from app.models import Property, Room, Device, DeviceType, Booking
from app.simulator import DeviceSimulator
from app.services import timeseries as ts
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def local_dt(y, m, d, hh):
    return datetime(y, m, d, hh, tzinfo=CST).astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def world(db):
    """两房一店：空调(+故障空调) / 热水器 / 光伏 / 蓄水池 + 一个入住订单。"""
    p = Property(code="T01", name="测试小院", pv_capacity_kwp=3.0, tank_capacity_l=1000,
                 elec_price=1.2, water_price=4.0)
    db.add(p); db.flush()
    r1 = Room(property_id=p.id, code="R1", name="一号房")
    r2 = Room(property_id=p.id, code="R2", name="二号房")
    db.add_all([r1, r2]); db.flush()

    def mk(code, name, t, room=None, rated=None, fault=None):
        d = Device(code=code, name=name, type=t, property_id=p.id,
                   room_id=room.id if room else None,
                   rated_power_kw=rated or 0.0, fault_profile=fault)
        db.add(d); db.flush()
        return d

    ac = mk("AC1", "一号房空调", DeviceType.AC, r1, rated=1.6)
    heater = mk("WH1", "一号房热水器", DeviceType.HEATER, r1, rated=2.0)
    ac_fault = mk("AC2", "二号房故障空调", DeviceType.AC, r2, rated=1.6,
                  fault="always_on,overpower_fault")
    pv = mk("PV1", "光伏", DeviceType.PV, rated=3.0)
    tank = mk("TK1", "蓄水池", DeviceType.TANK)
    db.commit()
    return dict(p=p, r1=r1, r2=r2, ac=ac, heater=heater, ac_fault=ac_fault, pv=pv, tank=tank)
