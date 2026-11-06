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
