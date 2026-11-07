# CDB

Shared tools for interacting with the Pro Cycling Manager CDB SQLite file.
This integration handles SQLite inspection, release stat updates, and PCM version
field mappings. The one-to-one namespace PostgreSQL models (`Rider`, `Team`,
`Country`, `Division`, `Region`) live in
[cdb.py](../../models/ns/cdb.py), using the tables `rider`, `team`, `country`,
`division`, and `region`. Workflow models remain in
[schema.py](../../models/ns/schema.py). Workflow references use `rider_id`.
Existing namespace databases require the manual
[soft-delete migration](../../../migrations/cdb_import_soft_delete.sql).
Run it separately against each namespace database before deploying this change;
never run it against `main`. Back up each database and pause writes first.
New namespaces receive the columns through normal schema creation.

The namespace importer also accepts packed `.cdb` uploads. It converts them to
SQLite with the `cdb-converter` CLI (`--precise-types`) before import, preserves
the original upload, and records the converted SQLite file as the namespace's
release baseline. The web image supplies Node.js 22 and the pinned converter.
SQLite uploads are checked read-only before a namespace is created.

Cyclist imports deliberately ignore `DYN_cyclist.CONSTANT`; PCS profile links
are managed by Activity Monitor and are never sourced from an uploaded CDB.
The rider mapping's `export_fields` section identifies `CONSTANT` as the PCS
slug output field. During release generation, only confirmed or explicitly
accepted/manual PCS profile links may be written there. The updated SQLite
working copy is converted back to PCM's packed `.cdb` format for download.

## Files

Manual object edits use the shared [editable-field allowlist](editable_fields.yaml).
Only listed logical model attributes are editable; omitted objects and fields are
read-only. The initial policy enables rider stats and leaves teams and races
read-only. Race fields cannot be enabled until a namespace CDB race model exists.
Primary keys and `is_deleted` cannot be enabled. This policy is separate from
import preservation rules and does not grant user permissions. The stats save
and application handlers enforce it on the server, and editor inputs reflect it.
Numeric validation and edit/approval permission checks still apply. The file is
read when used; deploy the same configuration to all web workers. To enable a
team field, add its logical attribute (for example `team_name`) under `team`;
this config does not itself create a team editing endpoint.

## Attribute proposals and review

Rider workflow state is stored in `identity_pcs_status` (unknown, verified,
review, failed) and the derived `has_pending_stat_changes` and
`has_pending_attribute_changes` flags. The flags are refreshed transactionally
by edit/approval workflows across all scopes, so approving one proposal cannot
clear a flag while another remains pending. These internal fields are read-only
to the manual field editor and are not mapped into the external CDB. Identity
defaults to unknown; this schema change does not infer or change PCS verification.
For existing namespace databases, pause writes, back up, and manually run
[rider_state_namespace.sql](../../tools/rider_state_namespace.sql) after the history
schema and before deploying this code. It backfills pending flags from existing
changes; run it against each namespace, never main. New namespaces receive the
columns through normal schema creation.

Rider and team detail pages show proposal forms for configured non-stat fields.
Editable values have a pencil icon that opens an inline input and save icon.
Saving returns to the detail page. Pending proposals show an hourglass linking
to Review while the current value stays visible; another edit is unavailable
until the proposal is reviewed. Read-only viewers also see pending indicators.
Saving creates a pending `cdb_changes` row and a `created` event, without changing
the object. Each object's Review tab lists its pending proposals. Approval locks
the proposal and object, checks the current value against the recorded baseline,
and applies the value and approval/application events in one transaction.
Conflicting proposals must be rejected and resubmitted against the current value.
Stats never enter this workflow. Race proposals require a future namespace race
model and mapping; the race Review tab is ready to display proposals when added.

Deployment: first review and manually run
[the history schema SQL](../../tools/cdb_change_history_namespace.sql) against each
existing **namespace** database if those tables do not exist. Never run it on main.
Then run `docker compose exec web flask seed-roles` to register `cdb_attributes`
read/update/approve permissions in **main** using the existing RBAC seed process.
Only admin receives these permissions initially. Assign update to proposers and
approve to reviewers as needed; page access also requires its existing teams or
stats read permission. The deployment commands are operator steps, not automatic
migrations. Back up databases before schema changes.

- `cdb_mappings.yaml`: authoritative PCM version, table, and import policies.
- `api.py`: mapping loaders, version lookups, table inspection, rider lookup,
  and transactional SQLite updates used by release generation.
- `import.py`: initial counts, delta planning, validation, and atomic imports.
  Python callers use `from webapp.integrations.cdb import cdb_import` because
  `import` is a reserved keyword. `CDBImporter` remains available from namespace
  setup for compatibility.
- `cli.py`: command-line arguments and output for table inspection and rider lookup.

## Mapping and inspection usage

```python
import sqlite3
from webapp.integrations.cdb import get_field_mapping, find_matching_tables

mapping = get_field_mapping('PCM2026', 'rider')
connection = sqlite3.connect('file:/path/to/database.cdb?mode=ro', uri=True)
try:
    tables = find_matching_tables(connection, mapping['cdb_table_name'])
finally:
    connection.close()
```

The legacy mapping imports from `webapp.models.ns.schema` remain available.
Rider lookup currently targets the `DYN_cyclist` fields used by the original
inspection script, rather than adapting its SQL to each PCM mapping version.

## Command line

```shell
python -m webapp.integrations.cdb.cli /path/to/database.cdb --table DYN_team
python -m webapp.integrations.cdb.cli /path/to/database.cdb --rider "Smith"
python -m webapp.integrations.cdb.cli /path/to/database.cdb --rider-id 123
```

The inspection CLI opens existing files read-only.

## Updates

```python
from webapp.integrations.cdb import apply_updates_to_sqlite

count = apply_updates_to_sqlite(
    '/path/to/release-copy.cdb',
    'DYN_cyclist',
    'IDcyclist',
    {(123, 'charac_i_mountain'): 78},
)
```

Updates are keyed by `(rider_id, column_name)`. Values are bound parameters;
identifiers are quoted. The function commits all updates together, rolls back
on failure, and closes the connection. The return value counts submitted values,
including IDs that do not match a row, preserving release generation behavior.
The file must already exist. Apply release edits to a copy of the source CDB.

Other import and namespace setup workflows remain in their existing modules;
this package is the home for shared CDB operations as they are extracted.

## Import policies

Each `mappings` entry declares `source`, `policy`, and `nulls`. Policies are
`overwrite` (copy changes), `preserve` (keep existing values), `reject` (block
changed values), and `identity` (primary key matching, only on `id`). All mapped
values populate new records. Current non-ID fields default to `preserve`.
Null modes are `allow`, `ignore`, and `reject`; null handling precedes update
policy, so `allow` still respects `preserve`. An ignored null on insert uses the
model default/NULL. IDs must reject nulls. Missing columns and duplicate IDs fail
the import. `fields` is a generated column-only projection for release consumers;
the YAML uses only `mappings`.

Initial creation and subsequent imports use the same atomic upsert engine.
Records missing from the complete CDB table are soft-deleted; reappearing deleted
records stay deleted and retain their values. Unmapped enrichment and workflow
records are retained. No imported-value snapshots are stored.

The namespace's **CDB Import** tab analyzes uploads without changing namespace
records or the release baseline. It shows inserts, field differences, preserved
values, removals, and validation errors before confirmation. Reviews expire after
one hour and are tied to the user, namespace, and PCM version. Confirmation locks
the mapped tables and rejects changed namespace data or policies since review.
The uploaded SQLite file becomes the release baseline after successful import;
preserved namespace values do not rewrite that file.

When creating a namespace with a CDB, the first submission validates the upload
against an empty namespace and shows only record counts for every mapped table.
Confirmation revalidates that plan and the namespace settings before creating or
provisioning the namespace. No CDB still allows direct namespace creation.
