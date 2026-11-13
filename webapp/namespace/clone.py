"""Clone a published CDB and completed workflow records into a new database.

The release artifact is authoritative: live mapped rows are never copied.
Main database registration belongs to the caller, inside clone_namespace's
context, so a failed registration also cleans up the new files and database.
"""
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil

from sqlalchemy import and_, create_engine, or_, select, text

from webapp.config import get_namespace_path
from webapp.extensions import db
from webapp.integrations.cdb import cdb_import
from webapp.integrations.cdb.converter import convert_cdb_to_sqlite, validate_sqlite_cdb
from webapp.models.ns.cdb import CDBChange, CDBChangeEvent
from webapp.models.ns.field_configuration import FieldLevelConfiguration
from webapp.models.ns.base import (
    NamespaceBase, Release, ReleaseFile, RiderList, RiderListItem,
    StatChange, StatChangeSnapshot,
)
from webapp.namespace.setup import NamespaceProvisioner

logger = logging.getLogger(__name__)
CATEGORIES = ('rider_stats', 'rider_attributes', 'teams', 'races')


def _check_connection(conn, namespace):
    main_database = (db.engines.get('main') or db.engine).url.database
    if (namespace.database_name == main_database
            or conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name):
        raise ValueError('Cloning requires the selected isolated namespace database.')


def _rows(conn, model, *conditions):
    table = model.__table__
    query = select(table).where(*conditions).order_by(*table.primary_key.columns)
    return [dict(row) for row in conn.execute(query).mappings()]


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_path(namespace, stored_path):
    root = Path(get_namespace_path(namespace.slug)).resolve()
    path = Path(stored_path)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('A published release artifact is missing or outside its namespace.')
    return path


def load_clone_plan(source, categories):
    """Read one consistent history snapshot and fingerprint a reviewable plan."""
    categories = sorted(set(categories))
    if not set(categories).issubset(CATEGORIES):
        raise ValueError('Unknown change history selection.')
    engine = create_engine(source.build_connection_string(), isolation_level='REPEATABLE READ')
    try:
        with engine.begin() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            _check_connection(conn, source)
            releases = _rows(conn, Release, Release.status == 'published')
            if not releases or any(row['published_at'] is None for row in releases):
                raise ValueError('The source must have a published release with a publication date.')
            latest = max(releases, key=lambda row: (row['published_at'], row['id']))
            cutoff = latest['published_at']
            published_ids = {row['id'] for row in releases}
            files = _rows(conn, ReleaseFile, ReleaseFile.release_id == latest['id'])
            types = {row['file_type'] for row in files}
            if (not types.intersection({'cdb', 'cdb_sqlite'})
                    or not {'rider_stats_sql', 'release_overview_html'}.issubset(types)):
                raise ValueError('The latest published release is missing required release files.')

            data = {}
            data['field_level_configuration'] = _rows(conn, FieldLevelConfiguration)
            stats = (_rows(conn, StatChange, StatChange.is_approved.is_(True),
                           StatChange.release_id.in_(published_ids))
                     if 'rider_stats' in categories else [])
            data['stat_changes'] = stats
            kinds = []
            if 'rider_stats' in categories:
                kinds.append(and_(CDBChange.entity_type == 'rider',
                                  CDBChange.attribute_name.startswith('stat_', autoescape=True)))
            if 'rider_attributes' in categories:
                kinds.append(and_(CDBChange.entity_type == 'rider',
                                  ~CDBChange.attribute_name.startswith('stat_', autoescape=True)))
            if 'teams' in categories:
                kinds.append(CDBChange.entity_type == 'team')
            if 'races' in categories:
                kinds.append(CDBChange.entity_type.in_(['race', 'stage', 'race_class']))
            rejected_before_cutoff = select(CDBChangeEvent.change_id).where(
                CDBChangeEvent.event_type == 'rejected', CDBChangeEvent.occurred_at <= cutoff)
            completed = or_(
                and_(CDBChange.status == 'approved', CDBChange.applied_at <= cutoff),
                and_(CDBChange.status == 'rejected', CDBChange.id.in_(rejected_before_cutoff)),
            )
            changes = (_rows(conn, CDBChange, or_(*kinds), completed,
                             CDBChange.created_at <= cutoff) if kinds else [])
            data['cdb_changes'] = changes
            change_ids = [row['id'] for row in changes]
            events = (_rows(conn, CDBChangeEvent, CDBChangeEvent.change_id.in_(change_ids),
                            CDBChangeEvent.occurred_at <= cutoff,
                            or_(CDBChangeEvent.release_id.is_(None),
                                CDBChangeEvent.release_id.in_(published_ids))) if change_ids else [])
            data['cdb_change_events'] = events
            release_ids = {latest['id']} | {row['release_id'] for row in stats}
            release_ids.update(row['release_id'] for row in events if row['release_id'] is not None)
            data['releases'] = [row for row in releases if row['id'] in release_ids]

            list_ids = {row['stat_list_id'] for row in stats + changes if row['stat_list_id'] is not None}
            data['rider_list'] = _rows(conn, RiderList, RiderList.id.in_(list_ids)) if list_ids else []
            if list_ids != {row['id'] for row in data['rider_list']}:
                raise ValueError('Completed history references a missing rider list.')
            members = {(row['stat_list_id'], row.get('rider_id', row.get('entity_id')))
                       for row in stats + changes if row['stat_list_id'] is not None}
            data['rider_list_item'] = [
                row for row in (_rows(conn, RiderListItem, RiderListItem.rider_list_id.in_(list_ids))
                                if list_ids else [])
                if (row['rider_list_id'], row['rider_id']) in members
            ]
            scopes = {(row['race_result_id'], row['stat_list_id'], row['rider_id']) for row in stats}
            data['stat_change_snapshot'] = []
            for row in (_rows(conn, StatChangeSnapshot) if scopes else []):
                if (row['race_result_id'], row['stat_list_id'], row['rider_id']) in scopes:
                    # A clone must not notify or operate on the source's Discord threads.
                    row.update(discord_thread_id=None, discord_thread_url=None, discord_notified_at=None)
                    data['stat_change_snapshot'].append(row)
    finally:
        engine.dispose()

    for file in files:
        path = _artifact_path(source, file['relative_path'])
        checksum = _sha256(path)
        if file['sha256_checksum'] and checksum != file['sha256_checksum']:
            raise ValueError('A published release artifact failed checksum validation.')
        file['sha256_checksum'] = checksum
    payload = dict(source_id=source.id, pcm_version=source.pcm_version, categories=categories,
                   release=latest, files=files, data=data)
    payload['fingerprint'] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    payload['counts'] = {name: len(rows) for name, rows in data.items()
                         if name != 'field_level_configuration'}
    payload['history_summary'] = {
        label: count for category, label, count in [
            ('rider_stats', 'Rider Stats', len(stats) + sum(
                row['entity_type'] == 'rider' and row['attribute_name'].startswith('stat_') for row in changes)),
            ('rider_attributes', 'Rider Attributes', sum(
                row['entity_type'] == 'rider' and not row['attribute_name'].startswith('stat_') for row in changes)),
            ('teams', 'Teams', sum(row['entity_type'] == 'team' for row in changes)),
            ('races', 'Races', sum(row['entity_type'] in ('race', 'stage', 'race_class') for row in changes)),
        ] if category in categories
    }
    return payload


def _insert_rows(conn, table, rows):
    for offset in range(0, len(rows), 1000):
        conn.execute(table.insert(), rows[offset:offset + 1000])


def _copy_history(conn, plan):
    data = plan['data']
    # Configuration is seeded by provisioning; replace defaults with the source's
    # effective mappings before importing the release. No schema is dropped.
    conn.execute(FieldLevelConfiguration.__table__.delete())
    for model in (FieldLevelConfiguration, Release, RiderList, RiderListItem,
                  StatChange, StatChangeSnapshot, CDBChange, CDBChangeEvent):
        _insert_rows(conn, model.__table__, data[model.__tablename__])


@contextmanager
def clone_namespace(source, destination, categories, expected_fingerprint):
    """Own only fresh destination resources; remove them on any failure."""
    if (source.database_name == destination.database_name or source.slug == destination.slug
            or not re.fullmatch(r'[a-z0-9-]+', destination.slug)):
        raise ValueError('The clone must have a different, valid destination namespace.')
    plan = load_clone_plan(source, categories)
    if plan['fingerprint'] != expected_fingerprint:
        raise ValueError('The clone preview changed. Preview the source again.')
    if destination.pcm_version != source.pcm_version:
        raise ValueError('The clone PCM version must match the source namespace.')
    provisioner = NamespaceProvisioner()
    root = Path(get_namespace_path(destination.slug))
    files_created = database_created = False
    engine = None
    try:
        root.mkdir(parents=True, exist_ok=False)
        files_created = True
        provisioner.create_namespace_database(destination.database_name, require_new=True)
        database_created = True
        url = destination.build_connection_string()
        provisioner.create_namespace_schema(url, destination.pcm_version)
        engine = create_engine(url)
        with engine.begin() as conn:
            _check_connection(conn, destination)
            _copy_history(conn, plan)

        release_dir = root / 'releases'
        release_dir.mkdir()
        copied_files = []
        baseline_file = None
        for file in plan['files']:
            original = _artifact_path(source, file['relative_path'])
            relative = f'releases/{file["id"]}-{original.name}'
            target = root / relative
            shutil.copy2(original, target)
            if _sha256(target) != file['sha256_checksum']:
                raise ValueError('The source release file changed during cloning.')
            copied_files.append({**file, 'relative_path': relative})
            if file['file_type'] == 'cdb' or (baseline_file is None and file['file_type'] == 'cdb_sqlite'):
                baseline_file = (file['file_type'], target)
        baseline = root / 'cdb' / 'import' / 'clone-baseline.sqlite'
        baseline.parent.mkdir(parents=True)
        if baseline_file[0] == 'cdb':
            convert_cdb_to_sqlite(str(baseline_file[1]), str(baseline))
        else:
            shutil.copy2(baseline_file[1], baseline)
            validate_sqlite_cdb(baseline)
        with cdb_import.CDBImporter(str(baseline), url, destination.pcm_version,
                                   expected_database=destination.database_name) as importer:
            mapped_counts = importer.import_all_tables()
        with engine.begin() as conn:
            _check_connection(conn, destination)
            _insert_rows(conn, ReleaseFile.__table__, copied_files)
            # Explicit IDs preserve relations, but do not advance SERIAL sequences.
            for table in NamespaceBase.metadata.sorted_tables:
                if 'id' in table.c and table.c.id.primary_key:
                    name = table.name
                    conn.execute(text(f"""
                        SELECT setval(pg_get_serial_sequence('{name}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {name}), 1), EXISTS (SELECT 1 FROM {name}))
                    """))
        destination.sqlite_file_path = 'cdb/import/clone-baseline.sqlite'
        yield dict(source_namespace_id=source.id, source_release_id=plan['release']['id'],
                   source_release_version=plan['release']['version'], categories=plan['categories'],
                   fingerprint=plan['fingerprint'], history_counts=plan['counts'], mapped_counts=mapped_counts)
    except BaseException:
        if engine is not None:
            engine.dispose()
        if database_created:
            try:
                provisioner.drop_namespace_database(destination.database_name)
            except Exception:
                logger.exception('Could not remove failed clone database %s', destination.database_name)
        if files_created:
            shutil.rmtree(root)
        raise
    finally:
        if engine is not None:
            engine.dispose()
