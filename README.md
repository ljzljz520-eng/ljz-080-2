# 海岛民宿能耗托管系统

面向海岛房东、管家和游客的一体化能耗托管原型：接入空调、热水器、光伏、蓄水池遥测，入住前预冷值守，自动识别耗电异常，离店后生成账单和节能建议，月底按房源/房间对比用水用电。

## 功能

- **房东接入**：房源、房间、空调、热水器、屋顶光伏、高位蓄水池建档。
- **IoT 遥测**：支持单条和批量上报，累计表读数自动求差；兼容离线事件。
- **入住看板**：24 小时到店订单自动判断 `已就绪 / 预冷中 / 未预冷 / 等待中 / 设备离线`，可下发预冷指令。
- **异常检测**：功率越限、累计电耗跳跃、电表停走、空调不制冷、热水器过热、水箱低水位/快速下降、光伏超发。
- **离店账单**：按入住窗口聚合空调、热水器、水量和光伏自发自用，计算电费、水费、光伏节省金额及分设备明细。
- **节能建议**：基于空调设定温度、人晚基准、用水量、光伏自用比例和未处理异常生成建议。
- **月度对比**：按房源和房间统计 kWh/人晚、L/人晚、费用、光伏抵扣和用电排行。
- **零第三方运行依赖**：后端只使用 Python 标准库（HTTP 服务 + SQLite）。

## 快速开始

```bash
# Python 3.11+
python -m app reset-demo     # 初始化并生成两栋海岛民宿演示数据
python -m app serve --port 8000
```

打开 <http://127.0.0.1:8000>。空数据库启动 `serve` 时也会自动生成演示数据。

也可以指定数据库：

```bash
python -m app --db ./data/demo.db reset-demo
python -m app --db ./data/demo.db serve
```

## 主要 API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/houses` | 创建房源 |
| GET | `/api/houses` | 查询房源 |
| POST | `/api/houses/{id}/rooms` | 创建房间 |
| GET | `/api/rooms?house_id={id}` | 查询房间 |
| POST | `/api/devices` | 绑定空调/热水器/光伏/蓄水设备 |
| GET | `/api/devices?house_id={id}` | 查询设备和最新遥测 |
| POST | `/api/bookings` | 创建入住订单 |
| POST | `/api/telemetry` | 单设备遥测上报 |
| POST | `/api/telemetry/batch` | 批量遥测上报 |
| GET | `/api/board/arrivals` | 入住预冷看板 |
| POST | `/api/bookings/{id}/precool` | 下发预冷指令 |
| GET | `/api/anomalies` | 查询异常 |
| POST | `/api/anomalies/{id}/ack` | 管家确认并关闭异常 |
| GET | `/api/board/tanks` | 蓄水状态 |
| POST | `/api/bookings/{id}/checkout` | 离店结算（幂等） |
| GET | `/api/bills` / `/api/bills/{id}` | 查询账单和建议 |
| GET | `/api/reports/monthly?year=2026&month=9` | 月度房源对比 |

### 遥测格式

空调：

```json
{
  "device_id": 1,
  "ts": "2026-09-30T12:00:00Z",
  "power_kw": 0.82,
  "energy_kwh": 126.4,
  "room_temperature": 25.6,
  "set_temperature": 26.0,
  "online": true
}
```

热水器支持 `power_kw/energy_kwh/water_temperature/water_flow_lpm/water_liters`；光伏支持 `solar_generation_kw/solar_generated_kwh`；水箱支持 `tank_level_percent`。

## 业务规则

- 实时功率超过额定功率 120%：严重告警。
- 区间电耗超过理论最大值 125%：电表/设备异常。
- 空调运行但室温高于设定温度 4℃：疑似不制冷。
- 水箱 `<20%` 预警，`<10%` 严重；无用水流量但液位下降过快疑似漏损。
- 预冷窗口为入住前 30 分钟；室温不高于目标 1℃、空调运行且设定温度匹配即视为就绪。
- 光伏抵扣不超过账单窗口内实际用电量，防止向游客超额返还。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 生产化建议

当前是可运行的业务原型，生产接入时建议补充：设备证书/鉴权、MQTT 或 IoT 网关、断点续传、指令 ACK/超时重试、时区和夏令时策略、价格方案与阶梯水电价、数据归档、告警通知（短信/企微）、审计日志以及容器部署。
