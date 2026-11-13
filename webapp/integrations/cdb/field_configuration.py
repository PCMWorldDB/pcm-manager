"""Defaults and storage for namespace field configuration."""
from sqlalchemy import Boolean, Float, Integer, String, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import insert

from webapp.integrations.cdb import get_field_mapping
from webapp.integrations.cdb.editability import SYSTEM_FIELDS, get_editable_fields
from webapp.models.ns.cdb import CDB_TABLES
from webapp.models.ns.field_configuration import FieldConfigurationAudit, FieldLevelConfiguration

TABLE = FieldLevelConfiguration.__table__
OBJECTS = {model.__tablename__: model.__table__ for model in CDB_TABLES}
IMPORT_POLICIES = ('preserve', 'overwrite', 'reject')
NULL_POLICIES = ('allow', 'ignore', 'reject')


def field_slug(object_name, name):
    if object_name == 'rider':
        if name.startswith('stat_'):
            return 'rider:stat'
        if name == 'birth_date':
            return 'rider:date_of_birth'
    return f'{object_name}:{name}'


def configurable_columns(object_name):
    return {column.name: column for column in OBJECTS[object_name].columns
            if column.name not in SYSTEM_FIELDS}


def can_edit_column(object_name, name):
    column = configurable_columns(object_name).get(name)
    return ((object_name in ('rider', 'team') or
             (object_name == 'race' and name in ('first_cycling_id', 'pcs_slug')))
            and column is not None and not column.primary_key
            and isinstance(column.type, (Boolean, Float, Integer, String)))


def field_slugs():
    return sorted({field_slug(object_name, name) for object_name in OBJECTS
                   for name in configurable_columns(object_name)})


def default_rows(pcm_version):
    for object_name in OBJECTS:
        mappings = get_field_mapping(pcm_version, object_name)['mappings']
        editable = get_editable_fields(object_name)
        for name in sorted(configurable_columns(object_name)):
            mapping = mappings.get(name, {})
            yield dict(
                object=object_name, name=name, slug=field_slug(object_name, name),
                is_mapped_to_cdb=bool(mapping), cdb_source=mapping.get('source'),
                cdb_source_import_policy=mapping.get('policy'),
                cdb_source_import_null_handling=mapping.get('nulls'),
                is_editable=name in editable, is_require_approval=True,
            )


def seed_fields(conn, pcm_version):
    conn.execute(insert(TABLE).values(list(default_rows(pcm_version))).on_conflict_do_nothing())


def stored_fields(conn, object_name=None, *, lock=False):
    """Return stored fields; pre-upgrade namespaces continue using baseline behavior."""
    if conn is None or not inspect(conn).has_table(TABLE.name):
        return {}
    query = select(TABLE)
    if object_name is not None:
        query = query.where(TABLE.c.object == object_name)
    if lock:
        query = query.with_for_update(read=True)
    return {(row['object'], row['name']): row for row in conn.execute(query).mappings()}


def import_mapping(default_mapping, object_name, configuration):
    """Overlay import settings while preserving version-specific table and optional columns."""
    fields = dict(default_mapping['mappings'])
    for (kind, name), field in configuration.items():
        if kind != object_name or name == 'id' or name not in configurable_columns(kind):
            continue
        baseline = fields.get(name, {})
        if field['is_mapped_to_cdb']:
            fields[name] = dict(
                source=field['cdb_source'], policy=field['cdb_source_import_policy'],
                nulls=field['cdb_source_import_null_handling'],
            )
            if 'required' in baseline:
                fields[name]['required'] = baseline['required']
        else:
            fields.pop(name, None)
    return {**default_mapping, 'mappings': fields}


def form_values(object_name, name, form):
    column = configurable_columns(object_name)[name]
    mapped = form.get('is_mapped_to_cdb') == '1'
    source = form.get('cdb_source', '').strip() if mapped else None
    policy = form.get('cdb_source_import_policy') if mapped else None
    nulls = form.get('cdb_source_import_null_handling') if mapped else None
    editable = form.get('is_editable') == '1'
    if editable and not can_edit_column(object_name, name):
        raise ValueError('This column is managed by the application.')
    if mapped and (not source or len(source) > 255 or '\x00' in source):
        raise ValueError('Enter a valid CDB source column.')
    if mapped and (policy not in IMPORT_POLICIES or nulls not in NULL_POLICIES):
        raise ValueError('Choose a valid import policy and null handling.')
    if not column.nullable and mapped and nulls != 'reject':
        raise ValueError('A required column must reject NULL values.')
    if not column.nullable and not mapped and column.server_default is None:
        raise ValueError('A required column must be mapped.')
    return dict(is_mapped_to_cdb=mapped, cdb_source=source, cdb_source_import_policy=policy,
                cdb_source_import_null_handling=nulls, is_editable=editable,
                is_require_approval=(not can_edit_column(object_name, name)
                                     or form.get('is_require_approval') == '1'))


def forward_audits(namespace):
    """Idempotently project committed namespace events into the main Audit Log."""
    from webapp.extensions import db
    from webapp.models import AuditAction, AuditLog, User

    main_engine = db.engines.get('main') or db.engine
    if namespace.database_name == main_engine.url.database:
        raise ValueError('Field audit history requires an isolated namespace database.')
    engine = create_engine(namespace.build_connection_string())
    events = FieldConfigurationAudit.__table__
    try:
        with engine.begin() as conn:
            if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                raise ValueError('Field audit database does not match the selected namespace.')
            if not inspect(conn).has_table(events.name):
                return
            pending = conn.execute(select(events).where(events.c.forwarded.is_(False))
                                   .order_by(events.c.id).with_for_update()).mappings().all()
            if not pending:
                return
            # Serialize projections from concurrent requests before deduplication.
            db.session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 5928100000 + namespace.id})
            for event in pending:
                query = AuditLog.query.filter_by(
                    action=AuditAction.FIELD_CONFIGURATION_UPDATED,
                    resource_type='namespace', resource_id=namespace.id,
                ).filter(AuditLog.details['field_configuration_event_id'].as_integer() == event['id'])
                if query.first() is None:
                    actor = db.session.get(User, event['actor_id']) if event['actor_id'] is not None else None
                    db.session.add(AuditLog(
                        action=AuditAction.FIELD_CONFIGURATION_UPDATED,
                        user_id=actor.id if actor else None, username=event['username'],
                        resource_type='namespace', resource_id=namespace.id, resource_name=namespace.name,
                        details={**event['details'], 'field_configuration_event_id': event['id']},
                        ip_address=event['ip_address'], created_at=event['created_at'],
                    ))
            db.session.commit()
            conn.execute(events.update().where(events.c.id.in_([event['id'] for event in pending]))
                         .values(forwarded=True))
    except Exception:
        db.session.rollback()
        raise
    finally:
        engine.dispose()
