"""设备模拟器：空调/热水器/光伏/蓄水池的简化物理模型。

- 既能为历史区间按小时回放造数据（种子脚本），也能实时按节拍推进（API 进程内线程/独立进程）。
- 读取 device_commands 队列执行下行指令；fault_profile 支持逗号叠加故障画像：
  always_on / overpower_fault / low_target / heater_stuck / pv_fault / tank_leak。
"""
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import TIMEZONE, DEFAULT_PRECOOL_TARGET_C, DEFAULT_PRECOOL_LEAD_MIN
from .database import SessionLocal
from .models import Device, DeviceType, Booking, Room, DeviceCommand, CommandStatus
from .services import timeseries as ts
from .services.alerts import evaluate_all

METRICS = {
    DeviceType.AC: ["power_kw", "room_temp", "target_temp", "mode", "energy_kwh"],
    DeviceType.HEATER: ["power_kw", "water_temp", "target_temp", "energy_kwh"],
    DeviceType.PV: ["power_kw", "gen_kwh"],
    DeviceType.TANK: ["level_pct", "inflow_lh", "outflow_lh", "out_l"],
}


def local_hour(utc_dt: datetime) -> int:
    from datetime import timezone
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(TIMEZONE).hour


def ambient_temp(utc_dt: datetime) -> float:
    """海岛气温：凌晨约 26.5℃，午后约 33℃。"""
    h = local_hour(utc_dt) + utc_dt.minute / 60.0
    return 29.5 + 3.5 * math.sin((h - 9) * 2 * math.pi / 24)


def solar_factor(utc_dt: datetime) -> float:
    h = local_hour(utc_dt) + utc_dt.minute / 60.0
    if h < 6 or h > 20:
        return 0.0
    return max(0.0, math.sin((h - 6) / 14 * math.pi))


# 各住客画像：空调目标温、外出比例、热水目标、用水倍率
GUEST_PROFILES = {
    "eco":      dict(ac_target=26.0, away=0.55, heater_target=43.0, water_mult=0.7),
    "normal":   dict(ac_target=24.5, away=0.25, heater_target=46.0, water_mult=1.0),
    "wasteful": dict(ac_target=20.0, away=0.05, heater_target=50.0, water_mult=1.6),
}

# 时段每房间出水基线 L/h
def _draw_base(hour: int) -> float:
    if 7 <= hour < 9:
        return 40.0
    if 19 <= hour < 23:
        return 45.0
    if 23 <= hour or hour < 6:
        return 2.0
    return 7.0


@dataclass
class DevState:
    type: DeviceType
    room_id: int | None
    rated: float
    profiles: set[str]
    # 运行态
    room_temp: float = 29.0
    water_temp: float = 28.0
    target: float = 24.0
    compressor_on: bool = False
    heating_on: bool = False
    energy_kwh: float = 0.0
    gen_kwh: float = 0.0
    volume_l: float = 1500.0
    total_out_l: float = 0.0
    last_emit: dict = field(default_factory=dict)
    override_until: datetime | None = None
    override_action: str | None = None  # on / off
    override_target: float | None = None


class DeviceSimulator:
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self._load()

    def _load(self):
        db = SessionLocal()
        try:
            devs = list(db.scalars(select(Device)))
            self.devices: dict[int, Device] = {d.id: d for d in devs}
            self.states: dict[int, DevState] = {}
            for d in devs:
                st = DevState(
                    type=d.type, room_id=d.room_id, rated=d.rated_power_kw,
                    profiles=set((d.fault_profile or "").split(",")) - {""},
                )
                # 实时模式从数据库最新快照续算总表，保证账单连续
                for metric in ("energy_kwh", "gen_kwh"):
                    v = ts.state_value(db, d.id, metric)
                    if v is not None:
                        if metric == "energy_kwh":
                            st.energy_kwh = v
                        else:
                            st.gen_kwh = v
                v = ts.state_value(db, d.id, "out_l")
                if v is not None:
                    st.total_out_l = v
                v = ts.state_value(db, d.id, "level_pct")
                prop = d.property
                if d.type == DeviceType.TANK:
                    cap = prop.tank_capacity_l if prop else 2000.0
                    st.volume_l = cap * (v / 100.0) if v is not None else cap * 0.75
                v = ts.state_value(db, d.id, "room_temp")
                if v is not None:
                    st.room_temp = v
                v = ts.state_value(db, d.id, "water_temp")
                if v is not None:
                    st.water_temp = v
                v = ts.state_value(db, d.id, "target_temp")
                if v is not None:
                    st.target = v
                self.states[d.id] = st
        finally:
            db.close()

    # ---------------- 行为决策 ----------------
    def _active_booking(self, db, room_id: int, at: datetime):
        for b in db.scalars(select(Booking).where(
                Booking.room_id == room_id, Booking.check_in <= at, Booking.check_out > at)):
            return b
        return None

    def _guest_mode(self, db, room_id, at, guest_modes):
        b = self._active_booking(db, room_id, at)
        if not b:
            return None, None
        mode = guest_modes.get(b.id, "normal")
        return GUEST_PROFILES[mode], b

    def _close_expired(self, db, at: datetime):
        """已投递且超过接管时长的指令标记 DONE。"""
        delivered = list(db.scalars(
            select(DeviceCommand).where(DeviceCommand.status == CommandStatus.DELIVERED)
        ))
        for cmd in delivered:
            ttl = 30 if cmd.command != "precool" else \
                int(cmd.params.get("lead_min", DEFAULT_PRECOOL_LEAD_MIN)) + 240
            if cmd.delivered_at and at > cmd.delivered_at + timedelta(minutes=ttl):
                cmd.status = CommandStatus.DONE
                cmd.result = (cmd.result or "") + "；接管已结束"

    def _apply_commands(self, db, at: datetime):
        self._close_expired(db, at)
        cmds = list(db.scalars(
            select(DeviceCommand)
            .where(DeviceCommand.status.in_([CommandStatus.PENDING, CommandStatus.DELIVERED]))
            .order_by(DeviceCommand.id.asc())
        ))
        for cmd in cmds:
            st = self.states.get(cmd.device_id)
            if st is None:
                cmd.status = CommandStatus.FAILED
                cmd.result = "设备不在线"
                continue
            # 已被本实例接管且未过期 → 跳过
            if cmd.status == CommandStatus.DELIVERED and st.override_until and at <= st.override_until:
                continue
            ttl = 30
            if cmd.command == "turn_on":
                st.override_action, ttl = "on", 30
            elif cmd.command == "turn_off":
                st.override_action, ttl = "off", 30
            elif cmd.command == "set_target":
                st.override_action, ttl = "on", 30
                st.override_target = float(cmd.params.get("target_temp", 24))
            elif cmd.command == "precool":
                st.override_action = "on"
                # 覆盖从管家点击到入住后一段时间；自动预冷窗口由物理层 precool_rooms 兜底
                ttl = int(cmd.params.get("lead_min", DEFAULT_PRECOOL_LEAD_MIN)) + 240
                st.override_target = float(cmd.params.get("target_temp", DEFAULT_PRECOOL_TARGET_C))
            st.override_until = at + timedelta(minutes=ttl)
            cmd.status = CommandStatus.DELIVERED
            cmd.delivered_at = at
            cmd.result = f"已执行，人工接管 {ttl} 分钟"

    def _override(self, st: DevState, at: datetime):
        if st.override_until and at <= st.override_until:
            if st.override_target is not None:
                st.target = st.override_target
            return st.override_action
        if st.override_until and at > st.override_until:
            st.override_until = st.override_action = st.override_target = None
        return None

    # ---------------- 单步物理 ----------------
    def _step_device(self, db, d: Device, st: DevState, at: datetime, dt_h: float,
                     amb: float, guest_modes: dict, precool_rooms: set[int]):
        prof = GUEST_PROFILES["normal"]
        gmode, booking = (None, None)
        if st.room_id:
            gmode, booking = self._guest_mode(db, st.room_id, at, guest_modes)
            if gmode:
                prof = gmode
        hour = local_hour(at)

        if st.type == DeviceType.AC:
            self._step_ac(st, at, dt_h, amb, prof, gmode, hour, booking, precool_rooms)
        elif st.type == DeviceType.HEATER:
            self._step_heater(st, at, dt_h, amb, prof, gmode, hour, booking)
        elif st.type == DeviceType.PV:
            fault = "pv_fault" in st.profiles
            power = st.rated * solar_factor(at) * 0.82 * (0.04 if fault else 1.0)
            st.gen_kwh += power * dt_h
            st.last_emit = {"power_kw": round(power, 3), "gen_kwh": round(st.gen_kwh, 3)}
        elif st.type == DeviceType.TANK:
            self._step_tank(db, d, st, at, dt_h, guest_modes)

    def _step_ac(self, st, at, dt_h, amb, prof, gmode, hour, booking, precool_rooms):
        want_on, target = False, prof["ac_target"] if gmode else 24.5
        if "always_on" in st.profiles:
            want_on, target = True, 19.5 if "low_target" in st.profiles else 22.0
        elif gmode:
            away = self.rng.random() < prof["away"] and 10 <= hour < 18
            if away:
                want_on = False
            elif 23 <= hour or hour < 7:
                want_on = True
                target = 27.0 if prof["ac_target"] >= 26 else (25.0 if prof["ac_target"] >= 24 else 21.0)
            else:
                want_on = self.rng.random() < 0.8
                target = prof["ac_target"]
        if st.room_id in precool_rooms:
            want_on, target = True, DEFAULT_PRECOOL_TARGET_C

        ov = self._override(st, at)
        if ov == "on":
            want_on = True
            target = st.override_target or target
        elif ov == "off":
            want_on = False
        if want_on and not getattr(st, "_last_want", False):
            st.compressor_on = st.room_temp > target  # 新开机周期重置温控
        st._last_want = want_on
        st.target = target

        C_ROOM = 1.1   # kWh/℃ 等效热容
        UA = 0.40      # kW/℃ 漏热（海岛保温一般）
        power = 0.0
        if want_on:
            # 压缩机迟滞：高于 target+0.8 启动，低于 target-0.3 停机；死区内保持
            if st.room_temp > target + 0.8:
                st.compressor_on = True
            elif st.room_temp < target - 0.3:
                st.compressor_on = False
            elif not st.compressor_on and st.room_temp > target:
                st.compressor_on = True
            if st.compressor_on:
                rated = st.rated * (2.0 if "overpower_fault" in st.profiles else 1.0)
                power = rated
        else:
            st.compressor_on = False
        # 温度演化
        gain = UA * (amb - st.room_temp) * dt_h / C_ROOM
        cool = (power * 3.0 * dt_h) / C_ROOM  # COP=3
        st.room_temp += gain - cool
        st.room_temp = max(16.0, min(45.0, st.room_temp))
        st.energy_kwh += power * dt_h
        st.last_emit = {
            "power_kw": round(power, 3),
            "room_temp": round(st.room_temp, 2),
            "target_temp": round(st.target, 1),
            "mode": 1 if power > 0.05 else 0,
            "energy_kwh": round(st.energy_kwh, 3),
        }

    def _step_heater(self, st, at, dt_h, amb, prof, gmode, hour, booking):
        target = prof["heater_target"] if gmode else 45.0
        allow_window = False
        if "heater_stuck" in st.profiles:  # 卡滞：全时段加热
            allow_window, target = True, 52.0
        elif gmode:
            eco = prof["heater_target"] <= 43
            allow_window = (18 <= hour < 23) if eco else ((7 <= hour < 9) or (18 <= hour < 24))
            # 用热水：温度被拉低
            draw = _draw_base(hour) * prof["water_mult"] * dt_h
            st.water_temp -= (draw / 50.0) * (st.water_temp - amb) * 0.5
        ov = self._override(st, at=at)
        st.target = target
        # 散热
        st.water_temp -= (st.water_temp - amb) * dt_h / 9.0
        power = 0.0
        if allow_window or ov == "on":
            # 温控器迟滞
            if st.water_temp < target - 2:
                st.heating_on = True
            elif st.water_temp >= target:
                st.heating_on = False
            if st.heating_on:
                power = st.rated * (2.0 if "heater_stuck" in st.profiles else 1.0)
        else:
            st.heating_on = False
        st.water_temp += (power * dt_h) / 0.058  # 50L 等效热容
        st.water_temp = min(60.0, st.water_temp)
        st.energy_kwh += power * dt_h
        st.last_emit = {
            "power_kw": round(power, 3),
            "water_temp": round(st.water_temp, 2),
            "target_temp": round(st.target, 1),
            "energy_kwh": round(st.energy_kwh, 3),
        }

    def _step_tank(self, db, d, st, at, dt_h, guest_modes):
        cap = db.get(Device, d.id).property.tank_capacity_l
        leak = "tank_leak" in st.profiles
        # 统计当前在住房间与用水倍率
        rooms = list(db.scalars(select(Room.id).where(Room.property_id == d.property_id)))
        outflow = 0.0
        for rid in rooms:
            gmode, _ = self._guest_mode(db, rid, at, guest_modes)
            if gmode:
                outflow += _draw_base(local_hour(at)) * gmode["water_mult"]
        if leak:
            # 暗漏：持续小流量（哪怕液位见底），无在住时稳定触发告警
            outflow += 280.0
        # 补水阀：液位低于 25% 自动补水；漏损设备补水泵长期失灵（形成低液位告警）
        inflow = 0.0
        pct = st.volume_l / cap * 100
        pump_ok = (not leak) or self.rng.random() > 0.9
        if (not leak and pct < 30) or (leak and pct < 10 and pump_ok):
            inflow = 2000.0
        elif pct >= 90:
            inflow = 0.0
        # 正常出水受存量限制；暗漏可把液位压至 5% 底位但流量持续
        capacity_out = max(0.0, st.volume_l - cap * 0.05) / dt_h if dt_h else outflow
        guest_out = outflow - (280.0 if leak else 0.0)
        actual_out = (min(guest_out, capacity_out) + (280.0 if leak else 0.0))
        st.volume_l = min(cap, max(cap * 0.05, st.volume_l + (inflow - actual_out) * dt_h))
        st.total_out_l += actual_out * dt_h
        st.last_emit = {
            "level_pct": round(st.volume_l / cap * 100, 1),
            "inflow_lh": round(inflow, 1),
            "outflow_lh": round(actual_out, 1),
            "out_l": round(st.total_out_l, 2),
        }

    # ---------------- 对外入口 ----------------
    def step(self, at: datetime, dt_h: float, db, guest_modes: dict | None = None,
             commit: bool = False, evaluate: bool = False,
             emit: bool = True, sub_minutes: int = 15) -> dict[int, dict]:
        """
        推进一个时间片。内部按 sub_minutes 子步计算（保证温控物理稳定），
        在 at 时刻发射一次读数（功率/流量为子步均值，累计量为末值）。
        """
        guest_modes = guest_modes or {}
        self._apply_commands(db, at)
        # 预冷：入住前 lead 内、已请求预冷的房间
        lead = timedelta(minutes=DEFAULT_PRECOOL_LEAD_MIN)
        precool_rooms: set[int] = set()
        soon = db.scalars(select(Booking).where(
            Booking.check_in > at, Booking.check_in <= at + timedelta(hours=dt_h) + lead,
            Booking.precool_requested.is_(True),
        ))
        for b in soon:
            precool_rooms.add(b.room_id)

        n_sub = max(1, round(dt_h * 60 / sub_minutes))
        sub_h = dt_h / n_sub
        sums: dict[int, dict[str, float]] = {did: {} for did in self.devices}
        peaks: dict[int, dict[str, float]] = {did: {} for did in self.devices}

        for i in range(n_sub):
            sub_at = at - timedelta(hours=dt_h) + timedelta(hours=sub_h * (i + 1))
            amb = ambient_temp(sub_at)
            for d in self.devices.values():
                if not d.active:
                    continue
                st = self.states[d.id]
                self._step_device(db, d, st, sub_at, sub_h, amb, guest_modes, precool_rooms)
                for m in ("power_kw", "inflow_lh", "outflow_lh"):
                    if m in st.last_emit:
                        sums[d.id][m] = sums[d.id].get(m, 0.0) + st.last_emit[m]
                        peaks[d.id][m] = max(peaks[d.id].get(m, 0.0), st.last_emit[m])

        emitted: dict[int, dict] = {}
        peak_metric = {"power_kw": "power_peak_kw", "outflow_lh": "outflow_peak_lh"}
        for d in self.devices.values():
            if not d.active:
                continue
            st = self.states[d.id]
            snap = dict(st.last_emit)
            for m, v in sums[d.id].items():
                snap[m] = round(v / n_sub, 3)
                if m in peak_metric:
                    snap[peak_metric[m]] = round(peaks[d.id].get(m, 0.0), 3)
            emitted[d.id] = snap
            if emit:
                for metric, value in snap.items():
                    ts.insert_reading(db, d.id, at, metric, value)
        if evaluate:
            evaluate_all(db, at)
        if commit:
            db.commit()
        return emitted

    def backfill(self, start: datetime, end: datetime, guest_modes: dict | None = None,
                 step_minutes: int = 60, progress: bool = False):
        """历史回放：批量写入读数，结束后刷状态、跑一轮规则评估。"""
        guest_modes = guest_modes or {}
        dt_h = step_minutes / 60.0
        db = SessionLocal()
        batch = 0
        try:
            t = start
            n = 0
            while t < end:
                self.step(t, dt_h, db, guest_modes=guest_modes, commit=False)
                t += timedelta(minutes=step_minutes)
                n += 1
                if n % 24 == 0:
                    db.commit()
                    if progress:
                        print(f"  backfill -> {t.astimezone(TIMEZONE):%Y-%m-%d %H:%M}", flush=True)
            db.commit()
            evaluate_all(db, end)
            db.commit()
        finally:
            db.close()

    # ---------------- 实时循环 ----------------
    def run_forever(self, interval_s: float = 60.0, guest_modes: dict | None = None,
                    eval_every: int = 4):
        ticks = 0
        while True:
            t0 = time.time()
            db = SessionLocal()
            try:
                self.step(datetime.utcnow(), interval_s / 3600.0, db,
                          guest_modes=guest_modes or {}, commit=False,
                          evaluate=(ticks % eval_every == 0))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            ticks += 1
            dt = time.time() - t0
            time.sleep(max(0.0, interval_s - dt))


_thread: threading.Thread | None = None


def start_background(interval_s: float = 60.0) -> bool:
    global _thread
    if _thread and _thread.is_alive():
        return False
    sim = DeviceSimulator()
    _thread = threading.Thread(target=sim.run_forever, args=(interval_s,), daemon=True)
    _thread.start()
    return True


if __name__ == "__main__":
    import os
    iv = float(os.getenv("ENERGY_SIMULATOR_INTERVAL_S", "60"))
    print(f"模拟器启动，节拍 {iv}s")
    DeviceSimulator().run_forever(iv)
