import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from app import main


class CalculationTests(unittest.TestCase):
    def test_grace_period_uses_seconds_before_flooring_minutes(self):
        for seconds, expected in ((0, 0), (1, 10), (59, 10)):
            with self.subTest(seconds=seconds):
                punch = datetime(2026, 7, 6, 9, 40, seconds, tzinfo=main.IST)
                self.assertEqual(main.compute_late_minutes(punch, "09:30"), expected)

    def test_overtime_requires_thirty_whole_minutes(self):
        for minutes, seconds, expected in ((29, 59, 0), (30, 0, 30), (40, 59, 40)):
            with self.subTest(minutes=minutes, seconds=seconds):
                punch = datetime(2026, 7, 6, 18, 30, tzinfo=main.IST)
                punch += timedelta(minutes=minutes, seconds=seconds)
                self.assertEqual(main.compute_overtime(punch, "18:30", "2026-07-06"), expected)

    def test_work_hours_round_half_up(self):
        start = datetime(2026, 7, 6, 9, 30, tzinfo=main.IST)
        self.assertEqual(main.compute_work_hours(start, start + timedelta(seconds=3618)), 1.01)

    def test_overnight_date_boundary_and_half_day_rounding(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        for clock, expected in (("05:59:59", "2026-07-06"), ("06:00:00", "2026-07-07")):
            stamp = datetime.fromisoformat(f"2026-07-07T{clock}+05:30")
            self.assertEqual(main.attendance_date(stamp, employee), expected)
        start = datetime(2026, 7, 6, 22, tzinfo=main.IST)
        record = {"date": "2026-07-06", "status": "WFH", "punch_in": start}
        for seconds, half_day in ((16181, True), (16182, False)):
            result = main.derived_fields({**record, "punch_out": start + timedelta(seconds=seconds)}, employee)
            self.assertEqual(result["half_day"], half_day)

    def test_nested_history_serialization_and_missing_defaults(self):
        stamp = datetime(2026, 7, 6, 4, tzinfo=main.UTC)
        record = {"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE"}
        result = main.attendance_response(record)
        self.assertEqual(result["history"], [])
        self.assertIs(result["half_day"], False)
        record["history"] = [{"at": stamp, "changes": {"punch_in": {"from": None, "to": stamp}}}]
        result = main.attendance_response(record)
        self.assertEqual(result["history"][0]["changes"]["punch_in"]["to"], main.json_value(stamp))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)
        self.addCleanup(self.http.close)

    def test_timestamp_and_presence_validation(self):
        for stamp in (1783312500, 1783312500000.0, "1783312500000", True, None, 4102444800001):
            for path in ("punch-in", "punch-out"):
                with self.subTest(stamp=stamp, path=path):
                    response = self.http.post(f"/attendance/{path}", json={"emp_code": "EMP0001", "punched_at": stamp})
                    self.assertEqual(response.status_code, 422, response.text)
        for status in ("ABSENT", "LEAVE", "present"):
            self.assertEqual(self.http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status}).status_code, 422)

    def test_invalid_dates_pagination_and_employee_fields(self):
        for query in ("page=0", "page_size=101", "date_from=2026-02-30", "date_to=20260701", "status=other", "date_from=2026-08-01&date_to=2026-07-01"):
            self.assertEqual(self.http.get(f"/attendance?{query}").status_code, 422)
        valid = {"emp_code": "EMP0001", "name": "Asha", "email": "asha@example.com", "department": "Engineering", "joined_on": "2026-07-01"}
        for changes in ({"emp_code": "123"}, {"name": ""}, {"email": "bad"}, {"joined_on": "2026-02-30"}, {"shift_start": "9:30"}, {"shift_start": "09:30", "shift_end": "09:30"}):
            response = self.http.post("/employees", json={**valid, **changes})
            self.assertEqual(response.status_code, 422, response.text)

    def test_health_reports_database_failure(self):
        with patch.object(main.client.admin, "command", side_effect=ConnectionFailure("offline")):
            self.assertEqual(self.http.get("/health").status_code, 503)

    def test_analytics_and_explain_reject_missing_or_invalid_parameters(self):
        for endpoint in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend", "unknown"):
            self.assertEqual(self.http.get(f"/admin/explain/{endpoint}").status_code, 422)
        for month in ("2026-13", "2026-1", "0000-01", "202607", "2026-07-01"):
            response = self.http.get("/analytics/departments/summary", params={"month": month})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.http.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code, 422)
        self.assertEqual(self.http.get("/admin/explain/attendance_list?date_from=2026-08-01&date_to=2026-07-01").status_code, 422)


@unittest.skipUnless(os.getenv("TEST_MONGO_URI"), "Set TEST_MONGO_URI for real MongoDB integration tests")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=3000)
        cls.mongo.admin.command("ping")
        cls.database = cls.mongo[f"attendance_test_{uuid4().hex}"]
        cls.database_patch = patch.object(main, "db", cls.database)
        cls.client_patch = patch.object(main, "client", cls.mongo)
        cls.database_patch.start()
        cls.client_patch.start()
        cls.http = TestClient(main.app)
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.drop_database(cls.database.name)
        cls.http.__exit__(None, None, None)
        cls.client_patch.stop()
        cls.database_patch.stop()

    def setUp(self):
        self.database.employees.delete_many({})
        self.database.attendance_logs.delete_many({})

    def employee(self, number=1, **changes):
        document = {
            "emp_code": f"EMP{number:04d}", "name": f"Employee {number}", "email": f"employee{number}@example.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
            **changes,
        }
        response = self.http.post("/employees", json=document)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def milliseconds(self, value):
        return main.json_value(datetime.fromisoformat(value))

    def log(self, number=1, day="2026-07-06", **changes):
        document = {
            "emp_code": f"EMP{number:04d}", "date": day, "status": "PRESENT",
            "punch_in": datetime.fromisoformat(f"{day}T09:30:00+05:30"),
            "punch_out": datetime.fromisoformat(f"{day}T18:30:00+05:30"),
            "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
            **changes,
        }
        self.database.attendance_logs.insert_one(document)

    def test_employee_and_attendance_pagination(self):
        self.employee(3)
        self.employee(1)
        self.employee(2, department="Sales")
        response = self.http.get("/employees", params={"department": "Engineering", "page_size": 1}).json()
        self.assertEqual(response["total"], 2)
        self.assertEqual(response["items"][0]["emp_code"], "EMP0001")
        self.assertIsInstance(response["items"][0]["created_at"], int)
        self.assertEqual(self.http.get("/employees?department=engineering").json()["total"], 0)
        for number in (3, 1, 2):
            self.log(number)
        self.log(1, "2026-07-07")
        response = self.http.get("/attendance?page_size=2").json()
        self.assertEqual([(item["date"], item["emp_code"]) for item in response["items"]], [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001")])
        self.assertEqual(response["total"], 4)
        self.assertFalse({"id", "_id"} & response["items"][0].keys())
        self.assertEqual(self.http.get("/attendance?emp_code=EMP0003&date_to=2026-07-06").json()["total"], 1)

    def test_atomic_employee_and_punch_races(self):
        payload = {"emp_code": "EMP0001", "name": "Asha", "email": "a@example.com", "department": "Engineering", "joined_on": "2026-07-01"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.http.post("/employees", json=payload).status_code, range(8)))
        self.assertEqual(sorted(responses), [201] + [409] * 7)
        payload = {"emp_code": "EMP0001", "punched_at": self.milliseconds("2026-07-06T09:40:01.900+05:30"), "work_hours": 99}
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.http.post("/attendance/punch-in", json=payload).status_code, range(8)))
        self.assertEqual(sorted(responses), [201] + [409] * 7)
        record = self.http.get("/attendance").json()["items"][0]
        self.assertEqual(record["late_minutes"], 10)
        self.assertEqual(record["punch_in"] % 1000, 0)
        self.assertIsNone(record["work_hours"])
        payload["punched_at"] = self.milliseconds("2026-07-06T19:00:00+05:30")
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.http.post("/attendance/punch-out", json=payload).status_code, range(8)))
        self.assertEqual(sorted(responses), [200] + [409] * 7)
        self.assertEqual(self.http.get("/attendance").json()["items"][0]["history"], [])

    def test_overnight_punch_and_punch_out_selection(self):
        self.employee(shift_start="22:00", shift_end="06:00")
        request = {"emp_code": "EMP0001", "punched_at": self.milliseconds("2026-07-07T02:00:00+05:30")}
        response = self.http.post("/attendance/punch-in", json=request)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["date"], "2026-07-06")
        self.assertEqual(response.json()["late_minutes"], 240)
        request["punched_at"] = self.milliseconds("2026-07-07T06:40:00+05:30")
        response = self.http.post("/attendance/punch-out", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["overtime_minutes"], 40)
        self.assertEqual(response.json()["work_hours"], 4.67)
        self.log(day="2026-07-05", punch_out=None)
        self.assertEqual(self.http.post("/attendance/punch-out", json=request).status_code, 409)

    def test_punch_errors_and_twenty_four_hour_limit(self):
        timestamp = self.milliseconds("2026-07-06T09:30:00+05:30")
        payload = {"emp_code": "EMP0001", "punched_at": timestamp}
        self.assertEqual(self.http.post("/attendance/punch-in", json=payload).status_code, 404)
        self.employee()
        self.assertEqual(self.http.post("/attendance/punch-out", json=payload).status_code, 404)
        self.assertEqual(self.http.post("/attendance/punch-in", json=payload).status_code, 201)
        self.assertEqual(self.http.post("/attendance/punch-out", json=payload).status_code, 422)
        payload["punched_at"] = timestamp + 86401000
        self.assertEqual(self.http.post("/attendance/punch-out", json=payload).status_code, 422)
        payload["punched_at"] = timestamp + 86400000
        self.assertEqual(self.http.post("/attendance/punch-out", json=payload).status_code, 200)

    def test_corrections_preserve_history_and_validate_final_state(self):
        self.employee()
        self.log(late_minutes=10)
        path = "/attendance/EMP0001/2026-07-06"
        metadata = {"reason": "Clock correction", "regularized_by": "hr.admin"}
        response = self.http.patch(path, json={**metadata, "punch_in": self.milliseconds("2026-07-06T10:00:00+05:30")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()["history"][0]["changes"]), {"punch_in", "late_minutes", "work_hours"})
        self.assertIsInstance(response.json()["history"][0]["changes"]["punch_in"]["from"], int)
        self.assertEqual(self.http.patch(path, json=metadata).status_code, 422)
        self.assertEqual(self.http.patch(path, json={**metadata, "punch_in": self.milliseconds("2026-07-07T09:30:00+05:30")}).status_code, 422)
        self.assertEqual(self.http.patch(path, json={**metadata, "punch_out": None}).status_code, 422)
        self.assertEqual(self.http.patch(path, json={**metadata, "status": "LEAVE", "punch_out": self.milliseconds("2026-07-06T19:00:00+05:30")}).status_code, 422)
        response = self.http.patch(path, json={**metadata, "status": "LEAVE"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["history"]), 2)
        self.assertIsNone(response.json()["punch_in"])
        self.assertIsNone(response.json()["punch_out"])
        self.assertEqual(response.json()["late_minutes"], 0)
        self.assertEqual(self.http.patch(path, json={**metadata, "status": "WFH"}).status_code, 422)
        response = self.http.patch(path, json={**metadata, "status": "WFH", "punch_in": self.milliseconds("2026-07-06T09:30:00+05:30")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["history"]), 3)

    def test_concurrent_corrections_keep_every_successful_history_entry(self):
        self.employee()
        self.log()
        def correct(offset):
            return self.http.patch("/attendance/EMP0001/2026-07-06", json={
                "punch_in": self.milliseconds("2026-07-06T10:00:00+05:30") + offset * 60000,
                "reason": "Clock correction", "regularized_by": f"hr.{offset}",
            }).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(correct, range(8)))
        self.assertTrue(all(status in (200, 409) for status in responses), responses)
        record = self.http.get("/attendance").json()["items"][0]
        self.assertEqual(len(record["history"]), responses.count(200))

    def test_monthly_and_department_use_stored_values_and_all_employees(self):
        self.employee(joined_on="2026-07-06")
        self.employee(2, joined_on="2026-07-15")
        self.employee(3, joined_on="2026-08-01")
        self.employee(4, department="Sales")
        self.employee(5)
        self.log(5, work_hours=1)
        self.log(work_hours=2.0, late_minutes=10)
        self.log(day="2026-07-07", status="WFH", work_hours=4.49, half_day=True, late_minutes=20)
        self.log(day="2026-07-11", work_hours=10, late_minutes=7, overtime_minutes=30)
        self.log(day="2026-07-08", status="LEAVE", punch_in=None, punch_out=None, work_hours=None)
        response = self.http.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"emp_code": "EMP0001", "month": "2026-07", "working_days": 20, "present_days": 1.5, "leave_days": 1, "late_count": 3, "total_late_minutes": 37, "total_overtime_minutes": 30, "attendance_pct": 7.5})
        response = self.http.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(response.status_code, 200, response.text)
        engineering, sales = response.json()["items"]
        self.assertEqual(engineering["headcount"], 3)
        self.assertEqual(engineering["present_days"], 2.5)
        self.assertEqual(engineering["avg_work_hours"], 4.37)
        self.assertEqual(sales["headcount"], 1)
        self.assertIsNone(sales["avg_work_hours"])
        future = self.http.get("/analytics/employees/EMP0003/monthly?month=2026-07").json()
        self.assertEqual(future["working_days"], 0)
        self.assertIsNone(future["attendance_pct"])

    def test_leaderboard_competition_ranks_and_department_filter(self):
        for number, total in ((1, 50), (2, 40), (3, 40), (4, 20)):
            self.employee(number, department="Sales" if number == 1 else "Engineering")
            self.log(number, late_minutes=total)
        self.log(99, late_minutes=1000)
        response = self.http.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([(item["emp_code"], item["rank"]) for item in response.json()["items"]], [("EMP0001", 1), ("EMP0002", 2), ("EMP0003", 2)])
        response = self.http.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=1")
        self.assertEqual([item["rank"] for item in response.json()["items"]], [1, 1])

    def test_trend_fills_gaps_and_uses_in_range_non_null_window(self):
        self.employee()
        self.employee(2, joined_on="2026-07-08")
        self.log(day="2026-07-03", late_minutes=500)
        self.log(day="2026-07-06")
        self.log(day="2026-07-08", half_day=True, late_minutes=10)
        self.log(day="2026-07-11")
        response = self.http.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-14")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 9)
        self.assertEqual([item["attendance_rate"] for item in items], [1.0, 0.0, 0.25, 0.0, 0.0, None, None, 0.0, 0.0])
        self.assertEqual([item["moving_avg_7d"] for item in items], [1.0, 0.5, 0.4167, 0.3125, 0.25, 0.25, 0.25, 0.05, 0.05])
        self.assertEqual(items[5]["present_count"], 1)
        self.assertEqual(items[0]["headcount"], 1)
        self.assertEqual(items[2]["headcount"], 2)
        empty = self.http.get("/analytics/departments/Engineering/trend?from=2026-06-01&to=2026-06-02").json()["items"]
        self.assertEqual(len(empty), 2)
        self.assertTrue(all(item["headcount"] == 0 and item["moving_avg_7d"] is None for item in empty))
        self.assertEqual(self.http.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-10-01").status_code, 422)
        self.assertEqual(self.http.get("/analytics/departments/Unknown/trend?from=2026-07-01&to=2026-07-01").status_code, 404)

    def test_empty_analytics_and_missing_seed_fields(self):
        self.assertEqual(self.http.get("/analytics/departments/summary?month=2026-07").json()["items"], [])
        self.assertEqual(self.http.get("/analytics/employees/EMP0001/monthly?month=2026-07").status_code, 404)
        self.employee()
        monthly = self.http.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
        self.assertEqual(monthly["working_days"], 23)
        self.assertEqual(monthly["attendance_pct"], 0)
        self.assertEqual(self.http.get("/analytics/leaderboard/late?month=2026-07").json()["items"], [])
        trend = self.http.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-09-30")
        self.assertEqual(trend.status_code, 200, trend.text)
        self.assertEqual(len(trend.json()["items"]), 92)
        self.assertEqual(trend.json()["items"][0]["attendance_rate"], 0)
        self.database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "ON_DUTY", "punch_in": main.instant(1783310400000)})
        self.database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE"})
        monthly = self.http.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
        self.assertEqual(monthly["present_days"], 1)
        self.assertEqual(monthly["leave_days"], 1)
        self.assertEqual(monthly["late_count"], 0)
        summary = self.http.get("/analytics/departments/summary?month=2026-07").json()["items"][0]
        self.assertIsNone(summary["avg_work_hours"])
        self.assertEqual(summary["on_duty_count"], 1)

    def test_aggregation_half_up_rounding_and_zero_work_hours(self):
        self.employee()
        self.log(work_hours=2.66)
        self.log(day="2026-07-07", work_hours=2.67)
        summary = self.http.get("/analytics/departments/summary?month=2026-07").json()["items"][0]
        self.assertEqual(summary["avg_work_hours"], 2.67)
        self.log(day="2026-07-08", work_hours=0)
        self.log(day="2026-07-09", work_hours=None, punch_out=None)
        summary = self.http.get("/analytics/departments/summary?month=2026-07").json()["items"][0]
        self.assertEqual(summary["avg_work_hours"], 1.78)
        self.assertEqual(summary["present_days"], 4)

    def test_explain_uses_the_real_operation_and_indexes(self):
        self.employee()
        self.log()
        operations = [
            ("attendance_list", {"emp_code": "EMP0001", "page": 2, "page_size": 3, "status": "PRESENT"}),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07", "department": "Engineering"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 2}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
        ]
        for endpoint, parameters in operations:
            with self.subTest(endpoint=endpoint):
                response = self.http.get(f"/admin/explain/{endpoint}", params=parameters)
                self.assertEqual(response.status_code, 200, response.text)
                document = response.json()
                builder_parameters = {({"from": "trend_from", "to": "trend_to"}.get(key, key)): value for key, value in parameters.items()}
                collection, pipeline, index = main.main_operation(endpoint, **builder_parameters)
                command = document["explain"]["command"]
                self.assertEqual(command["pipeline"], main.json.loads(main.json_util.dumps(pipeline)))
                self.assertEqual(command["hint"], index)
                self.assertEqual(document["collection"], collection)
                self.assert_plan_is_indexed(document["explain"])

    def assert_plan_is_indexed(self, document):
        stages = []
        def visit(value):
            if isinstance(value, dict):
                if "stage" in value:
                    stages.append(value["stage"])
                if "collectionScans" in value:
                    self.assertEqual(value["collectionScans"], 0)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(document)
        self.assertIn("IXSCAN", stages)
        self.assertNotIn("COLLSCAN", stages)

    @unittest.skipUnless(os.getenv("RUN_SCALE_TESTS") == "1", "Opt-in 100,000-record startup and query-plan check")
    def test_scale_index_build_and_queries(self):
        start = datetime(2026, 5, 1, 4, tzinfo=main.UTC)
        self.database.employees.insert_many([
            {"emp_code": f"EMP{number:04d}", "name": f"Employee {number}", "email": f"employee{number}@example.com",
             "department": f"D{number % 10:02d}", "shift_start": "09:30", "shift_end": "18:30",
             "joined_on": "2026-05-01", "created_at": start}
            for number in range(1, 1001)
        ])
        for batch in range(20):
            records = []
            for position in range(batch * 5000, (batch + 1) * 5000):
                number, offset = position // 100 + 1, position % 100
                timestamp = start + timedelta(days=offset)
                records.append({
                    "emp_code": f"EMP{number:04d}", "date": timestamp.date().isoformat(), "status": "PRESENT",
                    "punch_in": timestamp, "punch_out": timestamp + timedelta(hours=9),
                    "work_hours": 9.0, "late_minutes": number % 37, "overtime_minutes": 0, "half_day": False,
                })
            self.database.attendance_logs.insert_many(records)
        self.assertEqual(self.database.attendance_logs.count_documents({}), 100000)
        self.database.employees.drop_indexes()
        self.database.attendance_logs.drop_indexes()
        started = time.perf_counter()
        main.create_indexes()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 20)
        print(f"\n100k records: startup indexes {elapsed:.2f}s")
        operations = [
            ("attendance_list", "/attendance", {}),
            ("attendance_list", "/attendance", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2}),
            ("attendance_list", "/attendance", {"status": "PRESENT", "date_from": "2026-07-01"}),
            ("employee_monthly", "/analytics/employees/EMP0001/monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
            ("department_summary", "/analytics/departments/summary", {"month": "2026-07", "department": "D00"}),
            ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 2}),
            ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 2, "department": "D00"}),
            ("department_trend", "/analytics/departments/D00/trend", {"department": "D00", "from": "2026-05-01", "to": "2026-07-31"}),
        ]
        for endpoint, path, parameters in operations:
            with self.subTest(endpoint=endpoint, parameters=parameters):
                started = time.perf_counter()
                response = self.http.get(path, params=parameters)
                elapsed = time.perf_counter() - started
                self.assertEqual(response.status_code, 200, response.text)
                print(f"{endpoint} {parameters}: {elapsed:.3f}s")
                response = self.http.get(f"/admin/explain/{endpoint}", params=parameters)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_plan_is_indexed(response.json()["explain"])


if __name__ == "__main__":
    unittest.main()