"""PCM CDB SQLite updates, inspection, and field mappings."""

import sqlite3
from pathlib import Path

import yaml


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


# Load PCM Version Field Mappings from YAML
# Maps PCM CDB fields to internal model fields
def get_cdb_mappings():
    """Load CDB mappings from YAML file
    
    Returns:
        dict: Complete mapping configuration with PCM_VERSION_MAPPINGS and CDB_TABLE_MAPPINGS
    """
    yaml_path = Path(__file__).with_name('cdb_mappings.yaml')
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


def get_field_mapping(pcm_version, table_type='cyclist'):
    """Get field mapping for a specific PCM version and table type
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        dict: Mapping configuration with 'cdb_table_name' and 'fields'
    """
    cdb_mappings = get_cdb_mappings()
    
    # Get the mapping version for this PCM version and table type
    version_mappings = cdb_mappings.get('PCM_VERSION_MAPPINGS', {})
    pcm_config = version_mappings.get(pcm_version.upper())
    if not pcm_config:
        raise ValueError(f"Unsupported PCM version: {pcm_version}")
    
    mapping_version = pcm_config.get(table_type)
    if not mapping_version:
        raise ValueError(f"Table type '{table_type}' not defined for PCM version {pcm_version}")
    
    # Get the actual table mapping from CDB_TABLE_MAPPINGS
    table_mappings = cdb_mappings.get('CDB_TABLE_MAPPINGS', {})
    version_config = table_mappings.get(mapping_version)
    if not version_config:
        raise ValueError(f"Mapping version '{mapping_version}' not found in CDB_TABLE_MAPPINGS")
    
    table_mapping = version_config.get(table_type)
    if not table_mapping:
        raise ValueError(f"Table type '{table_type}' not defined in mapping version '{mapping_version}'")
    
    return table_mapping


def get_cdb_table_name(pcm_version, table_type='cyclist'):
    """Get the CDB table name for a specific PCM version
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        str: CDB table name
    """
    mapping = get_field_mapping(pcm_version, table_type)
    return mapping['cdb_table_name']


def normalize_name(value):
    return ''.join(char for char in value.lower() if char.isalnum())


def find_matching_tables(conn, requested_name):
    normalized_requested = normalize_name(requested_name)
    singular_requested = normalized_requested.rstrip('s')

    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    exact_matches = []
    fuzzy_matches = []
    for (table_name,) in rows:
        normalized_table = normalize_name(table_name)
        singular_table = normalized_table.rstrip('s')
        if normalized_table in (normalized_requested, singular_requested):
            exact_matches.append(table_name)
        elif singular_table in (normalized_requested, singular_requested):
            exact_matches.append(table_name)
        elif normalized_requested in normalized_table:
            fuzzy_matches.append(table_name)

    return exact_matches or fuzzy_matches


def describe_table(conn, table_name):
    quoted_name = table_name.replace('"', '""')
    columns = conn.execute(f'PRAGMA table_info("{quoted_name}")').fetchall()
    sample = conn.execute(f'SELECT * FROM "{quoted_name}" LIMIT 1').fetchone()
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{quoted_name}"').fetchone()[0]
    return columns, sample, row_count


def quote_identifier(value):
    return value.replace('"', '""')


def find_riders(conn, search_terms=None, cdb_ids=None, firstcycling_ids=None, limit=25):
    search_terms = [term.strip() for term in (search_terms or []) if term.strip()]
    cdb_ids = [int(value) for value in (cdb_ids or [])]
    firstcycling_ids = [int(value) for value in (firstcycling_ids or [])]

    where_parts = []
    params = {}

    for index, term in enumerate(search_terms):
        param_name = f'term_{index}'
        where_parts.append(
            '('
            f'LOWER(gene_sz_firstname) LIKE :{param_name} OR '
            f'LOWER(gene_sz_lastname) LIKE :{param_name} OR '
            f'LOWER(gene_sz_firstlastname) LIKE :{param_name} OR '
            f'LOWER(TRIM(COALESCE(gene_sz_firstname, "") || " " || COALESCE(gene_sz_lastname, ""))) LIKE :{param_name}'
            ')'
        )
        params[param_name] = f'%{term.lower()}%'

    if cdb_ids:
        placeholders = []
        for index, rider_id in enumerate(cdb_ids):
            param_name = f'cdb_id_{index}'
            placeholders.append(f':{param_name}')
            params[param_name] = rider_id
        where_parts.append(f'IDcyclist IN ({", ".join(placeholders)})')

    if firstcycling_ids:
        placeholders = []
        for index, rider_id in enumerate(firstcycling_ids):
            param_name = f'fc_id_{index}'
            placeholders.append(f':{param_name}')
            params[param_name] = rider_id
        where_parts.append(f'CAST(value_f_current_ability AS INTEGER) IN ({", ".join(placeholders)})')

    if not where_parts:
        raise SystemExit('Provide --rider, --rider-id, or --firstcycling-id for rider lookup.')

    params['limit'] = limit
    query = f"""
        SELECT
            IDcyclist,
            gene_sz_firstname,
            gene_sz_lastname,
            gene_sz_firstlastname,
            CAST(value_f_current_ability AS INTEGER) AS first_cycling_id,
            fkIDteam,
            fkIDregion,
            gene_i_birthdate,
            gene_i_size,
            gene_i_weight,
            charac_i_plain,
            charac_i_mountain,
            charac_i_medium_mountain,
            charac_i_hill,
            charac_i_downhilling,
            charac_i_cobble,
            charac_i_timetrial,
            charac_i_prologue,
            charac_i_sprint,
            charac_i_acceleration,
            charac_i_endurance,
            charac_i_resistance,
            charac_i_recuperation,
            charac_i_baroudeur
        FROM DYN_cyclist
        WHERE {' OR '.join(where_parts)}
        ORDER BY gene_sz_lastname ASC, gene_sz_firstname ASC, IDcyclist ASC
        LIMIT :limit
    """
    return conn.execute(query, params).fetchall()
