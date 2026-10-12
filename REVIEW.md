# Starter Review

Locations below identify functions in the original starter. Regression coverage is in [tests/test_main.py](tests/test_main.py).

| # | Where | What was wrong | Reproduction or symptom | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floored minutes before testing the grace boundary. | A 09:40:01 punch for 09:30 returned 0 instead of 10. | Compare elapsed seconds against 600, then floor minutes. |
| 2 | `compute_late_minutes` | Bound shift start to the punch's calendar day without accounting for overnight attendance dates. | A 02:00 punch for a 22:00 shift should be 240 minutes late for the preceding day. | Use the resolved attendance date and an IST shift-start instant. |
| 3 | `compute_work_hours` | Used binary floating-point and banker's rounding. | 3,618 seconds returned 1.00 instead of 1.01 hours. | Divide using `Decimal`, then `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Counted overtime below the 30-minute minimum. | 18:59:59 after an 18:30 shift end returned 29 rather than 0. | Apply the threshold after flooring elapsed minutes. |
| 5 | `compute_overtime` | Always placed shift end on the attendance date. | A 22:00-06:00 shift ending next morning produced excessive overtime. | Advance shift end one day for overnight shifts. |
| 6 | Datetime helpers / MongoClient | Mixed naive local punches and timezone-aware shift boundaries; PyMongo defaulted to naive results. | Overtime subtraction could raise `TypeError`; behavior depended on host timezone. | Use UTC-aware instants and `tz_aware=True`, converting to IST for calendar rules. |
| 7 | `EmployeeIn` | Did not validate employee codes, lengths, or email format. | Empty names, malformed codes, and invalid emails were accepted. | Add the contract's patterns and length bounds. |
| 8 | `EmployeeIn` | Did not validate real dates or shift-time syntax. | `2026-02-30` or `25:90` was accepted and could later crash calculations. | Validate canonical calendar strings and actual dates. |
| 9 | `EmployeeIn` | Allowed equal shift endpoints. | Creating an employee with 09:30-09:30 succeeded. | Reject equal shift times in model validation. |
| 10 | `PunchInIn` | Coerced timestamp types and imposed no epoch bounds. | Seconds, numeric strings, integral floats, booleans, null, or excessive values were accepted. | Strict bounded integer validation; only omission defaults to now. |
| 11 | `PunchInIn` | Accepted arbitrary statuses. | Punch-in with `LEAVE`, `ABSENT`, or an unknown value succeeded. | Restrict punch-in to the three presence statuses. |
| 12 | `health` | Returned healthy without checking MongoDB. | Stopping MongoDB still produced 200. | Ping MongoDB; report 503 when unavailable. |
| 13 | Startup | Created no indexes. | Seeded databases had only `_id`; duplicate races and collection scans were possible. | Create eight named indexes idempotently during lifespan startup. |
| 14 | `create_employee` | Relied on check-then-insert for uniqueness. | Concurrent duplicate creates could both succeed. | Unique database index, insert directly, map duplicate-key errors to 409. |
| 15 | `create_employee` | Used a naive local creation timestamp and returned a datetime directly. | `created_at` was an ISO string instead of integer epoch milliseconds. | Server-generated whole-second UTC time and explicit boundary serialization. |
| 16 | `list_employees` | Used `page * page_size` as the skip offset. | Page 1 omitted the first page of employees. | Use `(page - 1) * page_size`. |
| 17 | `list_employees` | Counted the entire collection despite filters. | Department-filtered `total` included other departments. | Use the same filter for count and fetch. |
| 18 | `list_employees` | Did not sort by employee code. | Insertion order changed pagination results. | Database-side ascending employee-code sort. |
| 19 | Both list endpoints | Did not enforce pagination bounds. | Page 0, negative sizes, or sizes above 100 were accepted. | Query validation returns 422. |
| 20 | Filter construction | Used truthiness instead of checking whether a filter was supplied. | `department=` silently listed every department. | Apply exact filters whenever a value is not `None`. |
| 21 | `punch_in` | Dereferenced an unknown employee. | An unrecognized employee caused a 500. | Return 404 before using the employee record. |
| 22 | `punch_in` | Interpreted epochs in the server's local timezone and used that calendar date. | A punch near IST midnight was assigned to the wrong day on a UTC host. | Parse UTC and resolve the attendance date in IST, including the overnight exception. |
| 23 | `punch_in` / helpers | Did not truncate milliseconds. | `09:40:00.900` was stored and returned with a fractional second. | Truncate instants before computations, storage, and responses. |
| 24 | `punch_in` | Used check-then-insert for daily uniqueness. | Simultaneous punches could produce duplicate daily records. | Enforce the natural key with a unique index and return 409 for losers. |
| 25 | Attendance responses | Added `id` from MongoDB and serialized datetime objects directly. | Responses exposed an internal identifier and returned ISO timestamps. | Return only contract fields and convert all instants, including nested history values, to epoch milliseconds. |
| 26 | `list_attendance` | Loaded and sorted the complete matching collection in Python. | Memory and latency grew with 100,000 records even for page size 20. | Indexed database sort, skip, and limit; independent filtered count. |
| 27 | `list_attendance` | Sorted only by date. | Records on the same date were not ordered by employee code. | Sort by date descending, then employee code ascending. |
| 28 | `list_attendance` | Accepted invalid dates, inverted ranges, and unknown statuses. | Invalid filter inputs returned data or empty pages rather than 422. | Validate calendar strings, date order, and status values. |
| 29 | `list_attendance` | Returned seeded records without filling optional stored fields. | Missing `history`, `half_day`, or minute fields violated the required response shape. | Supply the documented defaults at the response boundary. |

## Reviewed and Kept

- `load_dotenv()` already leaves real environment variables in control; no override is enabled.
- Customer-supplied `emp_code` is the correct public identity. MongoDB `_id` remains an internal update key only.
- String calendar dates are intentional and sort chronologically when validated as `YYYY-MM-DD`.
- Synchronous PyMongo is appropriate with synchronous FastAPI handlers, which execute in the thread pool. Startup index creation also runs off the event loop.
- Ignoring extra body fields is intentional for read-only derived inputs. They never overwrite computed values.
- Null work hours, zero overtime, false half-day, and an empty history are correct initial punch-in values.
- Punch-in and punch-out must not append history. Only successful manual corrections append an entry.
- Missing weekdays are not fabricated as `ABSENT` documents. Trend gap rows are aggregate output, not stored attendance.

## Verification

The initial three calculation tests reproduced four failing assertions in the starter. The completed suite has 22 tests covering validation, writes, concurrent requests, corrections, analytics, and explain parity. All passed on Python 3.13 and MongoDB 7.0.14, including the opt-in 100,000-record test. Index creation took about 0.72 seconds locally; all checked plans contained `IXSCAN`, no `COLLSCAN`, and zero lookup collection scans. These are local synthetic measurements, not a claim about hidden-grader results or production latency.
