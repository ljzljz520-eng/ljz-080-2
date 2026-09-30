"""全局配置：可通过环境变量覆盖。"""
import os
from zoneinfo import ZoneInfo

# 数据库
DATABASE_URL = os.getenv(
    "ENERGY_DATABASE_URL",
    "sqlite:///" + os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "island_energy.db"),
)

# 时区（海岛民宿默认东八区）
TIMEZONE = ZoneInfo(os.getenv("ENERGY_TIMEZONE", "Asia/Shanghai"))

# 后台模拟器是否随 API 进程自动启动
SIMULATOR_AUTOSTART = os.getenv("ENERGY_SIMULATOR_AUTOSTART", "0") == "1"
SIMULATOR_INTERVAL_S = int(os.getenv("ENERGY_SIMULATOR_INTERVAL_S", "30"))

# 业务默认参数
DEFAULT_PRECOOL_LEAD_MIN = int(os.getenv("ENERGY_PRECOOL_LEAD_MIN", "60"))
DEFAULT_PRECOOL_TARGET_C = float(os.getenv("ENERGY_PRECOOL_TARGET_C", "24"))
DEFAULT_WATER_READY_C = float(os.getenv("ENERGY_WATER_READY_C", "45"))
DEFAULT_WATER_LOW_PCT = float(os.getenv("ENERGY_WATER_LOW_PCT", "20"))
DEFAULT_ELEC_PRICE = float(os.getenv("ENERGY_ELEC_PRICE", "1.2"))   # 元/kWh
DEFAULT_WATER_PRICE = float(os.getenv("ENERGY_WATER_PRICE", "4.0"))  # 元/吨
PV_FEEDIN_PRICE = float(os.getenv("ENERGY_PV_FEEDIN_PRICE", "0.42"))  # 余电上网价
