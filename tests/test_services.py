import unittest
from datetime import timedelta

from app.services import board, checkout as checkout_service, monthly, telemetry
from app.storage import Database
from app.timeutil import iso, utc_now


class ServiceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init_db()
        self.now = utc_now().replace(second=0, microsecond=0)
        self.house = telemetry.create_house(self.db, {
            "name": "工作流院", "electricity_price": 0.7, "water_price": 5
        })
        self.room = telemetry.create_room(self.db, self.house, {"name": "房1", "target_temperature": 26})
        self.ac = telemetry.create_device(self.db, {"house_id": self.house, "room_id": self.room, "name": "空调", "type": "air_conditioner", "rated_power_kw": 1.2})
        self.heater = telemetry.create_device(self.db, {"house_id": self.house, "room_id": self.room, "name": "热水器", "type": "water_heater", "rated_power_kw": 2})
        self.pv = telemetry.create_device(self.db, {"house_id": self.house, "name": "光伏", "type": "solar_pv", "pv_capacity_kwp": 5})
        self.tank = telemetry.create_device(self.db, {"house_id": self.house, "name": "水箱", "type": "water_tank", "capacity_liters": 10000})
        check_in = self.now + timedelta(minutes=20)
        check_out = self.now + timedelta(days=2)
        self.booking = telemetry.create_booking(self.db, {
            "room_id": self.room, "guest_name": "测试游客", "guest_count": 2,
            "check_in_at": check_in, "check_out_at": check_out,
        })

    def tearDown(self):
        self.db.close()

    def test_arrival_not_ready_then_command_then_becomes_ready(self):
        item = board.arrival_board(self.db, now=self.now)[0]
        self.assertEqual(item["precool_status"], "not_ready")
        result = board.precool_booking(self.db, self.booking)
        self.assertEqual(len(result["commands"]), 1)
        telemetry.record_reading(self.db, self.ac, {
            "ts": iso(self.now), "power_kw": 0.8, "energy_kwh": 1.0,
            "room_temperature": 26.2, "set_temperature": 26.0,
        })
        item = board.arrival_board(self.db, now=self.now)[0]
        self.assertEqual(item["precool_status"], "ready")

    def test_checkout_bill_aggregates_energy_water_and_pv(self):
        start = self.now + timedelta(minutes=20)
        end = self.now + timedelta(days=2)
        mid = start + timedelta(hours=1)
        # AC 2 kWh
        telemetry.record_reading(self.db, self.ac, {"ts": iso(start), "power_kw": 0, "energy_kwh": 0})
        telemetry.record_reading(self.db, self.ac, {"ts": iso(mid), "power_kw": 0, "energy_kwh": 2})
        # Heater 3 kWh, 1000 L water
        telemetry.record_reading(self.db, self.heater, {"ts": iso(start), "power_kw": 0, "energy_kwh": 0, "water_liters": 0, "water_temperature": 50})
        telemetry.record_reading(self.db, self.heater, {"ts": iso(mid), "power_kw": 0, "energy_kwh": 3, "water_liters": 1000, "water_temperature": 50})
        # PV 4 kWh in stay window, only 5 total consumed so credit is 4
        telemetry.record_reading(self.db, self.pv, {"ts": iso(start), "solar_generation_kw": 0, "solar_generated_kwh": 0})
        telemetry.record_reading(self.db, self.pv, {"ts": iso(mid), "solar_generation_kw": 0, "solar_generated_kwh": 4})
        # Extend checkout to end because original booking end has no reading before exact boundary.
        bill = checkout_service.checkout(self.db, self.booking, iso(end + timedelta(minutes=5)))
        self.assertEqual(bill["electricity_kwh"], 5)
        self.assertEqual(bill["water_liters"], 1000)
        self.assertEqual(bill["pv_self_consumed_kwh"], 4)
        self.assertEqual(bill["electricity_fee"], 0.70)  # (5 - 4) * .7
        self.assertEqual(bill["water_fee"], 5)
        self.assertEqual(bill["total_fee"], 5.7)
        self.assertEqual(len(bill["details"]), 3)
        self.assertTrue(bill["recommendations"])
        # Idempotent settlement.
        self.assertEqual(checkout_service.checkout(self.db, self.booking)["id"], bill["id"])

    def test_monthly_report_groups_by_house_and_room(self):
        start = self.now - timedelta(hours=3)
        end = self.now - timedelta(hours=1)
        booking = telemetry.create_booking(self.db, {
            "room_id": self.room, "guest_name": "离店游客", "guest_count": 2,
            "check_in_at": start, "check_out_at": end, "status": "checked_out",
        })
        telemetry.record_reading(self.db, self.ac, {"ts": iso(start), "power_kw": 0, "energy_kwh": 0})
        telemetry.record_reading(self.db, self.ac, {"ts": iso(end), "power_kw": 0, "energy_kwh": 8})
        telemetry.record_reading(self.db, self.heater, {"ts": iso(start), "energy_kwh": 0, "water_liters": 0, "water_temperature": 50})
        telemetry.record_reading(self.db, self.heater, {"ts": iso(end), "energy_kwh": 2, "water_liters": 500, "water_temperature": 50})
        checkout_service.checkout(self.db, booking)
        report = monthly.monthly_report(self.db, self.now.year, self.now.month)
        self.assertEqual(report["totals"]["bills"], 1)
        self.assertEqual(report["houses"][0]["electricity_kwh"], 10)
        self.assertEqual(report["rooms"][0]["water_liters"], 500)
        self.assertIn("工作流院", report["rank_by_electricity"])


if __name__ == "__main__":
    unittest.main()
