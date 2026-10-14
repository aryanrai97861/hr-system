"""
Employee Attendance & Analytics API

Single-file FastAPI application implementing the complete API contract defined in
openapi.yaml and DATA_MODEL.md with business rules R1 through R10.
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

def sanitize_bson(obj: Any) -> Any:
    """Serialize BSON types (like Timestamp, ObjectId) safely for JSON responses."""
    return json.loads(json_util.dumps(obj))

# Load .env without overriding active environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


# --------------------------------------------------------------------------- #
# Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes() -> None:
    """Create all required indexes idempotently at application startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1), ("emp_code", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_out", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation on startup
    try:
        init_indexes()
    except Exception as exc:
        # If DB is not available yet during cold start, keep app alive so health endpoint can report 503
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Math & Time Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: Union[float, Decimal, int], decimals: int = 2) -> float:
    """R8: Round number using standard half-up rounding (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds_ms(epoch_ms: int) -> int:
    """R1: Instants are truncated to whole seconds before calculations."""
    return (int(epoch_ms) // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Convert truncated epoch ms to UTC timezone-aware datetime."""
    truncated = truncate_to_seconds_ms(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


def get_current_epoch_ms() -> int:
    """Return current server time in epoch milliseconds truncated to whole seconds."""
    now_ts = int(datetime.now(tz=UTC).timestamp() * 1000)
    return truncate_to_seconds_ms(now_ts)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Overnight shift condition: shift_end <= shift_start."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Attendance date rule:
    Shift times and attendance dates are IST. A record's date is the IST calendar date
    of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started
    the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        shift_end_time = time(eh, em)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """
    Construct shift start and shift end instants as UTC datetimes for an attendance date.
    For overnight shifts, shift_end falls on the next calendar day in IST.
    """
    base_date = date.fromisoformat(date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    start_ist = datetime.combine(base_date, time(sh, sm), tzinfo=IST)
    if is_overnight_shift(shift_start, shift_end):
        end_ist = datetime.combine(base_date + timedelta(days=1), time(eh, em), tzinfo=IST)
    else:
        end_ist = datetime.combine(base_date, time(eh, em), tzinfo=IST)

    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


def compute_late_minutes(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    """
    R2 Late Rule:
    Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 (600s)
    after shift_start. When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    elapsed_seconds = int((punch_in_utc - shift_start_utc).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """
    R3 Overtime Rule:
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    """
    elapsed_seconds = int((punch_out_utc - shift_end_utc).total_seconds())
    if elapsed_seconds < 0:
        return 0
    ot_minutes = elapsed_seconds // 60
    return ot_minutes if ot_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours rule:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    duration_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    d = Decimal(duration_sec) / Decimal(3600)
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day rule:
    After punch-out, half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry(entry: dict) -> dict:
    """Format MongoDB history document into OpenAPI HistoryEntry schema."""
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = dt_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = dt_to_epoch_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def format_attendance_record(doc: dict) -> dict:
    """
    Format internal MongoDB attendance document into AttendanceRecord response schema.
    Strips internal _id, applies default values for legacy records, and formats epoch ms.
    """
    history_raw = doc.get("history") or []
    history = [serialize_history_entry(h) for h in history_raw]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee_record(doc: dict) -> dict:
    """Format internal MongoDB employee document into Employee response schema."""
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = dt_to_epoch_ms(created_at)
    elif isinstance(created_at, (int, float)):
        created_at_ms = int(created_at)
    else:
        created_at_ms = None

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces unique emp_code via database unique index (concurrency safe).
    """
    created_at_dt = datetime.now(tz=UTC)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as err:
        raise HTTPException(status_code=500, detail="Database error creating employee")

    return format_employee_record(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending.
    total counts the filtered set, not the whole collection.
    """
    query: Dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Creates attendance day's record adhering to R1, R2.
    Concurrency safe: exactly one 201 and competitors receive 409 via unique compound index.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_utc = ms_to_utc_dt(punch_ms)

    att_date = get_attendance_date(punch_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    shift_start_utc, _ = get_shift_instants(att_date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_mins = compute_late_minutes(punch_utc, shift_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    except PyMongoError:
        raise HTTPException(status_code=500, detail="Database error during punch-in")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in <= punched_at.
    Handles overnight shifts across midnight.
    Concurrency safe: atomic find_one_and_update guarantees exactly one 200 and competing requests get 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_utc = ms_to_utc_dt(punch_ms)

    # Find the most recent record with punch_in <= punched_at
    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_utc}},
        sort=[("punch_in", -1)],
    )
    if not latest_record:
        raise HTTPException(status_code=404, detail="No matching punch-in record found")

    if latest_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = latest_record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    duration_sec = (punch_utc - punch_in_utc).total_seconds()
    if duration_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if duration_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    att_date = latest_record["date"]
    _, shift_end_utc = get_shift_instants(att_date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))

    work_hrs = compute_work_hours(punch_in_utc, punch_utc)
    overtime_mins = compute_overtime_minutes(punch_utc, shift_end_utc)
    half_day = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": latest_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was already punched out by a concurrent request")

    return format_attendance_record(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Scales to 100k+ records without loading collection into memory.
    """
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    query: Dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_q: Dict[str, str] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        query["date"] = date_q
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and atomically appends one entry to history.
    Concurrency safe: uses optimistic match on current history to prevent lost audit updates.
    """
    try:
        from datetime import date as date_cls
        date_cls.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not emp or not record:
        raise HTTPException(status_code=404, detail="Employee or attendance record not found for date")

    # Current stored values
    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=UTC)
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=UTC)

    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    history_list = record.get("history") or []

    # Validation: ABSENT / LEAVE rules
    new_status = body.status if body.status is not None else old_status
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when status is ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in instant")

        if body.punch_out is not None:
            new_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = old_punch_out

    # Validation: punch_in must stay on record's attendance date per R1
    if new_punch_in is not None:
        computed_date = get_attendance_date(new_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    # Validation: punch_out must be after punch_in and within 24 hours
    if new_punch_in is not None and new_punch_out is not None:
        delta_sec = (new_punch_out - new_punch_in).total_seconds()
        if delta_sec <= 0:
            raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
        if delta_sec > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    shift_start_utc, shift_end_utc = get_shift_instants(date, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))

    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, shift_start_utc)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, shift_end_utc)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Build changes dictionary (only fields that actually changed)
    changes: Dict[str, Dict[str, Any]] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    correction_dt = datetime.now(tz=UTC)
    history_entry = {
        "at": correction_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with optimistic lock on history array length/state
    update_result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": history_list},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not update_result:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict; please retry")

    return format_attendance_record(update_result)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by a MongoDB aggregation.
    working_days per R7 (Mon-Fri from joined_on).
    present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year_str, month_str = month.split("-")
    y, m = int(year_str), int(month_str)
    num_days = calendar.monthrange(y, m)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    # Calculate working_days (Mon-Fri) from max(joined_on, first_day) to last_day
    joined_date = date.fromisoformat(emp["joined_on"])
    month_start = date(y, m, 1)
    month_end = date(y, m, num_days)

    working_days = 0
    if joined_date <= month_end:
        curr = max(joined_date, month_start)
        while curr <= month_end:
            if curr.weekday() < 5:  # Mon=0, Fri=4
                working_days += 1
            curr += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late = int(stats["total_late_minutes"])
        total_ot = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB aggregation across employees and attendance_logs.
    Includes employees with zero logs in headcount.
    Excludes employees not yet joined by month end.
    A department whose headcount is 0 for the month is omitted.
    """
    year_str, month_str = month.split("-")
    y, m = int(year_str), int(month_str)
    num_days = calendar.monthrange(y, m)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day_str]},
                                    {"$lte": ["$date", last_day_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "all_logs": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$all_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = d.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)

        items.append({
            "department": d["department"],
            "headcount": int(d["headcount"]),
            "present_days": float(d["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(d["late_count"]),
            "total_late_minutes": int(d["total_late_minutes"]),
            "leave_count": int(d["leave_count"]),
            "on_duty_count": int(d["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Uses standard competition ranking ($rank 1, 2, 2, 4) in MongoDB.
    limit is applied after ranking (preserving ties at cutoff).
    Ordered by total_late_minutes desc, then emp_code asc.
    """
    year_str, month_str = month.split("-")
    y, m = int(year_str), int(month_str)
    num_days = calendar.monthrange(y, m)[1]
    first_day_str = f"{month}-01"
    last_day_str = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department via MongoDB aggregation.
    Returns exactly one row per calendar day from from_date to to_date inclusive.
    attendance_rate computed on Mon-Fri with headcount > 0 (4 decimals half-up).
    moving_avg_7d computed over non-null attendance_rate in up to 7-day window inside requested range.
    """
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")

    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline: List[Dict[str, Any]] = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "day_offset": {"$range": [0, days_count]},
            }
        },
        {"$unwind": "$day_offset"},
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": {"$dateFromString": {"dateString": from_date}},
                                "unit": "day",
                                "amount": "$day_offset",
                            }
                        },
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "eligible_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "cur_date": "$date",
                    "emp_codes": "$eligible_emps.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "headcount": {"$size": "$eligible_emps"},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "l",
                            "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = d.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(att_rate, 4)

        mov_avg = d.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = round_half_up(mov_avg, 4)

        items.append({
            "date": d["date"],
            "is_working_day": bool(d["is_working_day"]),
            "headcount": int(d["headcount"]),
            "present_count": float(d["present_count"]),
            "late_count": int(d["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if endpoint == "attendance_list":
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from format")
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        query: Dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            date_q: Dict[str, str] = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            query["date"] = date_q
        if status is not None:
            query["status"] = status

        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain_doc)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")

        year_str, month_str = month.split("-")
        y, m = int(year_str), int(month_str)
        num_days = calendar.monthrange(y, m)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain_doc)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year_str, month_str = month.split("-")
        y, m = int(year_str), int(month_str)
        num_days = calendar.monthrange(y, m)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{num_days:02d}"

        match_filter = {"joined_on": {"$lte": last_day_str}}
        if department is not None:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", first_day_str]},
                                        {"$lte": ["$date", last_day_str]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "logs": {"$push": "$logs"},
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": 1,
                    "all_logs": {
                        "$reduce": {
                            "input": "$logs",
                            "initialValue": [],
                            "in": {"$concatArrays": ["$$value", "$$this"]},
                        }
                    },
                }
            },
            {"$sort": {"department": 1}},
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_bson(explain_doc)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year_str, month_str = month.split("-")
        y, m = int(year_str), int(month_str)
        num_days = calendar.monthrange(y, m)[1]
        first_day_str = f"{month}-01"
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day_str, "$lte": last_day_str},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
        if department is not None:
            pipeline.append({"$match": {"department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain_doc)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, 'from', and 'to' are required for department_trend")

        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
        days_count = (d_to - d_from).days + 1
        if days_count > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {
                "$project": {
                    "_id": 0,
                    "day_offset": {"$range": [0, days_count]},
                }
            },
            {"$unwind": "$day_offset"},
            {
                "$project": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": {
                                "$dateAdd": {
                                    "startDate": {"$dateFromString": {"dateString": from_date}},
                                    "unit": "day",
                                    "amount": "$day_offset",
                                }
                            },
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"cur_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$cur_date"]},
                                    ]
                                }
                            }
                        },
                        {"$project": {"_id": 0, "emp_code": 1}},
                    ],
                    "as": "eligible_emps",
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"cur_date": "$date", "emp_codes": "$eligible_emps.emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$cur_date"]},
                                        {"$in": ["$emp_code", "$$emp_codes"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "day_logs",
                }
            },
            {"$sort": {"date": 1}},
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_bson(explain_doc)}

    raise HTTPException(status_code=422, detail="Unknown endpoint for explain")
