"""Provider-only identity maps and retryable namespace contributions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import re

from flask import g, has_app_context, has_request_context
from sqlalchemy import create_engine, inspect, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from webapp.config import get_setting
from webapp.extensions import db
from webapp.models.main.identity import RaceIdentity, RiderIdentity
from webapp.models.ns.identity import IdentitySyncOutbox


logger = logging.getLogger(__name__)
MODELS = {'race': RaceIdentity, 'rider': RiderIdentity}
OUTBOX = IdentitySyncOutbox.__table__


@contextmanager
def main_engine():
    if has_app_context():
        yield db.engines['main']
    else:
        url = get_setting('SQLALCHEMY_DATABASE_URI')
        if not url:
            raise ValueError('An explicit main database URL is required.')
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def identity_values(kind, values):
    """Accept source IDs only. Namespace/WDBO IDs must never enter these maps."""
    if kind not in MODELS:
        raise ValueError('Unknown identity kind.')
    if any(key.startswith('wdbo_') or key in ('race_id', 'rider_id', 'namespace_id', 'entity_id', 'name')
           for key in values):
        raise ValueError('Shared identity maps contain FC and PCS IDs only.')
    value = values.get('firstcycling_id')
    if value in (None, ''):
        value = None
    elif isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**31:
        raise ValueError('Invalid firstcycling_id')
    else:
        value = int(value)
    slug = values.get('pcs_slug')
    slug = slug.strip() if isinstance(slug, str) else slug
    if slug == '':
        slug = None
    if slug is not None and (not isinstance(slug, str) or len(slug) > 500
                             or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug)):
        raise ValueError('Enter a PCS slug without a URL, year, or stage path.')
    if value is None and slug is None:
        raise ValueError('At least one provider ID is required.')
    return {'firstcycling_id': value, 'pcs_slug': slug}


def save_identity(conn, kind, values, *, identity_id):
    """Explicit maintainer correction; namespace contributions use merge_pair."""
    table = MODELS[kind].__table__
    values = identity_values(kind, values)
    conn.execute(text(f'LOCK TABLE {table.name} IN SHARE ROW EXCLUSIVE MODE'))
    existing = conn.execute(select(table.c.id).where(table.c.id == identity_id)).first()
    if existing is None:
        raise LookupError('Identity not found.')
    conditions = [table.c[key] == value for key, value in values.items() if value is not None]
    if conn.execute(select(table.c.id).where(table.c.id != identity_id, or_(*conditions))).first():
        raise ValueError('A source ID is already linked to another identity.')
    conn.execute(table.update().where(table.c.id == identity_id).values(
        **values, updated_at=datetime.now(timezone.utc)))
    return identity_id


def merge_pair(conn, kind, firstcycling_id, pcs_slug):
    """Merge compatible fragments, preserve established mappings on conflict."""
    values = identity_values(kind, {'firstcycling_id': firstcycling_id, 'pcs_slug': pcs_slug})
    if any(value is None for value in values.values()):
        raise ValueError('Namespace contributions require both FC and PCS IDs.')
    table = MODELS[kind].__table__
    conn.execute(text(f'LOCK TABLE {table.name} IN SHARE ROW EXCLUSIVE MODE'))
    rows = conn.execute(select(table).where(or_(
        table.c.firstcycling_id == values['firstcycling_id'], table.c.pcs_slug == values['pcs_slug'],
    ))).mappings().all()
    for row in rows:
        if any(row[key] is not None and row[key] != value for key, value in values.items()):
            raise ValueError('Conflicting FC/PCS mapping; the shared map was preserved for review.')
    if not rows:
        return conn.execute(table.insert().values(**values).returning(table.c.id)).scalar_one()
    winner = min(row['id'] for row in rows)
    if len(rows) > 1:
        conn.execute(table.delete().where(table.c.id.in_([row['id'] for row in rows if row['id'] != winner])))
    conn.execute(table.update().where(table.c.id == winner).values(
        **values, updated_at=datetime.now(timezone.utc)))
    return winner


def queue_pair(conn, kind, fc_id, pcs_slug):
    """Seed contribution; only a provider pair is stored in the namespace outbox."""
    values = identity_values(kind, {'firstcycling_id': fc_id, 'pcs_slug': pcs_slug})
    if any(value is None for value in values.values()):
        return
    from sqlalchemy.dialects.postgresql import insert
    conn.execute(insert(OUTBOX).values(entity_type=kind, is_seed=True, **values).on_conflict_do_update(
        constraint='uq_identity_sync_pair', set_={'is_seed': True, 'status': 'pending', 'error': None}))


def mark_identity_sync(conn):
    """Routes publish only after their namespace transaction has committed."""
    if has_request_context():
        g.identity_sync_url = conn.engine.url


def publish_or_defer(namespace_url):
    if has_request_context():
        g.identity_sync_url = namespace_url
        return {'deferred': True}
    return publish_namespace_pairs(namespace_url)


def publish_namespace_pairs(namespace_url, *, retry_conflicts=False):
    """At-least-once delivery: commit main before marking a local pair published.

    No namespace identifiers, CDB IDs, names, or status metadata enter main.
    Failures leave the durable outbox pending; replay is idempotent.
    """
    result = {'published': 0, 'conflicts': 0, 'superseded': 0, 'failed': False}
    namespace_engine = create_engine(namespace_url)
    try:
        with main_engine() as main:
            if namespace_engine.url.database == main.url.database:
                raise ValueError('Identity contributions require an isolated namespace database.')
            with namespace_engine.begin() as local:
                if local.execute(text('SELECT current_database()')).scalar_one() != namespace_engine.url.database:
                    raise ValueError('Wrong namespace database.')
                if not inspect(local).has_table(OUTBOX.name):
                    return result
                states = ('pending', 'conflict') if retry_conflicts else ('pending',)
                rows = local.execute(select(OUTBOX).where(OUTBOX.c.status.in_(states))
                                     .order_by(OUTBOX.c.id).with_for_update(skip_locked=True)).mappings().all()
                if not rows:
                    return result
                outcomes = []
                with main.begin() as shared:
                    for row in rows:
                        if not row['is_seed']:
                            from webapp.models.ns.cdb import Race, Rider
                            source = {'race': Race, 'rider': Rider}[row['entity_type']].__table__
                            query = select(source.c.id).where(
                                source.c.first_cycling_id == row['firstcycling_id'],
                                source.c.pcs_slug == row['pcs_slug'], source.c.is_deleted.is_(False),
                            )
                            if row['entity_type'] == 'rider':
                                query = query.where(source.c.pcs_slug_is_assumed.is_(False))
                            if local.execute(query.limit(1)).first() is None:
                                outcomes.append((row['id'], 'superseded', None))
                                continue
                        try:
                            with shared.begin_nested():
                                merge_pair(shared, row['entity_type'], row['firstcycling_id'], row['pcs_slug'])
                        except ValueError as exc:
                            outcomes.append((row['id'], 'conflict', str(exc)))
                        else:
                            outcomes.append((row['id'], 'published', None))
                # main has committed. A crash before this commit safely replays.
                for row_id, status, error in outcomes:
                    local.execute(OUTBOX.update().where(OUTBOX.c.id == row_id).values(
                        status=status, error=error,
                        published_at=datetime.now(timezone.utc) if status == 'published' else None))
                    result[{'published': 'published', 'conflict': 'conflicts', 'superseded': 'superseded'}[status]] += 1
    except (SQLAlchemyError, ValueError, OSError):
        logger.warning('Shared identity publication deferred; namespace outbox retained.', exc_info=True)
        result['failed'] = True
    finally:
        namespace_engine.dispose()
    return result
