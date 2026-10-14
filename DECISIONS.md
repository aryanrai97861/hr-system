# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce business uniqueness and prevent concurrent duplicates. Compound indexes on `attendance_logs.(date, emp_code)` power fast sorted pagination, and `(emp_code, punch_out, punch_in)` optimizes punch-out resolution. On `employees`, `(department, joined_on, emp_code)` speeds department lookups, while `(joined_on, department, emp_code)` guarantees zero-COLLSCAN headcount filtering in department summaries. I rejected an isolated index on `status` due to low selectivity.

2. **Punch-in race.**
Both requests calculate the attendance date and execute `insert_one`. MongoDB's unique compound index on `(emp_code, date)` guarantees atomic mutual exclusion: exactly one insert succeeds with 201 Created, while the competing insert fails with `DuplicateKeyError`, caught and translated to HTTP 409 Conflict.

3. **Ties.**
The pipeline uses MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4). Filtering with `rank <= limit` preserves all tied employees at the boundary (returning more than `limit` rows when ties occur), sorted deterministically by `emp_code` ascending.

4. **Headcount.**
The pipeline begins on `employees` (filtering `joined_on <= end_of_period`), groups by department to establish baseline headcount, and `$lookup`s matching attendance logs. Employees with zero logs still count as 1 towards department headcount.

5. **One thing for 100x data.**
At 100x scale (~10 million records), I would maintain pre-aggregated daily summaries updated incrementally via Change Streams or nightly rollups, avoiding expensive cross-collection multi-stage aggregations on live operational traffic.
