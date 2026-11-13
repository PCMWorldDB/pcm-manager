"""CDB import validation, planning, and atomic namespace upserts."""
import hashlib
import json
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from webapp.integrations.cdb import get_field_mapping
from webapp.integrations.cdb.field_configuration import import_mapping, stored_fields
from webapp.integrations.cdb.rider_state import PCS_IDENTITY_RESET
from webapp.integrations.pcs.slug import assumed_rider_slug
from webapp.models.ns.cdb import Country, Division, Race, RaceClass, Region, Rider, Stage, Team


MODELS = (('country', Country), ('division', Division), ('region', Region),
          ('team', Team), ('race_class', RaceClass), ('race', Race), ('stage', Stage),
          ('rider', Rider))


def _rider_pcs_slug(first_name, last_name):
    """Generate an unverified PCS identifier from the rider's name."""
    return assumed_rider_slug(first_name, last_name)


def build_plan(sqlite, connection, pcm_version, table_types=None, *, lock_configuration=False,
               assumed_rider_slug_ids=None):
    plan = {'tables': {}, 'errors': [], 'changes': []}
    operations = []
    state = []
    mappings = {}
    configuration = stored_fields(connection, lock=lock_configuration)
    for kind, model in MODELS:
        if table_types is not None and kind not in table_types:
            continue
        mapping = import_mapping(get_field_mapping(pcm_version, kind), kind, configuration)
        mappings[kind] = mapping
        fields = mapping['mappings']
        table = model.__table__
        existing = ({row['id']: dict(row) for row in connection.execute(select(table)).mappings()}
                    if connection is not None else {})
        state.append((kind, sorted(existing.values(), key=lambda row: row['id'])))
        counts = dict(inserted=0, updated=0, deleted=0, preserved=0)
        plan['tables'][kind] = counts
        quote = lambda name: '"' + name.replace('"', '""') + '"'
        available = {row[1].casefold() for row in sqlite.execute(
            f'PRAGMA table_info({quote(mapping["cdb_table_name"])})'
        )}
        missing = {config['source'] for config in fields.values()
                   if config.get('required', True) and config['source'].casefold() not in available}
        if missing:
            raise sqlite3.OperationalError(f'{kind}: missing CDB columns: {", ".join(sorted(missing))}')
        columns = ', '.join(
            quote(config['source']) if config['source'].casefold() in available else 'NULL'
            for config in fields.values()
        )
        rows = sqlite.execute(
            f'SELECT {columns} FROM {quote(mapping["cdb_table_name"])} '
            f'ORDER BY {quote(fields["id"]["source"])}'
        )
        seen = set()
        for source in rows:
            incoming = {}
            for (field, config), value in zip(fields.items(), source):
                if value is not None:
                    target_type = table.c[field].type.python_type
                    if target_type is int and isinstance(value, float) and not value.is_integer():
                        raise ValueError(f'{kind}.{field}: expected an integer')
                    value = bool(int(value)) if target_type is bool else target_type(value)
                    if kind == 'rider' and field == 'pcs_slug':
                        value = value.strip()
                        value = None if value in ('', '0') else value
                    max_length = getattr(table.c[field].type, 'length', None)
                    if target_type is str and value is not None and max_length is not None and len(value) > max_length:
                        raise ValueError(f'{kind}.{field}: value is too long')
                incoming[field] = value
            key = incoming['id']
            if key is None or key in seen:
                raise ValueError(f'{kind}: missing or duplicate primary key {key}')
            seen.add(key)
            old = existing.get(key)
            values = {}
            for field, config in fields.items():
                value = incoming[field]
                if value is None:
                    if config['nulls'] == 'reject':
                        plan['errors'].append(f'{kind} {key}: {field} cannot be NULL')
                        continue
                    if config['nulls'] == 'ignore':
                        continue
                if old is None:
                    values[field] = value
                    continue
                if old[field] == value:
                    continue
                policy = config['policy']
                action = 'preserved' if policy == 'preserve' or old['is_deleted'] else policy
                plan['changes'].append(dict(table=kind, id=key, field=field,
                                            old=old[field], new=value, action=action))
                if policy == 'reject':
                    plan['errors'].append(f'{kind} {key}: {field} changed')
                elif action == 'overwrite':
                    values[field] = value
                elif action == 'preserved':
                    counts['preserved'] += 1
            if (kind == 'rider' and 'pcs_slug' in incoming and incoming['pcs_slug'] is None
                    and (old is None or fields['pcs_slug']['policy'] == 'overwrite')
                    and (old is None or (not old['is_deleted'] and (old['pcs_slug'] or '').strip() in ('', '0')))):
                effective = {**(old or {}), **values}
                slug = _rider_pcs_slug(effective.get('first_name'), effective.get('last_name'))
                if slug:
                    values['pcs_slug'] = slug
                    values['pcs_slug_is_assumed'] = True
                    if assumed_rider_slug_ids is not None:
                        assumed_rider_slug_ids.add(key)
                    if old is None:
                        incoming['pcs_slug'] = slug
                    else:
                        plan['changes'].append(dict(table=kind, id=key, field='pcs_slug',
                                                    old=old['pcs_slug'], new=slug, action='overwrite'))
            if (kind == 'rider' and incoming.get('pcs_slug') and 'pcs_slug_is_assumed' not in values
                    and (old is None or old['pcs_slug_is_assumed'])):
                if old is None or values.get('pcs_slug', old['pcs_slug']) == incoming['pcs_slug']:
                    values['pcs_slug_is_assumed'] = False
            if kind == 'rider' and old is not None and 'pcs_slug' in values:
                reset = {**PCS_IDENTITY_RESET, 'pcs_identity_verified_override': False}
                for field, value in reset.items():
                    if old[field] != value:
                        values[field] = value
                        plan['changes'].append(dict(table=kind, id=key, field=field,
                                                    old=old[field], new=value, action='overwrite'))
            if old is None:
                counts['inserted'] += 1
                values['is_deleted'] = False
                operations.append((kind, 'insert', key, values))
                plan['changes'].append(dict(table=kind, id=key, field='record', old=None,
                                            new=incoming, action='insert'))
            elif values:
                counts['updated'] += 1
                operations.append((kind, 'update', key, values))
        for key in sorted(existing.keys() - seen):
            if not existing[key]['is_deleted']:
                counts['deleted'] += 1
                operations.append((kind, 'update', key, {'is_deleted': True}))
                plan['changes'].append(dict(table=kind, id=key, field='is_deleted',
                                            old=False, new=True, action='soft_delete'))
    # Includes current rows and policies, so confirmation rejects stale reviews.
    payload = (plan, state, mappings)
    plan['fingerprint'] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return plan, operations


def analyze_initial(sqlite_path, pcm_version):
    """Validate against an empty namespace and return only table counts."""
    with sqlite3.connect(Path(sqlite_path).resolve().as_uri() + '?mode=ro', uri=True) as source:
        plan, _ = build_plan(source, None, pcm_version)
    return {'tables': {kind: counts['inserted'] for kind, counts in plan['tables'].items()},
            'errors': plan['errors'], 'fingerprint': plan['fingerprint']}


def apply_plan(connection, operations):
    models = dict(MODELS)
    for kind, action, key, values in operations:
        table = models[kind].__table__
        if action == 'insert':
            connection.execute(table.insert().values(**values))
        else:
            connection.execute(table.update().where(table.c.id == key).values(**values))


class CDBImporter:
    """Import data from SQLite CDB files to PostgreSQL namespace databases"""
    
    def __init__(self, sqlite_path, postgres_connection_string, pcm_version='PCM2025', expected_database=None):
        """Initialize importer
        
        Args:
            sqlite_path: Path to SQLite CDB file
            postgres_connection_string: Connection string to namespace PostgreSQL database
            pcm_version: PCM version for field mapping (default: PCM2025)
        """
        self.sqlite_path = sqlite_path
        self.postgres_connection_string = postgres_connection_string
        self.pcm_version = pcm_version
        self.expected_database = expected_database
        self.sqlite_conn = None
        self.pg_engine = None
        self.pg_session = None
    
    def connect(self):
        """Establish connections to both databases"""
        # Connect to SQLite
        self.sqlite_conn = sqlite3.connect(Path(self.sqlite_path).resolve().as_uri() + '?mode=ro', uri=True)
        self.sqlite_conn.row_factory = sqlite3.Row  # Access columns by name
        
        # Connect to PostgreSQL
        self.pg_engine = create_engine(self.postgres_connection_string)
        if self.expected_database and self.pg_engine.url.database != self.expected_database:
            self.disconnect()
            raise ValueError('CDB import database does not match the selected namespace')
        if self.expected_database:
            with self.pg_engine.connect() as connection:
                if connection.execute(text('SELECT current_database()')).scalar_one() != self.expected_database:
                    self.disconnect()
                    raise ValueError('CDB import connected to the wrong namespace database')
        Session = sessionmaker(bind=self.pg_engine)
        self.pg_session = Session()
    
    def disconnect(self):
        """Close all connections"""
        if self.sqlite_conn:
            self.sqlite_conn.close()
        if self.pg_session:
            self.pg_session.close()
        if self.pg_engine:
            self.pg_engine.dispose()

    def import_mapped_table(
        self, table_type, model_class, batch_size=1000, boolean_fields=None,
        commit_batches=True,
    ):
        """Apply the same policies when importing an individual mapped table."""
        if dict(MODELS).get(table_type) is not model_class:
            raise ValueError('CDB table and namespace model do not match')
        with self.pg_engine.begin() as connection:
            connection.execute(text(f'LOCK TABLE {model_class.__tablename__} IN EXCLUSIVE MODE'))
            plan, operations = build_plan(self.sqlite_conn, connection, self.pcm_version, {table_type},
                                          lock_configuration=True)
            if plan['errors']:
                raise ValueError('; '.join(plan['errors'][:20]))
            apply_plan(connection, operations)
        if table_type in ('race', 'rider'):
            from webapp.integrations.identity import publish_or_defer
            self.identity_sync_result = publish_or_defer(self.postgres_connection_string)
        counts = plan['tables'][table_type]
        return counts['inserted'] + counts['updated']

    def import_cyclists(self, batch_size=1000):
        """Import cyclist table from SQLite to PostgreSQL
        
        Uses PCM version mapping to translate CDB fields to internal model.
        
        Args:
            batch_size: Number of records to process at once
            
        Returns:
            Number of cyclists imported
        """
        return self.import_mapped_table('rider', Rider, batch_size=batch_size)

    def import_teams(self, batch_size=1000):
        """Import team table from SQLite to PostgreSQL."""
        return self.import_mapped_table('team', Team, batch_size=batch_size)

    def import_countries(self, batch_size=1000):
        """Import country lookup table from SQLite to PostgreSQL."""
        return self.import_mapped_table('country', Country, batch_size=batch_size)

    def import_divisions(self, batch_size=1000):
        """Import division lookup table from SQLite to PostgreSQL."""
        return self.import_mapped_table('division', Division, batch_size=batch_size)

    def import_race_classes(self, batch_size=1000):
        """Import race categories from SQLite to PostgreSQL."""
        return self.import_mapped_table('race_class', RaceClass, batch_size=batch_size)

    def import_races(self, batch_size=1000):
        """Import race definitions from SQLite to PostgreSQL."""
        return self.import_mapped_table('race', Race, batch_size=batch_size)

    def import_stages(self, batch_size=1000):
        """Import stage calendar entries from SQLite to PostgreSQL."""
        return self.import_mapped_table('stage', Stage, batch_size=batch_size)

    def import_regions(self, batch_size=1000):
        """Import region lookup table from SQLite to PostgreSQL."""
        return self.import_mapped_table(
            'region',
            Region,
            batch_size=batch_size,
            boolean_fields={'is_europe'},
        )
    
    def analyze(self):
        with self.pg_engine.connect() as connection:
            return build_plan(self.sqlite_conn, connection, self.pcm_version)[0]

    def import_all_tables(self, *, replace_existing=True, expected_fingerprint=None, seed_initial_identities=False):
        """Upsert mapped records atomically and soft-delete absent records.

        Only namespace creation from a CDB enables identity seeds. They run
        after mapped rows are imported, within this same transaction. Clones
        and subsequent uploads retain the default without seeds.
        """
        with self.pg_engine.begin() as connection:
            # Also prevents app writes between comparison and application.
            tables = ', '.join(model.__tablename__ for _, model in MODELS)
            connection.execute(text(f'LOCK TABLE {tables} IN EXCLUSIVE MODE'))
            assumed_slug_ids = set()
            plan, operations = build_plan(self.sqlite_conn, connection, self.pcm_version,
                                          lock_configuration=True,
                                          assumed_rider_slug_ids=assumed_slug_ids)
            if expected_fingerprint and plan['fingerprint'] != expected_fingerprint:
                raise ValueError('Import review is stale. Analyze the file again.')
            if plan['errors']:
                raise ValueError('; '.join(plan['errors'][:20]))
            if seed_initial_identities and any(
                connection.execute(select(model.id).limit(1)).first() for _, model in MODELS
            ):
                raise ValueError('Identity seeds require a new, empty namespace')
            apply_plan(connection, operations)
            if seed_initial_identities:
                from webapp.seeds import race_identity, rider_identity
                rider_identity.seed(connection, assumed_slug_ids=assumed_slug_ids)
                race_identity.seed(connection)
            for _, model in MODELS:
                name = model.__tablename__
                connection.execute(text(f"""
                    SELECT setval(pg_get_serial_sequence('{name}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {name}), 1),
                        EXISTS (SELECT 1 FROM {name}))
                """))
        from webapp.integrations.identity import publish_or_defer
        self.identity_sync_result = publish_or_defer(self.postgres_connection_string)
        return {kind: counts['inserted'] + counts['updated']
                for kind, counts in plan['tables'].items()}

    def __enter__(self):
        """Context manager support"""
        try:
            self.connect()
        except Exception:
            self.disconnect()
            raise
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup"""
        self.disconnect()
