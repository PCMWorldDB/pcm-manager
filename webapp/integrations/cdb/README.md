# CDB

Shared tools for interacting with the Pro Cycling Manager CDB SQLite file.
This integration handles SQLite inspection, release stat updates, and PCM version
field mappings. Namespace PostgreSQL models remain in `webapp/models/ns`.

## Files

- `cdb_mappings.yaml`: authoritative PCM version, table, and field mappings,
  moved from `webapp/models/ns/cdb_mappings.yaml` without changing its contents.
- `api.py`: mapping loaders, version lookups, table inspection, rider lookup,
  the inspection CLI, and transactional SQLite updates used by release generation.

## Mapping and inspection usage

```python
import sqlite3
from webapp.integrations.cdb import get_field_mapping, find_matching_tables

mapping = get_field_mapping('PCM2026', 'cyclist')
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
python -m webapp.integrations.cdb /path/to/database.cdb --table DYN_team
python -m webapp.integrations.cdb /path/to/database.cdb --rider "Smith"
python -m webapp.integrations.cdb /path/to/database.cdb --rider-id 123
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
