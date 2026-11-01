# Activity Monitor history import: findings and design

> **Updated architecture:** the earlier notes below describe the first
> namespace-local implementation and its initial history import. The current
> cross-namespace archive is in the main database and associates records by
> WorldDB rider ID while retaining their source namespace and record key. See
> `activity_monitor_global_history_runbook.md` for installation and operations.
> The earlier FirstCycling-ID/birth-date matching discussion applies only to
> that original one-time migration; it is not the cross-namespace link key.

> **Current status (2026-10-30):** the reviewed local Activity Monitor history
> has been imported into the draft development namespace
> `officialrelease-pcs-2026-local-test-2026-men`. The older notes below record
> the investigation as it progressed; statements that no import/schema change
> had happened describe earlier checkpoints and are superseded by this status.

## Confirmed identity match

The local Activity Monitor and the selected PCM Manager namespace both use the
FirstCycling ID stored in the CDB's `DYN_cyclist.value_f_current_ability`.
The Activity Monitor also stores the rider's birth date from the CDB. The
read-only preview matched on the pair `(FirstCycling ID, birth date)`, required
the ID to be unique on both sides, and rejected held or invalid source
identities. It was rerun against the active monitor database and local PCM
Manager development namespace on 2026-10-30.

Preview result for namespace `officialrelease-pcs-2026-local-test-2026-men`:

- 8,042 current monitor riders have a unique FirstCycling ID match and equal
  birth dates in the namespace CDB roster (8,042 corroborated candidates, with
  zero identity or birth-date conflicts).
- 1 monitor rider has no match in the current namespace roster. Retain this
  rider's history as an unlinked historical identity; do not discard it or
  attach it by the old numeric WorldDB cyclist ID.
- The source currently has no identity holds and every stored rider identity
  generation is `0`.
- The PCM Manager main FirstCycling profile table is empty in this local
  development setup. It is not needed for this match because the source and
  target CDB rosters provide the same stable ID and birth date.

The preview read the source SQLite database in read-only mode and opened
PostgreSQL read-only transactions against namespace
`officialrelease-pcs-2026-local-test-2026-men` (status `draft`). It sent only
FirstCycling IDs, birth dates, and identity-quality fields to the local preview
process; it printed no rider names, URLs, or profile details. No Activity
Monitor history has been imported.

## Shared data ownership

Activity Monitor data belongs to the selected namespace. It must not be scoped
to `user_id`. A user may initiate a check or review, and that action should be
audited with the user's account, but every authorized user of that namespace
must see the same roster, results, alerts, and check history.

The CDB roster remains the source for current game data. Activity Monitor
records are separate namespace data; importing or updating monitor records
must never edit `cdb_cyclist` or write to the original CDB file.

## Identity and history rules

- Use a namespace-scoped monitor identity keyed by FirstCycling ID, with the
  CDB birth date required as corroborating evidence during the initial import.
- Keep `worlddb_rider_id` and `identity_generation` as source provenance. Do
  not use the numeric CDB rider ID as the cross-release identity key.
- Preserve the one unmatched rider and its history without a current CDB link.
- Reject duplicate IDs, invalid IDs, unresolved identity holds, or birth-date
  conflicts for automatic linking; place them in a review queue.
- Preserve dated results, checks, schedules, profile history, eligibility
  changes, roster snapshots, and review history. Avoid importing machine-local
  paths and cache file paths as shared data.
- Make the import repeatable using a source database fingerprint and source
  row keys. Record who initiated the import in the application audit trail.

## Source history available

The active installed monitor database at
`%LOCALAPPDATA%/WorldDB Activity Monitor/worlddb_activity.sqlite` was found
and inspected in read-only mode on 2026-10-30. It reports:

| History | Rows |
| --- | ---: |
| Race results | 116,318 |
| Source checks | 3,404 |
| FirstCycling download attempts | 883 |
| Schedule snapshots | 11,163 |
| Rider profile history | 1,973 |
| Eligibility changes | 8,650 |
| Roster snapshots | 40,213 |

The active database schema fingerprint is
`dc163b01b26c6d41e21052f79fffa8c876adebb3a868e4fc3cb4335bca7e8059`.
The project checkout snapshot previously inspected has a different fingerprint
(`f25f8f...c1c9e30`) and much smaller counts, so it is not the import source.
The active database also contains `activity_source_cycle_history` (4,805 rows)
and `activity_source_cycle_pending` (0 rows). Source code confirms the history
table records completed FC/PCS cycles and the latest participation date; retain
it in the legacy holding area until the online run model can represent its
meaning. The pending table is only an in-progress work queue and currently has
no rows, so rebuild that queue for the online worker instead of migrating it.

The installed monitor database was only read. No row values were displayed,
and no data was changed.

## Full-history import dry run (2026-10-30)

The extended read-only preview was run against the active database and the same
draft namespace. Its table-level plan is:

| Planned disposition | Rows | Notes |
| --- | ---: | --- |
| Mapped to shared Activity Monitor tables | 193,058 | Candidate table mappings; field validation and deduplication remain |
| Preserved in the legacy holding area | 4,805 | Completed source-cycle history until its meaning is represented natively |
| Review required | 2 | `monitor_metadata` keys: eligibility reference date and monitor version |
| Excluded local/runtime state | 1 | Collector state; the pending online-check queue is empty |

The mapped-row count is a sum across tables, not a count of unique riders or a
guarantee that every row is importable. The dry run identifies local-only
columns to omit: CDB import source path, source-check file path, FirstCycling
cache path, and collector process ID. Source values, names, and URLs are not
printed. The metadata keys are not copied as raw records: the eligibility
reference date needs a deliberate migration rule, while the monitor-version
marker is app-local bookkeeping.

No records have been imported. Before an importer is implemented, the next
review should define row-level validation and duplicate rules for each mapped
table, including orphaned source rider references and preservation of the one
unmatched historical rider.

## Read-only row-shape validation (2026-10-30)

`activity_monitor_import_preview.py --row-validation-only --monitor-db <path>`
now checks the active local monitor database using a read-only SQLite
connection. It prints aggregate counts and key column names only; it does not
print rider names, URLs, notes, or record values.

First-pass validation covered the 19 populated tables planned for the shared
Activity Monitor or its legacy holding table: **197,863 rows**. It found no
missing required fields under the reviewed rules, missing source keys,
duplicate source-key groups, invalid date values, or references to unknown
source riders. Four rows in `source_checks` have no rider reference. Preserve
those as unlinked history; do not guess their rider identity or discard them.
All 8,043 source rider identities also have a unique `(worlddb_rider_id,
identity_generation)` key; the separate CDB match preview previously confirmed
8,042 identity-and-birth-date matches and one unmatched historical identity.

This is a shape and referential preview, not an import approval. It does not yet
validate JSON payload contents, every enum/status against the target domain,
field-length limits, all source-to-target transforms, or cross-table event
relationships. `monitor_metadata` remains a deliberate review item, collector
state remains excluded as machine-local, and the empty pending queue will be
rebuilt by the online worker.

The target schema also needed source-row keys to make retries safe. Nullable,
unique `source_record_key` columns have been added to the run, source-check,
profile-link, review-event, rider-schedule, eligibility-event, task, task-event,
and identity-alert models. The importer should use a table-prefixed source key
(for example, `source_checks:<id>`) so integer IDs from different source tables
cannot collide. Existing natural keys remain in place for riders, CDB imports,
roster snapshots, race results, and current reviews. These are model changes
only: no namespace schema was installed or altered.

## Source-code review and transformed-field validation (2026-10-30)

The source collector (`pcs_collector.py`) confirms that the long
`pcs_profile_state.check_result` values are successful activity summaries in
the form `confirmed_results_found; latest <date> — <race>`. The collector also
writes a separate `message` with the complete check explanation. The shared
model now has a `check_result_detail` text field so the import can store the
canonical status in the 40-character `check_result` field and retain the full
source value as detail. The read-only validation of the active monitor found
2,359 profile-state rows; 1,944 contain this appended detail. After splitting
at the source-defined separator, the longest status is 38 characters and no
status exceeds the target limit. The longest retained detail is 94 characters.
This avoids truncation and avoids treating race-specific detail as hundreds of
different statuses.

The source also establishes the logic behind those values: profile checks
record identity mismatch/unverified states, missing or unrecognized result
tables, profile-not-found/network outcomes, and successful checks with or
without confirmed results. The shared models use strings rather than database
enums. Import status fields verbatim so historical outcomes are not recoded;
the one exception is splitting the appended latest-race detail from the
canonical PCS check-result status. Any friendlier labels belong in the UI and
must not replace stored source statuses.

The active-database recheck found all mapped direct text fields within their
target limits, zero invalid JSON syntax, and zero unresolved cross-table
references across the reviewed relationships. A shape-only read confirmed that
all populated JSON payloads are objects: 5 import summaries, 40,213 roster
snapshots, and 11,163 schedule snapshots. The source writer defines roster
snapshots as serialized rider records and schedule snapshots as a fixed set of
five scheduling fields. The other JSON-bearing source tables are empty in this
database; their future shape must be validated when such rows exist. Payload
values remain opaque and should be preserved without exposing them in logs.

The field-by-field comparison also found two durable source fields that were
missing from the first target model: `riders.eligibility_reason` and
`eligibility_events.legacy_origin`. Both now have dedicated target columns;
neither is folded into a different field. The model also now defines
`activity_monitor_history_imports`, an audit ledger with a unique source
fingerprint, schema fingerprint, initiating user, timestamps, aggregate row
counts, unlinked-row count, and error summary. This table is created only by
the explicit manual Activity Monitor schema installer. These are model changes
only; no namespace schema has been installed or altered.

The source-code review now defines a field mapping for each
`pcs_profile_state` row: `url_override` → `override_url`, `override_input` →
`entered_value`, `exported_override` → `exported_override`, `approved_url` →
`approved_profile_path`, `cdb_export_url` → `exported_path`, and `profile_slug`
→ `profile_slug`. `cdb_import_url` is deliberately not imported or used as a
profile candidate; `CONSTANT` in an uploaded CDB is not a source of PCS links.
The existing target fields retain the error, checked URL/result, approved
identity dates, and CDB-link timestamp. The model now has dedicated override
update/error timestamps as well. This treats the source profile-state row as
one durable state record; verified-link history remains separate rows.

The source selection rule is manual override → verified history → generated
candidate. Importing stored state must not promote a generated
candidate to verified/current, and a failed or empty override must not shadow
the next usable link. The generated candidate remains derived data, not a
stored verified link. Per-rider target IDs must be created from the stable
identity mapping before dependent history is imported; the unmatched historical
rider remains unlinked. The import command still needs to implement and dry-run
this resolution rule before any write is enabled.

Four source checks have no rider reference. The target source-check model
allows a null rider link and retains the source rider ID separately, so these
records can be imported as unlinked history without guessing or dropping them.
No source rows were displayed or modified, no schema was installed, and no
history was imported.

## Implementation sequence

1. Add namespace-scoped Activity Monitor tables and an explicit manual schema
   installation. Do not run schema creation automatically at application
   startup.
2. Table-level inventory, stable identity matching, row-shape validation,
   populated JSON top-level shapes, direct text limits, transformed PCS
   check-result length, cross-table reference counts, and handling of the four
   unlinked source checks are complete. Preserve status strings verbatim,
   transform only the PCS check-result detail as documented, and copy reviewed
   object payloads without changing their structure. Keep import writes blocked
   until the PCS profile-link provenance and per-rider target-ID rules above
   are explicit.
3. Finalize the PCS profile-link mapping, then add a transactional, idempotent
   import command with an audit entry and rollback/export plan. Keep it separate
   from normal app startup and make dry-run the default.
4. After review, import the history into the local development namespace, then
   build the Activity Monitor pages on the shared namespace data.

No schema migration or history import has been run by this preview.

## PCM Manager integration findings (2026-10-30)

- The product goal and user journey are recorded in
  `activity_monitor_goal_and_user_route.md` and should guide the screen and
  worker implementation.
- `CDBCyclist` retains a legacy `pcs_rider_path` field, but cyclist imports now
  deliberately omit it. PCS links are managed by Activity Monitor. Release
  generation writes only green or orange profile slugs to the mapped export
  field `DYN_cyclist.CONSTANT`, matched through a unique FirstCycling ID;
  unmatched rows and existing `CONSTANT` values are left untouched.
- The Activity Monitor page currently only reports the selected namespace's
  CDB rider count. Shared monitor history and its checks are not yet wired into
  PCM Manager.
- The source-project database at `data/worlddb_activity.sqlite` was also
  inspected read-only. Its counts are much smaller than the active monitor
  database (2,677 race results and 49 source checks versus 116,318 and 3,404).
  Treat it as an incomplete development snapshot, not the import source.
- An initial shared namespace schema is now defined in
  `webapp/models/ns/activity_monitor.py`. It separates monitor metadata from
  `NamespaceBase`, so normal app startup and namespace creation do not create
  these tables. The explicit command `flask namespace install-activity-monitor
  <namespace-slug>` creates missing tables only; it does not import records or
  modify existing tables. It was explicitly run for the local draft namespace
  before importing the reviewed history.
- The first schema covers stable monitor identities and roster snapshots, CDB
  syncs, manual/automated run records, source checks and race evidence, PCS
  links with provenance, shared reviews and their audit history, schedule and
  eligibility history, resumable worker tasks, identity problems, and a
  de-duplicated JSON holding table for durable legacy records not yet mapped to
  their final home.
- Durable history to account for includes race results, per-source checks,
  FirstCycling download/run history, PCS profile state and verified-link
  history, roster/CDB import snapshots, identity alerts, manual reviews and
  review history, eligibility changes, research findings/tasks/events,
  scheduler runs, and Sheets export/import receipts and baselines.
- Local paths and machine-only runtime state must not become shared namespace
  data. In particular, omit or sanitize CDB/cache/source file paths and avoid
  copying collector locks, local API credentials, scheduler configuration, or
  machine-specific cooldown state. Keep durable request outcomes and audit
  history, and associate future online actions with PCM Manager user IDs.
- The initial import must also account for backward-compatibility tables with
  the `legacy_` prefix. Their contents should be retained or explicitly
  reconciled with the newer review/task tables, never silently discarded.
- `activity_monitor_import_preview.py --inventory-only --monitor-db <path>` now
  provides a read-only table-count and destination inventory without connecting
  to PostgreSQL. Unknown source tables are marked for review instead of being
  silently excluded. The full identity-match preview remains read-only too.
- The active monitor inventory and identity match have now both been confirmed
  against the local draft namespace. The only unmatched identity is retained as
  a separate monitor identity; the full-history migration is documented below.

## Completed history import (2026-10-30)

The Activity Monitor schema was explicitly installed in the draft namespace,
then the history importer was run against a read-only copy of the active local
monitor database. A PostgreSQL custom-format backup was taken before schema
installation and import at
`C:\Users\arnou\Documents\Codex\2026-10-28\we-gaan-de-bestaande-worlddb-app\outputs\pcm_manager_namespace_before_activity_import.dump`.
The original SQLite source was not modified. The temporary copy in the web
container was removed after verification.

The import completed as one transaction and its audit ledger records one
completed source import. It wrote **197,863** rows across the mapped history
tables, including 8,043 rider identities, 40,213 roster snapshots, 116,318 race
results, 4,287 source checks, 4,332 PCS profile-link/history records, 8,650
eligibility events, 11,163 schedule snapshots, and 4,805 legacy source-cycle
records. The one historical rider without a matching current CDB identity was
preserved as a monitor rider; four source checks without a rider reference
remain deliberately unlinked. Imported race results have no null rider links.

The 883 FirstCycling downloads, 47 collector runs, five CDB import snapshots,
manual-review/task/alert history (empty in the source), and other mapped
records are included in the total. The local collector state, local metadata,
and pending work queue were excluded as documented above. A second dry run with
the same source fingerprint reported that it had already been imported and
made no changes. Aggregate post-import counts matched the import output.

### Preserving research findings omitted by the first import

The large imported history is present and remains useful: it contains race
results, individual source checks, completed FC/PCS cycles, and planning
snapshots. I checked the installed V1.3 database again read-only. It currently
has 116,318 race results, 3,404 source checks, 4,827 completed source cycles,
11,172 schedule snapshots, and **zero** rows in `research_findings`. There is
no `legacy_activity_research_checks` table in that database. The schedule
snapshots preserve the latest participation date, next research date, deadline,
and reason, but their `completed_milestones` values are blank and they contain
no `last_research_date`. So the historical activity/check work is preserved;
separate proof that a milestone received full research is not in this source
database. The importer stores any future `research_findings` rows as
`activity_monitor_legacy_records` with record type `research_findings`, omitting
the local-only `import_file` path.

The source has continued changing since the original import: a read-only key
comparison found 22 additional completed source cycles and 9 additional
schedule snapshots. A separate
`flask namespace backfill-activity-monitor-history-tail <namespace-slug>
--source <path>` command previews missing cycles, snapshots, and findings by
default. It requires the source rider identities to exactly match the imported
namespace roster; writes happen only with `--apply`, and only absent source
keys are appended. The read-only preview matched all source rider identities,
then the backfill appended those 22 cycles and 9 snapshots (zero findings) to
the local draft namespace. It overwrote no rider or race-result records.

The Run Checks page shows riders due according to their latest imported
Arie Tool schedule snapshot, with a manual selection and preview path. It now
also compares that snapshot's activity date with newer confirmed, identity-
verified race results already stored online. If a newer race exists, the due
date is recalculated from that race using the Arie Tool's 100/150/200/250/300/
400/500-day milestones, five-day deadlines, winter handling for the early
milestones, and team 119's 345-day review rule. Because a newer race changes
the milestone basis, old milestone completions are not carried forward without
matching full-research evidence. PCS results count as activity only when their
successful road-profile check is recorded. This remains a read-only preview;
it neither saves a new schedule nor starts a source check. Existing imported
dates remain the source when no newer confirmed activity is present. The
preview also applies the Arie Tool's 20-day minimum between research attempts,
20-day general source-cycle retry, and 30-day free-agent source-cycle retry.
The rider details page now has an append-only research log. A complete entry
requires identity confirmation, at least one source link, and notes about the
search and its limits. Each entry records the latest confirmed activity date
it covers; only a complete entry for the current activity date can close
milestones. This uses the existing legacy-record table, so no namespace schema
change is needed. Existing race results, checks, source cycles, and saved plan
snapshots are not rewritten.

The history import is complete. The next development step is to carry these
research records and the same due-date rules into the shared check queue, so
manual and scheduled work both honor the recorded research coverage and retry
dates.
