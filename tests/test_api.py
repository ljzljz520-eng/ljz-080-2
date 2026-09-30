import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from app.api import AppState, create_handler
from app.demo import seed_demo
from app.storage import Database


class ApiIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database(":memory:")
        cls.db.init_db()
        cls.ids = seed_demo(cls.db)
        handler = create_handler(AppState(cls.db))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.db.close()

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_and_static_dashboard(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            html = response.read().decode()
        self.assertIn("海岛民宿能耗托管系统", html)

    def test_demo_board_has_arrivals_anomalies_tanks_and_bill(self):
        status, arrivals = self.request("GET", "/api/board/arrivals?hours=24")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(arrivals), 4)
        statuses = {a["precool_status"] for a in arrivals}
        self.assertIn("ready", statuses)
        self.assertIn("not_ready", statuses)
        self.assertIn("precooling", statuses)

        status, anomalies = self.request("GET", "/api/anomalies?active=1")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["rule"] == "tank_low" for a in anomalies))

        status, tanks = self.request("GET", "/api/board/tanks")
        self.assertEqual(status, 200)
        self.assertIn("critical", {t["state"] for t in tanks})

        status, bills = self.request("GET", "/api/bills")
        self.assertEqual(status, 200)
        self.assertTrue(bills)
        self.assertTrue(bills[0]["recommendations"])

        bill_id = bills[0]["id"]
        status, bill = self.request("GET", f"/api/bills/{bill_id}")
        self.assertEqual(status, 200)
        self.assertGreater(bill["total_fee"], 0)

    def test_onboarding_telemetry_precool_and_validation(self):
        status, bad = self.request("POST", "/api/houses", {"name": ""})
        self.assertEqual(status, 400)
        self.assertIn("名称", bad["error"])

        status, house = self.request("POST", "/api/houses", {"name": "API测试院", "electricity_price": 0.8})
        self.assertEqual(status, 201)
        house_id = house["id"]
        status, room = self.request("POST", f"/api/houses/{house_id}/rooms", {"name": "新房间"})
        self.assertEqual(status, 201)
        room_id = room["id"]
        status, rooms = self.request("GET", f"/api/rooms?house_id={house_id}")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in rooms], ["新房间"])
        status, device = self.request("POST", "/api/devices", {
            "house_id": house_id, "room_id": room_id, "name": "新空调",
            "type": "air_conditioner", "rated_power_kw": 1.1,
        })
        self.assertEqual(status, 201)
        device_id = device["id"]
        status, reading = self.request("POST", "/api/telemetry", {
            "device_id": device_id, "power_kw": 1.5, "energy_kwh": 1,
            "room_temperature": 30, "set_temperature": 24,
        })
        self.assertEqual(status, 201)
        status, anomalies = self.request("GET", f"/api/anomalies?active=1&house_id={house_id}")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["rule"] == "over_power" for a in anomalies))

        status, booking = self.request("POST", "/api/bookings", {
            "room_id": room_id, "guest_name": "API游客", "guest_count": 1,
            "check_in_at": "2026-10-01T10:00:00Z", "check_out_at": "2026-10-03T12:00:00Z",
        })
        self.assertEqual(status, 201)
        # Too early to precool: command endpoint itself is allowed by service but board is scheduled.
        status, overview = self.request("GET", "/api/board/arrivals?" + urlencode({"hours": 72}))
        self.assertEqual(status, 200)
        self.assertTrue(any(a["booking_id"] == booking["id"] for a in overview))

    def test_monthly_report(self):
        status, report = self.request("GET", "/api/reports/monthly?year=2026&month=9")
        self.assertEqual(status, 200)
        self.assertIn("totals", report)
        self.assertGreaterEqual(report["totals"]["bills"], 1)


if __name__ == "__main__":
    unittest.main()
