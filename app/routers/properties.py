"""房源 / 房间 / 设备台账与拓扑。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Property, Room, Device
from ..schemas import PropertyIn, PropertyOut, RoomIn, RoomOut, DeviceIn, DeviceOut, DeviceStateOut
from ..services import timeseries as ts

router = APIRouter(prefix="/api", tags=["台账"])


@router.post("/properties", response_model=PropertyOut, status_code=201)
def create_property(payload: PropertyIn, db: Session = Depends(get_db)):
    if db.scalar(select(Property).where(Property.code == payload.code)):
        raise HTTPException(409, f"房源编号 {payload.code} 已存在")
    p = Property(**payload.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("/properties", response_model=list[PropertyOut])
def list_properties(db: Session = Depends(get_db)):
    return list(db.scalars(select(Property).order_by(Property.id)))


@router.get("/properties/{property_id}")
def property_tree(property_id: int, db: Session = Depends(get_db)):
    """房源概览：房间 + 设备 + 每台设备最新状态。"""
    prop = db.get(Property, property_id)
    if prop is None:
        raise HTTPException(404, "房源不存在")
    rooms = list(db.scalars(select(Room).where(Room.property_id == property_id).order_by(Room.id)))
    devs = list(db.scalars(select(Device).where(Device.property_id == property_id).order_by(Device.id)))
    room_map = {r.id: {"id": r.id, "code": r.code, "name": r.name, "devices": []} for r in rooms}
    property_devices = []
    for d in devs:
        states = [
            DeviceStateOut(metric=m, value=s.value, ts=s.ts)
            for m, s in sorted(ts.state_map(db, d.id).items())
        ]
        item = {
            "id": d.id, "code": d.code, "name": d.name, "type": d.type.value,
            "room_id": d.room_id, "rated_power_kw": d.rated_power_kw,
            "fault_profile": d.fault_profile, "active": d.active, "states": states,
        }
        (room_map[d.room_id]["devices"] if d.room_id in room_map else property_devices).append(item)
    return {
        "id": prop.id, "code": prop.code, "name": prop.name, "address": prop.address,
        "pv_capacity_kwp": prop.pv_capacity_kwp, "tank_capacity_l": prop.tank_capacity_l,
        "elec_price": prop.elec_price, "water_price": prop.water_price,
        "rooms": list(room_map.values()), "property_devices": property_devices,
    }


@router.post("/properties/{property_id}/rooms", response_model=RoomOut, status_code=201)
def create_room(property_id: int, payload: RoomIn, db: Session = Depends(get_db)):
    if db.get(Property, property_id) is None:
        raise HTTPException(404, "房源不存在")
    if db.scalar(select(Room).where(Room.property_id == property_id, Room.code == payload.code)):
        raise HTTPException(409, "房间编号已存在")
    r = Room(property_id=property_id, **payload.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.post("/properties/{property_id}/devices", response_model=DeviceOut, status_code=201)
def create_device(property_id: int, payload: DeviceIn, db: Session = Depends(get_db)):
    from ..models import DeviceType
    prop = db.get(Property, property_id)
    if prop is None:
        raise HTTPException(404, "房源不存在")
    if db.scalar(select(Device).where(Device.code == payload.code)):
        raise HTTPException(409, "设备编码已存在")
    try:
        dtype = DeviceType(payload.type)
    except ValueError:
        raise HTTPException(400, "type 必须是 ac/heater/pv/tank")
    if payload.room_id is not None and db.get(Room, payload.room_id) is None:
        raise HTTPException(400, "关联房间不存在")
    rated = payload.rated_power_kw
    if rated == 0.0:
        rated = {"ac": 1.6, "heater": 2.0, "pv": prop.pv_capacity_kwp, "tank": 0.0}[dtype.value]
    d = Device(code=payload.code, name=payload.name, type=dtype, property_id=property_id,
               room_id=payload.room_id, rated_power_kw=rated, fault_profile=payload.fault_profile)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d
