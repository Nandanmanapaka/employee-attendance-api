# Employee Attendance & Analytics API

All 12 API operations are implemented in [app/main.py](app/main.py). Requires Python 3.11+ and a running MongoDB 6.0+ server. The HTTP contract is [openapi.yaml](openapi.yaml); storage conventions are in [DATA_MODEL.md](DATA_MODEL.md).

## Run

Run these commands from this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

On Linux/macOS, activate with `source .venv/bin/activate` and set configuration using `export MONGO_URI=... MONGO_DB=...`. A local `.env` is optional; existing environment variables take priority. All indexes are created automatically at startup. MongoDB must be reachable; `/health` pings it. Interactive API documentation is at http://localhost:8000/docs.

Optional: run `python sample_seed.py` before starting the app. **This replaces both collections in the configured database**, so use a dedicated sample database.

## Test

```powershell
python -m unittest discover -s tests -v
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
python -m unittest discover -s tests -v
$env:RUN_SCALE_TESTS = "1"
python -m unittest discover -s tests -v
```

Without `TEST_MONGO_URI`, database tests are skipped. Integration tests create and delete a uniquely named test database; the connection needs permission to do so. The opt-in scale test inserts 100,000 records and checks startup index creation, real endpoints, and execution plans. It is intentionally slower than the regular suite. All 22 tests passed locally on Python 3.13 / MongoDB 7.0.14, including the scale test. `httpx` is included for the HTTP test client.

## Contract Interpretations

- Optional request timestamps may be omitted, but explicit null is invalid because request schemas are not nullable. Corrections clear punches by switching to `ABSENT` or `LEAVE`.
- Punch-out selects the latest punch-in at or before the supplied instant, even if that record is already closed. No eligible record is 404; equal timestamps or durations exceeding 24 hours are 422; a closed selected record is 409.
- Derived inputs are ignored. A correction whose final values are unchanged returns 422. Concurrent changes use an atomic snapshot comparison; the loser can retry after a 409.
- Trend uses MongoDB `$range`/`$unwind` for complete calendar generation, including entirely empty periods. Its moving average uses the reported daily rates and only rows inside the requested range.
- Analytics consume stored derived values. Employee-based summaries include zero-log employees and do not count orphaned logs as department members.

## Submission

Include [app/main.py](app/main.py), [requirements.txt](requirements.txt), [REVIEW.md](REVIEW.md), [DECISIONS.md](DECISIONS.md), this README, and optionally the tests. Make this directory the public repository root. Do not commit `.env`, secrets, virtual environments, Dockerfiles, or generated data dumps. Review the implementation and decision notes before the live walkthrough.
