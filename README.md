# 🌊 海岛民宿能耗托管系统

面向海岛民宿房东 / 管家 / 游客三方的能耗托管后台：把每栋房子的**空调、热水器、光伏、蓄水池**接进来，
管家在入住前看到「哪间房还没预冷、哪台设备耗电异常」，游客离店后自动生成**能耗账单与节能建议**，
月底按房源**横向对比用水用电**（含故障/暗漏等房东侧损耗）。

## 功能一览

| 角色 | 能力 |
|---|---|
| 🧑‍💼 管家 | 今日入住预冷看板（pending / cooling / done / missed）、一键预冷、设备遥控、异常告警确认/解除、蓄水液位监控 |
| 🏠 房东 | 房源/房间/设备台账、设备实时状态、异常耗电与暗漏识别、月度房源对比与 CSV 导出 |
| 🧳 游客 | 离店能耗账单（电/水/光伏抵扣分项）、节能评分(0-100)、个性化节能建议 |
| 🔌 设备 | 遥测单条/批量上报、下行指令队列（开机/关机/设定温度/预冷） |

### 设备指标约定

| 设备 | 指标 |
|---|---|
| 空调 ac | `power_kw`(平均) `power_peak_kw`(峰值) `room_temp` `target_temp` `mode`(0关1制冷) `energy_kwh`(累计总表) |
| 热水器 heater | `power_kw` `power_peak_kw` `water_temp` `target_temp` `energy_kwh` |
| 光伏 pv | `power_kw` `gen_kwh`(累计发电总表) |
| 蓄水池 tank | `level_pct` `inflow_lh` `outflow_lh` `outflow_peak_lh` `out_l`(累计出水总表，房源级水表) |

### 内置异常规则（规则引擎每拍自动评估，恢复后自动解除）

- `ac_overpower` 空调峰值功率超额定 50%（critical）
- `ac_vacant_running` 空房（非预冷窗口）空调持续运行
- `ac_low_target` 制冷设定 ≤20℃
- `heater_overpower / heater_vacant_running / heater_max_temp` 热水器卡滞/空房加热/高温运行
- `pv_zero_daytime` 白天近 1.5h 平均出力 < 装机 5%
- `tank_low` 蓄水液位 <20%（<10% critical）
- `tank_idle_outflow` 无在住仍持续出水（暗漏/未关阀）

### 计费口径

- **用电** = 房间空调+热水器「累计电度表」离店-入住读数差；带故障画像的设备能耗由房东承担，不计客人账单。
- **用水** = 房源蓄水池「累计出水表」按各订单**在住时刻的并发房间数**时间积分分摊；
  检测到蓄水池漏损画像时，按住客用水模型估算，漏量不由客人承担。
- **光伏抵扣** = 在订单在住的每个小时内，该小时光伏发电量按「本单当前用电 / 同店在住订单总用电」分配，封顶不超过本单用电。
- 月报区分**计入账单的客人用量**、**抄表总量**与**异常/漏损量**三列。

## 快速开始

```bash
pip install -r requirements.txt

# 1) 初始化演示数据（3 个海岛房源 / 6 间房 / 15 台设备含故障 / 7 个订单 / 整月小时级回放）
python3 -m app.seed

# 2) 启动 API + 看板（可选：随进程启动实时模拟器）
ENERGY_SIMULATOR_AUTOSTART=1 python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
#   看板：http://127.0.0.1:8000    API 文档：http://127.0.0.1:8000/docs
#   或：./run.sh

# 3)（可选）独立进程跑实时设备模拟器
python3 -m app.simulator            # 默认 60s 一拍，ENERGY_SIMULATOR_INTERVAL_S 可调

# 4) 重置数据 / 测试
make reset      # 删库并重新播种
pytest tests/ -q
```

## 常用 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/properties`、`/api/properties/{id}/rooms`、`/api/properties/{id}/devices` | 台账建档 |
| GET | `/api/properties/{id}` | 房源拓扑+所有设备最新状态 |
| POST | `/api/telemetry/readings`、`/api/telemetry/readings/batch` | 设备遥测上报 |
| GET | `/api/telemetry/devices/{id}/series?metric=&start=&end=` | 历史曲线 |
| GET | `/api/butler/dashboard` | 预冷看板+告警+蓄水状态 |
| POST | `/api/bookings/{id}/precool?target_temp=24` | 一键预冷（向房间全部空调下发指令） |
| POST | `/api/devices/{id}/command?command=turn_off` | 设备遥控（30 分钟人工接管） |
| GET/POST | `/api/alerts`、`/api/alerts/{id}/ack`、`/resolve`、`POST /api/alerts/evaluate` | 告警管理/手动重评 |
| POST | `/api/bookings/{id}/checkout` | 离店出账 |
| GET | `/api/bills`、`/api/bookings/{id}/bill` | 账单查询 |
| GET | `/api/reports/monthly?month=2026-09`、`/api/reports/monthly.csv` | 月度对比 JSON/CSV |

### 设备上报示例

```bash
curl -X POST http://localhost:8000/api/telemetry/readings \
  -H 'Content-Type: application/json' \
  -d '{"device_code":"AC-ISL01-101","metric":"room_temp","value":27.5}'
```

## 演示数据剧本（种子脚本）

- **今日 14:00 入住**「海景亲子房」→ 管家看板显示 *待预冷*，点「一键预冷」可看到室温 29℃→24℃ 的变化（配合模拟器实时节拍）。
- `AC-ISL01-FAULT` 备用空调：常开 + 2 倍额定功率 → 空房运行 + 超功率双告警。
- `WH-ISL02-FAULT` 热水器卡滞：空房高温加热三连告警。
- `AC-ISL03-FAULT`：常开且设定 19.5℃ → 空房运行 + 设定过低。
- `TK-ISL03` 漏损蓄水池：液位低 critical + 无在住持续出水 warning，月报中体现 100+ 吨漏损水（不摊给客人）。
- `PV-ISL02` 光伏检修：月报中该店光伏自用率仅约 20%，可与另外两店对比。
- 6 个已离店订单覆盖 eco / normal / wasteful 三类住客画像：账单评分从 40 到 88，节能建议各不相同。

## 架构

```
app/
├── main.py              # FastAPI 入口（API + 静态看板）
├── config.py            # 环境变量配置（DB/时区/价格/预冷参数）
├── database.py models.py schemas.py
├── routers/             # properties / telemetry / bookings(管家) / alerts / billing / reports
├── services/
│   ├── ingest.py        # 遥测接入校验
│   ├── timeseries.py    # 读数入库/状态快照 upsert/累计表区间差值
│   ├── alerts.py        # 规则引擎（告警生命周期管理）
│   ├── butler.py        # 预冷看板 + 下行指令
│   ├── billing.py       # 读表差额/水分摊/光伏抵扣/评分建议
│   └── reports.py       # 月度对比 + CSV
├── simulator.py         # 设备物理模型（15min 子步）+ 指令执行 + 历史回放 + 实时循环
├── seed.py              # 演示数据一键初始化
└── static/index.html    # 管家看板（原生 JS 单页，无构建依赖）
tests/                   # 12 个端到端/服务级测试
```

- 存储：SQLite + WAL（零依赖，可平滑换 PostgreSQL，只需改 `ENERGY_DATABASE_URL`）。
- 时间：内部统一 UTC naive 存储，业务按 `ENERGY_TIMEZONE`（默认 Asia/Shanghai）计算“今天/白天/预冷窗口”。
- 累计量（电度/发电量/出水量）为**单调总表**，所有账单与月报均由区间差值计算，确定性、可审计。
