"""Transactional updates to an existing CDB SQLite file."""

import sqlite3
from pathlib import Path


def apply_updates_to_sqlite(sqlite_file_path, table_name, id_column, updates):
    """Apply stat updates to the copied SQLite CDB file."""
    if not updates:
        return 0

    grouped_updates = {}
    for (cyclist_pcm_id, stat_column), final_value in updates.items():
        grouped_updates.setdefault(stat_column, []).append((final_value, cyclist_pcm_id))

    applied_values = 0
    sqlite_uri = Path(sqlite_file_path).resolve().as_uri() + '?mode=rw'
    connection = sqlite3.connect(sqlite_uri, uri=True)
    try:
        cursor = connection.cursor()
        table_name = table_name.replace('"', '""')
        id_column = id_column.replace('"', '""')
        for stat_column, pairs in grouped_updates.items():
            stat_column = stat_column.replace('"', '""')
            cursor.executemany(
                f'UPDATE "{table_name}" SET "{stat_column}" = ? WHERE "{id_column}" = ?',
                pairs,
            )
            applied_values += len(pairs)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return applied_values


