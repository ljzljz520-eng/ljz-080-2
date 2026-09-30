"""领域模型：房源 / 房间 / 设备 / 预订 / 遥测 / 状态 / 账单 / 告警 / 指令。"""
import enum
from datetime import datetime
from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Date, ForeignKey, Text, Index, Enum, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


# ---------- 枚举 ----------
class DeviceType(str, enum.Enum):
    AC = "ac"           # 空调
    HEATER = "heater"   # 热水器
    PV = "pv"           # 光伏
    TANK = "tank"       # 蓄水池（房源级水表）

    @property
    def label(self):
        return {"ac": "空调", "heater": "热水器", "pv": "光伏", "tank": "蓄水池"}[self.value]


class AlertSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(str, enum.Enum):
    OPEN = "open"
    ACK = "ack"
    RESOLVED = "resolved"


class CommandStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DONE = "done"
    FAILED = "failed"


# ---------- 实体 ----------
class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(200), default="")
    pv_capacity_kwp: Mapped[float] = mapped_column(Float, default=5.0)
    tank_capacity_l: Mapped[float] = mapped_column(Float, default=2000.0)
    elec_price: Mapped[float] = mapped_column(Float, default=1.2)   # 元/kWh
    water_price: Mapped[float] = mapped_column(Float, default=4.0)  # 元/吨
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    rooms: Mapped[list["Room"]] = relationship(back_populates="property", cascade="all, delete-orphan")
    devices: Mapped[list["Device"]] = relationship(back_populates="property", cascade="all, delete-orphan")


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))

    property: Mapped["Property"] = relationship(back_populates="rooms")
    devices: Mapped[list["Device"]] = relationship(back_populates="room")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="room")

    __table_args__ = (Index("ux_room_property_code", "property_id", "code", unique=True),)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[DeviceType] = mapped_column(Enum(DeviceType))
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True, index=True)
    rated_power_kw: Mapped[float] = mapped_column(Float, default=0.0)  # 额定/装机功率
    fault_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 模拟器故障画像
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    property: Mapped["Property"] = relationship(back_populates="devices")
    room: Mapped["Room | None"] = relationship(back_populates="devices")
    states: Mapped[list["DeviceState"]] = relationship(back_populates="device", cascade="all, delete-orphan")


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    guest_name: Mapped[str] = mapped_column(String(64))
    guest_phone: Mapped[str] = mapped_column(String(32), default="")
    guest_count: Mapped[int] = mapped_column(Integer, default=2)
    check_in: Mapped[datetime] = mapped_column(DateTime, index=True)
    check_out: Mapped[datetime] = mapped_column(DateTime, index=True)
    precool_requested: Mapped[bool] = mapped_column(Boolean, default=True)
    precool_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    room: Mapped["Room"] = relationship(back_populates="bookings")
    bill: Mapped["Bill | None"] = relationship(back_populates="booking", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (Index("ux_booking_time", "check_in", "check_out"),)


class Reading(Base):
    """设备遥测读数（时序）。累计量是单调递增总表读数。"""
    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float] = mapped_column(Float)

    __table_args__ = (
        Index("ix_readings_device_metric_ts", "device_id", "metric", "ts"),
    )


class DeviceState(Base):
    """设备最新状态（每个 metric 一行的最新值快照，供看板快速查询）。"""
    __tablename__ = "device_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime)

    device: Mapped["Device"] = relationship(back_populates="states")

    __table_args__ = (Index("ux_state_device_metric", "device_id", "metric", unique=True),)


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), unique=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)

    elec_kwh: Mapped[float] = mapped_column(Float, default=0.0)       # 空调+热水器用电
    water_t: Mapped[float] = mapped_column(Float, default=0.0)        # 分摊用水（吨）
    pv_kwh: Mapped[float] = mapped_column(Float, default=0.0)         # 光伏抵扣电量
    elec_fee: Mapped[float] = mapped_column(Float, default=0.0)
    water_fee: Mapped[float] = mapped_column(Float, default=0.0)
    pv_offset_fee: Mapped[float] = mapped_column(Float, default=0.0)
    total_fee: Mapped[float] = mapped_column(Float, default=0.0)

    detail: Mapped[dict] = mapped_column(JSON, default=dict)          # 分项明细
    score: Mapped[int] = mapped_column(Integer, default=100)          # 节能评分 0-100
    advice: Mapped[list] = mapped_column(JSON, default=list)          # 节能建议

    booking: Mapped["Booking"] = relationship(back_populates="bill")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    rule: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity), default=AlertSeverity.WARNING)
    status: Mapped[AlertStatus] = mapped_column(Enum(AlertStatus), default=AlertStatus.OPEN, index=True)
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ux_open_alert", "device_id", "rule", "status"),)


class DeviceCommand(Base):
    """下行指令队列（API 写入 → 设备/模拟器拉取执行）。"""
    __tablename__ = "device_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    command: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[CommandStatus] = mapped_column(Enum(CommandStatus), default=CommandStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
