# Store PCS evidence when a rider has no FirstCycling ID

The Activity Monitor result table originally required every result row to carry
a FirstCycling ID. PCS can verify a rider independently, so the ID is not
available for some valid PCS-only checks. The application code now supports a
missing ID and builds an idempotency key from the namespace rider identity.

## Manual update

This change targets **one Namespace database**, not the shared `main` database.
Take a database backup first, connect directly to the intended Namespace
database, and run
[`activity_monitor_allow_pcs_without_fc_id.sql`](activity_monitor_allow_pcs_without_fc_id.sql).
The script only removes the `NOT NULL` constraint; it does not alter existing
rows. Repeat for each Namespace database that will use the updated Activity
Monitor code.

The code remains compatible with a Namespace that has not received this update,
but PCS result evidence for a rider without an FC ID cannot be saved there.

## Rollback

Rollback is safe only if no rows have been written with a missing FC ID. Check
first:

```sql
SELECT COUNT(*)
FROM activity_monitor_results
WHERE first_cycling_id IS NULL;
```

If the count is zero, run:

```sql
ALTER TABLE activity_monitor_results
    ALTER COLUMN first_cycling_id SET NOT NULL;
```

If the count is not zero, keep the nullable column. Restoring `NOT NULL` would
require deciding how to preserve those PCS evidence rows; do not delete them to
make the constraint pass.
