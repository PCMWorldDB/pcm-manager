# Activity Monitor query indexes

Target: each **namespace PostgreSQL database** that has the Activity Monitor
tables. Do not run this against `main` or the shared provisioner database.

The Overview, Riders, and planning screens repeatedly ask for the newest schedule,
source check, result, legacy research record, or open identity alert for each rider.
These composite indexes match those lookups. The Riders table's latest-race sort
uses a covering result index so PostgreSQL can find each rider's newest verified
race without sorting all of their saved results. The SQL uses `CONCURRENTLY`, so run
the statements against the target namespace while not wrapping the file in a
transaction. PostgreSQL can then build the indexes without blocking normal writes.
The result-to-check index also avoids repeatedly scanning all saved results when
Run Checks displays evidence counts for recent source checks.

Operator steps:

1. Identify the PostgreSQL database name for the namespace being optimized.
2. Connect to that namespace database with a PostgreSQL client.
3. Review and run `activity_monitor_query_indexes.sql` there. Run the statements
   as written; do not use a transaction wrapper.
4. Repeat for any other namespace whose Activity Monitor screens need the same
   improvement.

The script is safe to re-run: each index uses `IF NOT EXISTS`. It does not alter
or remove Activity Monitor records.
