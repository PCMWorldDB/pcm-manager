# Shared Activity Monitor history and CDB updates

## Data ownership

- The selected namespace remains the source of truth for its current CDB roster,
  Activity Monitor state, queued work, and other namespace workflows.
- The main database receives a durable history archive. Each archived record is
  scoped by namespace and source record, and is linked to a rider only by the
  stable WorldDB ID. Namespace-local integer IDs are retained inside the
  archived payload for provenance; they are never used for cross-namespace
  matching.
- A CDB update replaces only the five mapped CDB tables
  (`cdb_country`, `cdb_division`, `cdb_region`, `cdb_team`, and `cdb_cyclist`)
  in one namespace-database transaction. Activity Monitor tables, workflow
  tables, and the uploaded SQLite/CDB files are outside that replacement.
- An invalid mapped import rolls back the replacement transaction and leaves
  the previous mapped CDB rows in place. The original uploaded file is never
  modified.

## One-time operator steps

1. Back up the main database and the namespace databases using the deployment's
   normal backup process.
2. Review and apply
   `webapp/tools/activity_monitor_global_history_main.sql` to the **main**
   database. Alternatively, run the explicit installer:

   ```text
   flask namespace install-global-activity-monitor-history
   ```

3. For every namespace with existing Activity Monitor history, preview the
   archive first:

   ```text
   flask namespace archive-activity-monitor-history <namespace-slug>
   ```

4. Review the aggregate table counts, then copy that namespace's history:

   ```text
   flask namespace archive-activity-monitor-history <namespace-slug> --apply
   ```

   The operation is repeatable. It updates the archived payload for the same
   source record and does not create duplicate archive rows.

These commands are manual; neither startup nor normal namespace creation runs
the archive-table installation or migrates existing history automatically.

## Ongoing operation

- Once the main archive table is installed, each completed online Activity
  Monitor rider check synchronizes that rider's durable records from the
  selected namespace into the archive. The namespace's own check remains the
  authoritative result if the shared archive is unavailable; the worker logs
  archive failures for an operator to reconcile with the dry-run/apply command.
- After a successful CDB replacement, the application restores archived records
  for WorldDB IDs already present in that namespace's Activity Monitor roster.
  It remaps rider IDs and source-check references, preserves existing rows, and
  leaves the archive unchanged. The global archive table is not displayed on
  rider pages.
- Recovery supports source checks, race results, rider schedules, profile
  links, review state/events, identity alerts, and eligibility events. Runs,
  task queues/events, scheduler settings, roster snapshots, and generic legacy
  records are not restored. When the Activity Monitor schema has been
  operator-installed, CDB upload synchronizes its roster from `cdb_cyclist`
  using `IDcyclist`/`cdb_cyclist.id` as the WorldDB rider ID, then restores
  matching archived records. A rider returning after being absent starts a
  new identity generation; records from an earlier generation remain archived
  and are not attached to the current generation. Monitor-only rows are marked
  absent from the current CDB.
- Uploading a CDB from the namespace detail page replaces the mapped roster
  tables atomically. The namespace's Activity Monitor history is archived
  before the update when the shared archive is installed and remains in its
  namespace database throughout. New namespaces follow the same mapped-table
  replacement importer on their initially empty database.
- After any archived history source is changed outside normal online checks,
  rerun the namespace archive command with `--apply` to synchronize it.

## Verification and recovery

- Use the dry run before each first archive of a namespace. It reports counts
  by source table and performs no writes.
- The archive does not delete records from namespace databases. If an archive
  attempt fails, fix the cause and rerun `--apply`; the operation is idempotent.
- For a CDB update error, the mapped-table transaction rolls back. The prior
  namespace CDB remains available and the error is recorded in application
  logs. The SQLite source file is retained only after a successful import.
- Do not run the installer, archive `--apply`, or CDB update against production
  or development databases as a test. Use the disposable PostgreSQL setup in
  `AGENTS.md` for regression testing.
