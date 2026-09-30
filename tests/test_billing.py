import unittest
from datetime import datetime, timezone

from app.services.billing import build_bill_totals, sum_positive_delta
from app.storage import Database
from app.services import telemetry
from app.timeutil import iso


class BillingRulesTest(unittest.TestCase):
    def test_positive_cumulative_deltas_ignore_rollback(self):
        rows = [
            {"energy_kwh": 10},
            {"energy_kwh": 12.5},
            {"energy_kwh": 9},
            {"energy_kwh": 11},
        ]
        self.assertEqual(sum_positive_delta(rows, "energy_kwh"), 4.5)

    def test_pv_credit_cannot_exceed_usage_and_recommendations_include_low_setpoint(self):
        result = build_bill_totals(
            electricity_kwh=12, water_liters=1000, pv_self_consumed_kwh=20,
            electricity_price=0.7, water_price=5, guest_count=2,
            stay_hours=48, ac_avg_setpoint=23,
        )
        self.assertEqual(result["grid_electricity_kwh"], 0)
        self.assertEqual(result["electricity_fee"], 0)
        self.assertEqual(result["water_fee"], 5)
        self.assertEqual(result["total_fee"], 5)
        self.assertTrue(any(r["code"] == "ac_setpoint_26" for r in result["recommendations"]))


class TelemetryAnomalyTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init_db()
        house = telemetry.create_house(self.db, {"name": "测试院", "electricity_price": 0.7, "water_price": 5})
        room = telemetry.create_room(self.db, house, {"name": "海景区"})
        self.ac = telemetry.create_device(self.db, {"house_id": house, "room_id": room, "name": "空调", "type": "air_conditioner", "rated_power_kw": 1.0})
        self.tank = telemetry.create_device(self.db, {"house_id": house, "name": "水箱", "type": "water_tank", "capacity_liters": 1000})
        self.house_id = house

    def tearDown(self):
        self.db.close()

    def test_over_power_and_ac_not_cooling_create_critical_or_warning(self):
        telemetry.record_reading(self.db, self.ac, {
            "ts": "2026-09-30T12:00:00Z", "power_kw": 1.4,
            "energy_kwh": 1, "room_temperature": 31, "set_temperature": 24,
        })
        anomalies = telemetry.list_anomalies(self.db)
        rules = {a["rule"]: a for a in anomalies}
        self.assertIn("over_power", rules)
        self.assertEqual(rules["over_power"]["severity"], "critical")
        self.assertIn("ac_not_cooling", rules)

    def test_tank_low_anomaly(self):
        telemetry.record_reading(self.db, self.tank, {"ts": "2026-09-30T12:00:00Z", "tank_level_percent": 15})
        anomalies = telemetry.list_anomalies(self.db, house_id=self.house_id)
        self.assertEqual([a["rule"] for a in anomalies], ["tank_low"])
        self.assertEqual(anomalies[0]["severity"], "warning")

    def test_acknowledge_closes_anomaly(self):
        telemetry.record_reading(self.db, self.tank, {"ts": "2026-09-30T12:00:00Z", "tank_level_percent": 5})
        anomaly = telemetry.list_anomalies(self.db)[0]
        telemetry.acknowledge_anomaly(self.db, anomaly["id"])
        self.assertEqual(telemetry.list_anomalies(self.db), [])


if __name__ == "__main__":
    unittest.main()
