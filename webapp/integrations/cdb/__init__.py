"""PCM CDB SQLite access, inspection, and field mappings."""

from importlib import import_module


def __getattr__(name):
    # `import` is a Python keyword; expose the module through a usable alias.
    if name == 'cdb_import':
        return import_module('.import', __name__)
    raise AttributeError(name)

from .api import (
    apply_updates_to_sqlite,
    describe_table,
    find_matching_tables,
    find_riders,
    get_cdb_mappings,
    get_cdb_table_name,
    get_field_mapping,
)

__all__ = [
    'apply_updates_to_sqlite',
    'describe_table',
    'find_matching_tables',
    'find_riders',
    'get_cdb_mappings',
    'get_cdb_table_name',
    'get_field_mapping',
]
