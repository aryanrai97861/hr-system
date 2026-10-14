"""
Comprehensive test suite for Employee Attendance & Analytics API.
Tests all endpoints, business rules (R1-R10), edge cases, and concurrency guarantees.
"""
import concurrent.futures
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure project root is in sys.path regardless of how pytest is invoked
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.main import app, client, db, init_indexes, IST, UTC

test_client = TestClient(app)


@pytest.fixture(autouse=True, scope="session")
def setup_database():
    """Ensure indexes are created before running tests."""
    try:
        client.admin.command("ping")
        init_indexes()
    except Exception as exc:
        pytest.fail(
            f"Cannot connect to MongoDB: {exc}. "
            "If using MongoDB Atlas, please ensure your current IP is added to the IP Access List in Atlas Network Access (or allow 0.0.0.0/0)."
        )


def test_health():
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_employee_success_and_conflict():
    unique_code = f"EMP{int(datetime.now().timestamp()) % 90000 + 10000}"
    payload = {
        "emp_code": unique_code,
        "name": "Test User",
        "email": f"test_{unique_code}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }

    # 1. Create employee (201)
    res = test_client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == unique_code
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000

    # 2. Duplicate create returns 409 Conflict
    res_dup = test_client.post("/employees", json=payload)
    assert res_dup.status_code == 409


def test_create_employee_validation_errors():
    # Bad emp_code pattern
    res = test_client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Bad",
        "email": "bad@example.com",
        "department": "IT",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Equal shift times (shift_start == shift_end)
    res_shift = test_client.post("/employees", json={
        "emp_code": "EMP8888",
        "name": "Bad Shift",
        "email": "shift@example.com",
        "department": "IT",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01"
    })
    assert res_shift.status_code == 422


def test_list_employees_pagination_and_sorting():
    res = test_client.get("/employees?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 5

    # Check sorting by emp_code asc
    emp_codes = [e["emp_code"] for e in data["items"]]
    assert emp_codes == sorted(emp_codes)


def test_punch_in_r1_and_r2_rules():
    # Create an employee for punch-in tests
    emp_code = f"EMP{int(datetime.now().timestamp()) % 80000 + 20000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punch Tester",
        "email": f"{emp_code}@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Test 1: On-time punch in 09:28 IST (03:58 UTC) on 2026-08-10
    # 09:28 IST = 03:58 UTC
    dt_ontime = datetime(2026, 8, 10, 3, 58, 0, tzinfo=timezone.utc)
    ms_ontime = int(dt_ontime.timestamp() * 1000)

    res = test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_ontime,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    rec = res.json()
    assert rec["date"] == "2026-08-10"
    assert rec["late_minutes"] == 0
    assert rec["punch_in"] == ms_ontime
    assert rec["punch_out"] is None
    assert "id" not in rec

    # Test 2: Duplicate punch-in for same date yields 409
    res_dup = test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_ontime,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Test 3: R2 Strict Late Boundary (09:40:00 -> 0; 09:40:01 -> 10)
    # Date 2026-08-11
    # 09:40:00 IST = 04:10:00 UTC
    dt_grace_end = datetime(2026, 8, 11, 4, 10, 0, tzinfo=timezone.utc)
    res_grace = test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_grace_end.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert res_grace.status_code == 201
    assert res_grace.json()["late_minutes"] == 0

    # Date 2026-08-12: 09:40:01 IST = 04:10:01 UTC -> 10 minutes late!
    dt_late = datetime(2026, 8, 12, 4, 10, 1, tzinfo=timezone.utc)
    res_late = test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_late.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 10


def test_overnight_shift_punch_in_and_out():
    # Overnight employee: 22:00 - 06:00
    emp_code = f"EMP{int(datetime.now().timestamp()) % 70000 + 30000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Night Owl",
        "email": f"{emp_code}@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in on 2026-09-02 at 01:15 IST (2026-09-01 19:45 UTC).
    # Since 01:15 IST < 06:00 IST (shift_end), R1 dictates the shift started yesterday: 2026-09-01!
    dt_night_in = datetime(2026, 9, 1, 19, 45, 0, tzinfo=timezone.utc)
    res_in = test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_night_in.timestamp() * 1000),
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    data = res_in.json()
    assert data["date"] == "2026-09-01"

    # Punch out at 06:40 IST on 2026-09-02 (2026-09-02 01:10 UTC).
    # Overtime = 40 minutes (shift end was 06:00 IST on 2026-09-02).
    dt_night_out = datetime(2026, 9, 2, 1, 10, 0, tzinfo=timezone.utc)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": int(dt_night_out.timestamp() * 1000)
    })
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["date"] == "2026-09-01"
    assert out_data["overtime_minutes"] == 40
    assert out_data["work_hours"] == 5.42
    assert out_data["half_day"] is False


def test_punch_out_business_rules():
    emp_code = f"EMP{int(datetime.now().timestamp()) % 60000 + 40000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Day Worker",
        "email": f"{emp_code}@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 09:30 IST (04:00 UTC) on 2026-10-01
    dt_in = datetime(2026, 10, 1, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_in.timestamp() * 1000)
    })

    # Punch out 13:00 IST (07:30 UTC) -> 3.5 hours worked (< 4.50 -> half_day = True)
    dt_out = datetime(2026, 10, 1, 7, 30, 0, tzinfo=timezone.utc)
    res_out = test_client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": int(dt_out.timestamp() * 1000)
    })
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["work_hours"] == 3.5
    assert data["half_day"] is True
    assert data["overtime_minutes"] == 0

    # Duplicate punch-out yields 409
    res_dup = test_client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": int(dt_out.timestamp() * 1000)
    })
    assert res_dup.status_code == 409


def test_regularize_attendance_and_history():
    emp_code = f"EMP{int(datetime.now().timestamp()) % 40000 + 60000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Regularize Tester",
        "email": f"{emp_code}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Punch in on 2026-07-20
    dt_in = datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_in.timestamp() * 1000),
        "status": "PRESENT"
    })

    # Regularize status to WFH
    payload = {
        "status": "WFH",
        "reason": "Approved work from home request",
        "regularized_by": "hr.manager"
    }

    res = test_client.patch(f"/attendance/{emp_code}/2026-07-20", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) >= 1
    last_hist = data["history"][-1]
    assert last_hist["by"] == "hr.manager"
    assert "status" in last_hist["changes"]
    assert last_hist["changes"]["status"]["from"] == "PRESENT"
    assert last_hist["changes"]["status"]["to"] == "WFH"

    # Request that changes nothing yields 422
    res_no_change = test_client.patch(f"/attendance/{emp_code}/2026-07-20", json=payload)
    assert res_no_change.status_code == 422


def test_analytics_employee_monthly():
    # EMP0001 in 2026-07
    res = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0


def test_analytics_department_summary():
    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 1
    departments = [d["department"] for d in data["items"]]
    assert departments == sorted(departments)


def test_analytics_leaderboard_late():
    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    for item in data["items"]:
        assert item["rank"] <= 10
        assert item["total_late_minutes"] > 0


def test_analytics_department_trend():
    res = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 5
    for item in data["items"]:
        assert "date" in item
        assert "is_working_day" in item
        assert "headcount" in item
        assert "moving_avg_7d" in item


def test_explain_endpoints():
    # 1. attendance_list
    res1 = test_client.get("/admin/explain/attendance_list")
    assert res1.status_code == 200
    assert res1.json()["endpoint"] == "attendance_list"
    assert res1.json()["collection"] == "attendance_logs"

    # 2. employee_monthly
    res2 = test_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["endpoint"] == "employee_monthly"

    # 3. department_summary
    res3 = test_client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["endpoint"] == "department_summary"

    # 4. late_leaderboard
    res4 = test_client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["endpoint"] == "late_leaderboard"

    # 5. department_trend
    res5 = test_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10")
    assert res5.status_code == 200
    assert res5.json()["endpoint"] == "department_trend"


def test_concurrent_punch_in_race():
    emp_code = f"EMP{int(datetime.now().timestamp()) % 50000 + 50000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Tester",
        "email": f"{emp_code}@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    target_ts = int(datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch():
        return test_client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": target_ts,
            "status": "PRESENT"
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        statuses = [f.result().status_code for f in futures]

    # Exactly one request must succeed (201) and all others must conflict (409)
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4


def test_concurrent_punch_out_race():
    emp_code = f"EMP{int(datetime.now().timestamp()) % 50000 + 45000}"
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punchout Race Tester",
        "email": f"{emp_code}@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in on 2026-11-05
    dt_in = datetime(2026, 11, 5, 4, 0, 0, tzinfo=timezone.utc)
    test_client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": int(dt_in.timestamp() * 1000)
    })

    dt_out = datetime(2026, 11, 5, 13, 0, 0, tzinfo=timezone.utc)
    target_out_ts = int(dt_out.timestamp() * 1000)

    def do_punch_out():
        return test_client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": target_out_ts
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        statuses = [f.result().status_code for f in futures]

    # Exactly one 200 and competitors 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4


def test_mid_month_joiner_working_days():
    emp_code = f"EMP{int(datetime.now().timestamp()) % 30000 + 70000}"
    # Joins on 2026-07-20 (Monday)
    test_client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Mid Month Joiner",
        "email": f"{emp_code}@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20"
    })

    # In July 2026, from July 20 to July 31 there are exactly 10 weekdays:
    # July 20-24 (5 days), July 27-31 (5 days) = 10 working days
    res = test_client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 10
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_leaderboard_ties_and_cutoff():
    # Month: 2026-05
    # Create two employees in a unique department who tie on late minutes
    unique_suffix = f"{int(datetime.now().timestamp() * 1000) % 90000 + 10000}"
    dept_name = f"Dept_{unique_suffix}"
    emp1 = f"EMP{unique_suffix}1"
    emp2 = f"EMP{unique_suffix}2"
    test_client.post("/employees", json={
        "emp_code": emp1,
        "name": "Tied One",
        "email": f"{emp1}@example.com",
        "department": dept_name,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    test_client.post("/employees", json={
        "emp_code": emp2,
        "name": "Tied Two",
        "email": f"{emp2}@example.com",
        "department": dept_name,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 35 mins late on 2026-05-04 (10:05 IST = 04:35 UTC)
    ts_late = int(datetime(2026, 5, 4, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post("/attendance/punch-in", json={"emp_code": emp1, "punched_at": ts_late})
    test_client.post("/attendance/punch-in", json={"emp_code": emp2, "punched_at": ts_late})

    # Query leaderboard with limit=1 inside unique department
    res = test_client.get(f"/analytics/leaderboard/late?month=2026-05&limit=1&department={dept_name}")
    assert res.status_code == 200
    items = res.json()["items"]
    # Both tied employees have rank 1, so both must be returned even though limit=1!
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1
    assert items[0]["total_late_minutes"] == items[1]["total_late_minutes"]
    # Ties ordered by emp_code asc
    assert items[0]["emp_code"] < items[1]["emp_code"]


def test_explain_index_scan():
    # Verify that attendance_list explain uses an index scan (IXSCAN)
    res = test_client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31")
    assert res.status_code == 200
    explain_data = res.json()["explain"]
    # Traverse winning plan for IXSCAN
    explain_str = str(explain_data)
    assert "IXSCAN" in explain_str


def test_strict_epoch_millis_validation():
    # Seconds (< 100000000000) rejected with 422
    res_sec = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res_sec.status_code == 422

    # String rejected with 422
    res_str = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"})
    assert res_str.status_code == 422

    # Float rejected with 422
    res_flt = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000.0})
    assert res_flt.status_code == 422


def test_invalid_date_validation():
    # Invalid calendar date like 2026-02-31 rejected with 422
    res1 = test_client.get("/attendance?date_from=2026-02-31")
    assert res1.status_code == 422

    res2 = test_client.patch("/attendance/EMP0001/2026-02-31", json={
        "reason": "Test reason",
        "regularized_by": "HR",
        "status": "WFH"
    })
    assert res2.status_code == 422


def test_explain_all_endpoints_zero_collscan():
    import json
    urls = [
        ("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31", "attendance_list"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "employee_monthly"),
        ("/admin/explain/department_summary?month=2026-07", "department_summary"),
        ("/admin/explain/late_leaderboard?month=2026-07", "late_leaderboard"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10", "department_trend"),
    ]
    for url, name in urls:
        r = test_client.get(url)
        assert r.status_code == 200, f"{name} explain failed with status {r.status_code}: {r.text}"
        s = json.dumps(r.json()["explain"])
        assert "IXSCAN" in s, f"{name} plan missing IXSCAN"
        assert "COLLSCAN" not in s, f"{name} plan contains COLLSCAN"


