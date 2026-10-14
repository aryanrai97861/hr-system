# Employee Attendance & Analytics API (FastAPI + MongoDB)

Production-grade implementation for the HROne Engineering Assignment. Follows `openapi.yaml` and `DATA_MODEL.md` with complete enforcement of business rules R1–R10, concurrency safety, and MongoDB aggregation pipelines.

## Project Structure

```text
├── app/
│   ├── __init__.py
│   └── main.py              # Single-file FastAPI application containing all endpoints and logic
├── sample_data/             # Sample documents for employees and attendance logs
├── tests/
│   └── test_api.py          # Automated test suite covering contracts, edge cases, and concurrency
├── sample_seed.py           # Seeding script for sample data
├── requirements.txt         # Pinned python dependencies
├── REVIEW.md                # 24 starter-code defects analyzed with reproductions and fixes
├── DECISIONS.md             # Key architectural decisions (indexes, concurrency, ties, headcount, scaling)
└── README.md                # Run instructions and overview
```

## Quick Start

### 1. Environment Setup

```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate or source .venv/Scripts/activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configuration

Set `MONGO_URI` and `MONGO_DB` in your environment or in a local `.env` file (real environment variables take priority):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

*Compatible with MongoDB 6.0, 7.0, and 8.0 (including MongoDB Atlas M0).*

### 3. Seed Sample Data (Optional)

```bash
python sample_seed.py
```

### 4. Run the Server

```bash
uvicorn app.main:app --port 8000 --reload
```

The server automatically initializes all required unique and compound indexes idempotently at startup via the FastAPI lifespan handler.

### 5. Run Automated Tests

```bash
pytest -v tests/test_api.py
```

## Key Architecture & Business Rules Highlights

- **R1 IST Timezone & Overnight Shifts**: All calendar dates (`YYYY-MM-DD`) and shift times (`HH:MM`) operate in IST (UTC+05:30). Cross-midnight overnight shifts (e.g., 22:00–06:00) attribute punch-ins before shift end to the previous calendar day.
- **R2 Strict Late Grace**: Instants are truncated to whole seconds. A punch-in is late only if strictly > 10:00 (600s) after shift start; when late, minutes are calculated from shift start.
- **R3 Overtime**: Counted only when post-shift duration >= 30 minutes. Correctly identifies shift end across calendar dates for overnight shifts.
- **R4 Work Hours**: Computed using exact half-up rounding (`ROUND_HALF_UP`) to 2 decimal places.
- **R5 Half Day**: Flagged when work hours < 4.50; counts as 0.5 present days in aggregations.
- **R7 Working Days**: Calculated strictly for weekdays (Mon–Fri) from the employee's `joined_on` date.
- **R9 Headcount**: Department summary and trend aggregations start from `employees`, guaranteeing employees with zero logs are accurately counted.
- **Competition Ranking**: Late leaderboard uses `$rank` to handle ties (1, 2, 2, 4) with `limit` applied after ranking.
- **Concurrency Guarantees**: Enforced by MongoDB unique indexes and atomic updates (`find_one_and_update`), preventing duplicate punch-ins and punch-out race conditions.
- **Zero COLLSCAN**: All list and analytics queries leverage indexes and return execution statistics via `GET /admin/explain/{endpoint}`.
