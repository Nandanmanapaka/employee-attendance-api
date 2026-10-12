"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000."""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date as calendar_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.concurrency import run_in_threadpool

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    connect=False,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ATTENDANCE_FIELDS = (
    "emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
    "late_minutes", "overtime_minutes", "half_day", "history",
)
EMPLOYEE_FIELDS = (
    "emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at",
)
DEFAULTS = {
    "punch_in": None, "punch_out": None, "work_hours": None,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
}


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_joined_department",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch"
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    await run_in_threadpool(create_indexes)
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request: Request, exception: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def instant(milliseconds: int | None = None) -> datetime:
    value = datetime.now(UTC) if milliseconds is None else EPOCH + timedelta(milliseconds=milliseconds)
    return value.replace(microsecond=0)


def json_value(value):
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return (aware - EPOCH) // timedelta(milliseconds=1)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return value


def attendance_response(record):
    return json_value({field: record.get(field, DEFAULTS.get(field)) for field in ATTENDANCE_FIELDS})


def employee_response(employee):
    return json_value({field: employee[field] for field in EMPLOYEE_FIELDS})


def find_employee(emp_code: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST).replace(microsecond=0)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str | None = None) -> int:
    local = punch_in.astimezone(IST).replace(microsecond=0)
    day = date_str or local.date().isoformat()
    start = datetime.fromisoformat(f"{day}T{shift_start}").replace(tzinfo=IST)
    seconds = int((local - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "00:00"
) -> int:
    end = datetime.fromisoformat(f"{date_str}T{shift_end}").replace(tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((punch_out.replace(microsecond=0) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(record: dict, employee: dict) -> dict:
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if record["status"] not in PRESENCE:
        return result
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    if punch_in is None:
        raise HTTPException(422, "A presence status requires punch_in")
    if attendance_date(punch_in, employee) != record["date"]:
        raise HTTPException(422, "punch_in must remain on the attendance date")
    result["late_minutes"] = compute_late_minutes(punch_in, employee["shift_start"], record["date"])
    if punch_out is not None:
        duration = punch_out - punch_in
        if not timedelta(0) < duration <= timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        result["work_hours"] = compute_work_hours(punch_in, punch_out)
        result["half_day"] = result["work_hours"] < 4.5
        result["overtime_minutes"] = compute_overtime(
            punch_out, employee["shift_end"], record["date"], employee["shift_start"]
        )
    return result


def snapshot_filter(record: dict) -> dict:
    return {
        "_id": record["_id"],
        **{field: record[field] if field in record else {"$exists": False} for field in ATTENDANCE_FIELDS},
    }


def validate_date(value: str) -> str:
    calendar_date.fromisoformat(value)
    return value


def validate_month(value: str) -> str:
    calendar_date.fromisoformat(f"{value}-01")
    return value


DateString = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(validate_date)
]
MonthString = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(validate_month)
]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
RankLimit = Annotated[int, Query(ge=1, le=50)]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def reject_explicit_null(self):
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class EmployeeIn(RequestBody):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(RequestBody):
    emp_code: str
    punched_at: EpochMillis | None = None


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(RequestBody):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    index = "attendance_employee_date" if emp_code is not None else (
        "attendance_status_date" if status is not None else "attendance_date_employee"
    )
    return query, index


def attendance_pipeline(query, page, page_size):
    return [
        {"$match": query},
        {"$sort": {"date": -1, "emp_code": 1}},
        {"$skip": (page - 1) * page_size},
        {"$limit": page_size},
        {"$project": {"_id": 0}},
    ]


# --------------------------------------------------------------------------- #
# Employee and attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = instant()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    index = "employee_department_code" if department is not None else "employee_code_unique"
    total = db.employees.count_documents(query, hint=index)
    records = db.employees.find(query).hint(index).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(record) for record in records], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = find_employee(body.emp_code)
    timestamp = instant(body.punched_at)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date(timestamp, employee),
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "history": [],
    }
    doc.update(derived_fields(doc, employee))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date") from None
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = find_employee(body.emp_code)
    timestamp = instant(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": timestamp}},
        sort=[("punch_in", -1)], hint="attendance_employee_punch",
    )
    if record is None:
        raise HTTPException(404, "No punch-in found at or before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    changes = {"punch_out": timestamp}
    changes.update(derived_fields({**record, **changes}, employee))
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(record), {"$set": changes}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "Attendance record changed; retry with current values")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query, index = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query, hint=index)
    records = db.attendance_logs.aggregate(attendance_pipeline(query, page, page_size), hint=index)
    return {"items": [attendance_response(record) for record in records], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateString, body: RegularizeIn):
    employee = find_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "Attendance record not found")
    final = {**DEFAULTS, **record}
    supplied = body.model_dump(exclude_unset=True)
    if body.status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied.keys()):
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
    if "status" in supplied:
        final["status"] = body.status
    for field in ("punch_in", "punch_out"):
        if field in supplied:
            final[field] = instant(supplied[field])
        elif final[field] is not None:
            final[field] = final[field].replace(microsecond=0)
    if final["status"] not in PRESENCE:
        if {"punch_in", "punch_out"} & supplied.keys():
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        final.update(punch_in=None, punch_out=None)
    final.update(derived_fields(final, employee))
    changes = {
        field: {"from": record.get(field, DEFAULTS.get(field)), "to": final[field]}
        for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
        if record.get(field, DEFAULTS.get(field)) != final[field]
    }
    if not changes:
        raise HTTPException(422, "Correction changes nothing")
    history = {"at": instant(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(record),
        {"$set": {field: change["to"] for field, change in changes.items()}, "$push": {"history": history}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Attendance record changed; retry with current values")
    return attendance_response(updated)


def month_bounds(month: str):
    year, month_number = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, month_number)[1]:02d}"


def mongo_date(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d"}}


def mongo_working_day(value):
    return {"$in": [{"$dayOfWeek": value}, [2, 3, 4, 5, 6]]}


def mongo_round(value, places=2):
    factor = 10 ** places
    return {"$let": {
        "vars": {"scaled": {"$multiply": [{"$toDecimal": value}, factor]}},
        "in": {"$toDouble": {"$divide": [
            {"$cond": [
                {"$gte": ["$$scaled", 0]},
                {"$floor": {"$add": ["$$scaled", Decimal128("0.5")]}},
                {"$ceil": {"$subtract": ["$$scaled", Decimal128("0.5")]}},
            ]}, factor,
        ]}},
    }}


def presence_weight(prefix=""):
    return {"$cond": [
        {"$in": [f"${prefix}status", list(PRESENCE)]},
        {"$cond": [{"$ifNull": [f"${prefix}half_day", False]}, 0.5, 1]}, 0,
    ]}


def attendance_stats_group():
    has_hours = {"$and": [
        {"$in": ["$status", list(PRESENCE)]},
        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
    ]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [mongo_working_day(mongo_date("$date")), presence_weight(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "work_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
        "work_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def monthly_logs_lookup(first_day, last_day):
    return {"$lookup": {
        "from": "attendance_logs", "let": {"employee": "$emp_code"},
        "pipeline": [
            {"$match": {"$expr": {"$eq": ["$emp_code", "$$employee"]}, "date": {"$gte": first_day, "$lte": last_day}}},
            attendance_stats_group(),
        ],
        "as": "stats",
    }}


def working_days_expression(first_day, last_day):
    return {"$let": {
        "vars": {
            "start": mongo_date({"$max": [first_day, "$joined_on"]}),
            "end": {"$dateAdd": {"startDate": mongo_date(last_day), "unit": "day", "amount": 1}},
        },
        "in": {"$size": {"$filter": {
            "input": {"$range": [0, {"$max": [0, {"$dateDiff": {"startDate": "$$start", "endDate": "$$end", "unit": "day"}}]}]},
            "as": "offset",
            "cond": mongo_working_day({"$dateAdd": {"startDate": "$$start", "unit": "day", "amount": "$$offset"}}),
        }}},
    }}


def employee_monthly_pipeline(emp_code, month):
    first_day, last_day = month_bounds(month)
    fields = ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    return [
        {"$match": {"emp_code": emp_code}},
        monthly_logs_lookup(first_day, last_day),
        {"$set": {"stats": {"$first": "$stats"}}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month},
            "working_days": working_days_expression(first_day, last_day),
            **{field: {"$ifNull": [f"$stats.{field}", 0]} for field in fields},
        }},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            mongo_round({"$divide": [{"$multiply": [{"$toDecimal": "$present_days"}, 100]}, "$working_days"]}),
            None,
        ]}}},
    ]


def department_summary_pipeline(month, department):
    first_day, last_day = month_bounds(month)
    query = {"joined_on": {"$lte": last_day}}
    if department is not None:
        query["department"] = department
    sums = {
        "present_days": "present_days", "late_count": "late_count", "total_late_minutes": "total_late_minutes",
        "leave_count": "leave_days", "on_duty_count": "on_duty_count", "work_sum": "work_sum", "work_count": "work_count",
    }
    return [
        {"$match": query},
        monthly_logs_lookup(first_day, last_day),
        {"$set": {"stats": {"$first": "$stats"}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            **{output: {"$sum": {"$ifNull": [f"$stats.{source}", 0]}} for output, source in sums.items()},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            **{field: 1 for field in sums if field not in ("work_sum", "work_count")},
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_count", 0]}, mongo_round({"$divide": ["$work_sum", "$work_count"]}), None,
            ]},
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month, department, limit):
    first_day, last_day = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
            "total_late_minutes": 1, "late_count": 1, "rank": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def department_trend_pipeline(department, date_from, date_to):
    days = (calendar_date.fromisoformat(date_to) - calendar_date.fromisoformat(date_from)).days + 1
    if not 1 <= days <= 92:
        raise HTTPException(422, "Range must contain between 1 and 92 calendar days")
    return [
        {"$match": {"department": department}},
        {"$facet": {
            "joiners": [{"$group": {"_id": "$joined_on", "count": {"$sum": 1}}}],
            "daily": [
                {"$lookup": {
                    "from": "attendance_logs", "let": {"employee": "$emp_code"},
                    "pipeline": [
                        {"$match": {
                            "$expr": {"$eq": ["$emp_code", "$$employee"]},
                            "date": {"$gte": date_from, "$lte": date_to},
                        }},
                        {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                    ], "as": "attendance",
                }},
                {"$unwind": "$attendance"},
                {"$group": {
                    "_id": "$attendance.date",
                    "present_count": {"$sum": presence_weight("attendance.")},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
        }},
        {"$match": {"joiners.0": {"$exists": True}}},
        {"$set": {"offset": {"$range": [0, days]}}},
        {"$unwind": "$offset"},
        {"$set": {"day": {"$dateAdd": {"startDate": mongo_date(date_from), "unit": "day", "amount": "$offset"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}}},
        {"$set": {
            "stats": {"$first": {"$filter": {"input": "$daily", "as": "entry", "cond": {"$eq": ["$$entry._id", "$date"]}}}},
            "headcount": {"$sum": {"$map": {
                "input": "$joiners", "as": "entry",
                "in": {"$cond": [{"$lte": ["$$entry._id", "$date"]}, "$$entry.count", 0]},
            }}},
        }},
        {"$project": {
            "date": 1, "is_working_day": mongo_working_day("$day"),
            "headcount": 1,
            "present_count": {"$ifNull": ["$stats.present_count", 0]},
            "late_count": {"$ifNull": ["$stats.late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_round({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": mongo_round("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]


EndpointName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def main_operation(
    endpoint, *, emp_code=None, month=None, department=None, limit=10,
    date_from=None, date_to=None, status=None, page=1, page_size=20, trend_from=None, trend_to=None,
):
    required = {
        "employee_monthly": {"emp_code": emp_code, "month": month},
        "department_summary": {"month": month},
        "late_leaderboard": {"month": month},
        "department_trend": {"department": department, "from": trend_from, "to": trend_to},
    }
    missing = [name for name, value in required.get(endpoint, {}).items() if value is None]
    if missing:
        raise HTTPException(422, f"Missing required parameters: {', '.join(missing)}")
    if endpoint == "attendance_list":
        query, index = attendance_query(emp_code, date_from, date_to, status)
        return "attendance_logs", attendance_pipeline(query, page, page_size), index
    if endpoint == "employee_monthly":
        return "employees", employee_monthly_pipeline(emp_code, month), "employee_code_unique"
    if endpoint == "department_summary":
        index = "employee_department_joined" if department is not None else "employee_joined_department"
        return "employees", department_summary_pipeline(month, department), index
    if endpoint == "late_leaderboard":
        return "attendance_logs", late_leaderboard_pipeline(month, department, limit), "attendance_date_employee"
    if endpoint == "department_trend":
        return "employees", department_trend_pipeline(department, trend_from, trend_to), "employee_department_joined"
    raise HTTPException(422, "Unknown endpoint")


def aggregate_operation(endpoint, **parameters):
    collection, pipeline, index = main_operation(endpoint, **parameters)
    return list(db[collection].aggregate(pipeline, hint=index, allowDiskUse=True))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    results = aggregate_operation("employee_monthly", emp_code=emp_code, month=month)
    if not results:
        raise HTTPException(404, "Employee not found")
    return results[0]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": aggregate_operation("department_summary", month=month, department=department)}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, limit: RankLimit = 10, department: str | None = None):
    return {"month": month, "items": aggregate_operation("late_leaderboard", month=month, department=department, limit=limit)}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[DateString, Query(alias="from")],
    date_to: Annotated[DateString, Query(alias="to")],
):
    results = aggregate_operation("department_trend", department=department, trend_from=date_from, trend_to=date_to)
    if not results:
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": results}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EndpointName,
    emp_code: str | None = None,
    month: MonthString | None = None,
    department: str | None = None,
    limit: RankLimit = 10,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    trend_from: Annotated[DateString | None, Query(alias="from")] = None,
    trend_to: Annotated[DateString | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    collection, pipeline, index = main_operation(
        endpoint, emp_code=emp_code, month=month, department=department, limit=limit,
        date_from=date_from, date_to=date_to, status=status, page=page, page_size=page_size,
        trend_from=trend_from, trend_to=trend_to,
    )
    explanation = db.command("explain", {
        "aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": index, "allowDiskUse": True,
    }, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explanation))}
