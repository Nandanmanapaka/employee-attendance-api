# Decisions

1. **Indexes.** Unique employee-code and employee/date indexes enforce identity and one attendance record per day. Employee indexes on department/code, joined-date/department/code, and department/joined-date/code support sorted lists and headcount queries. Attendance indexes on date/code and status/date/code support filtered, sorted pages; employee/punch-time selects the latest eligible punch-in. I rejected a standalone status index: it would not support the required ordering. Explicit hints keep main queries and explain plans aligned.

2. **Punch-in race.** Both requests validate the employee and calculate the attendance date. Both attempt insertion. MongoDB's unique employee/date index allows one insert, returning 201. The other's duplicate-key error becomes 409. No application lock or preliminary duplicate lookup is needed.

3. **Ties.** MongoDB ranks by total lateness before applying the cutoff. Equal totals share a rank, and subsequent ranks skip positions. All rows with rank at most the limit are returned, ordered by employee code within ties.

4. **Headcount.** The summary starts from eligible employees, not logs. Each employee contributes one to headcount even when the attendance lookup is empty.

5. **At 100x.** I would introduce incrementally maintained daily department summaries, including correction-driven updates, to reduce repeated aggregation over historical logs.
