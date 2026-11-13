"""Non-stat attribute proposals. Callers supply a verified namespace transaction.

User permissions are enforced by routes. This module never opens a connection
or commits, so proposal/event writes and approval/application remain atomic.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
import math

from sqlalchemy import Boolean, Float, Integer, String, insert, select, update

from webapp.integrations.cdb.editability import get_editable_fields
from webapp.integrations.cdb.field_configuration import can_edit_column, field_slug, stored_fields
from webapp.integrations.cdb.rider_state import PCS_IDENTITY_RESET, pcs_dob_identity_status, refresh_pending_flags
from webapp.models.ns.cdb import CDB_TABLES, CDBChange, CDBChangeEvent
from webapp.models.ns.base import RiderListItem
from webapp.integrations.identity import mark_identity_sync


class AttributeConflict(ValueError):
    pass


def object_table(entity_type):
    for model in CDB_TABLES:
        if model.__tablename__ == entity_type:
            return model.__table__
    raise LookupError('This object has no mapped CDB table.')


def editable_attributes(entity_type, configuration=None):
    fields = set(get_editable_fields(entity_type))
    for (kind, name), config in (configuration or {}).items():
        if kind == entity_type:
            fields.discard(name)
            if config['is_editable'] and can_edit_column(entity_type, name):
                fields.add(name)
    return sorted(field for field in fields if not field.startswith('stat_'))


def serialize(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def parse_value(column, raw):
    if column.name in ('pcs_slug', 'first_cycling_id'):
        from webapp.integrations.identity import identity_values
        if raw in (None, ''):
            return None
        key = 'firstcycling_id' if column.name == 'first_cycling_id' else 'pcs_slug'
        return identity_values('rider', {key: raw})[key]
    if raw == '' and not isinstance(column.type, String):
        raw = None
    if raw is None:
        if not column.nullable:
            raise ValueError('This attribute cannot be empty.')
        return None
    if not isinstance(raw, str):
        raise ValueError('Attribute values must be strings.')
    if column.name == 'birth_date':
        try:
            parsed = datetime.strptime(raw, '%Y-%m-%d').date() if '-' in raw else datetime.strptime(raw, '%Y%m%d').date()
        except ValueError as exc:
            raise ValueError('Enter a valid birth date.') from exc
        if parsed > date.today():
            raise ValueError('Birth date cannot be in the future.')
        return int(parsed.strftime('%Y%m%d'))
    if isinstance(column.type, Boolean):
        if raw not in ('true', 'false'):
            raise ValueError('Choose true or false.')
        return raw == 'true'
    if isinstance(column.type, Integer):
        value = int(raw)
        if not -(2**31) <= value < 2**31:
            raise ValueError('Integer is outside the supported range.')
        return value
    if isinstance(column.type, Float):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError('Enter a finite number.')
        return value
    if isinstance(column.type, String):
        if column.type.length and len(raw) > column.type.length:
            raise ValueError('Value is too long.')
        return raw
    raise ValueError('Unsupported attribute type.')


def _column(entity_type, attribute, configuration):
    table = object_table(entity_type)
    if attribute not in editable_attributes(entity_type, configuration):
        raise PermissionError('This attribute is read-only or belongs to the stats workflow.')
    return table, table.c[attribute]


def editor_fields(conn, entity_type, entity_id, authorize=None):
    fields = editor_fields_by_id(conn, entity_type, [entity_id], authorize)
    if entity_id not in fields:
        raise LookupError('Object not found.')
    return fields[entity_id]


def editor_fields_by_id(conn, entity_type, entity_ids, authorize=None):
    if not entity_ids:
        return {}
    table = object_table(entity_type)
    rows = conn.execute(select(table).where(table.c.id.in_(entity_ids), table.c.is_deleted.is_(False))).mappings()
    changes = CDBChange.__table__
    pending_by_id = defaultdict(dict)
    for change in conn.execute(select(changes).where(
        changes.c.entity_type == entity_type, changes.c.entity_id.in_(entity_ids),
        changes.c.status == 'pending', changes.c.source == 'attribute_editor',
    )).mappings():
        pending_by_id[change['entity_id']][change['attribute_name']] = change
    configuration = stored_fields(conn, entity_type)
    editable = editable_attributes(entity_type, configuration)
    fields_by_id = {}
    for row in rows:
        pending = pending_by_id[row['id']]
        fields = []
        for name in sorted(set(editable) | (set(pending) & set(table.c.keys()))):
            if name.startswith('stat_'):
                continue
            value = serialize(row[name])
            if name == 'birth_date' and value:
                try:
                    value = datetime.strptime(value, '%Y%m%d').strftime('%Y-%m-%d')
                except ValueError:
                    value = ''
            label = name.replace('_', ' ').title()
            if name.startswith('pcs_'):
                label = 'PCS ' + label[4:]
            policy = configuration.get((entity_type, name), {})
            slug = policy.get('slug', field_slug(entity_type, name))
            fields.append({'name': name, 'label': label,
                           'value': value, 'type': 'date' if name == 'birth_date' else 'text',
                           'editable': name in editable and (authorize is None or authorize(slug, 'update')),
                           'requires_approval': policy.get('is_require_approval', True),
                           'can_approve': authorize is None or authorize(slug, 'approve'),
                           'pending': pending.get(name)})
        fields_by_id[row['id']] = fields
    return fields_by_id


def propose(conn, entity_type, entity_id, attribute, raw_value, actor_id, *, stat_list_id=None, authorize=None):
    configuration = stored_fields(conn, entity_type, lock=True)
    table, column = _column(entity_type, attribute, configuration)
    policy = configuration.get((entity_type, attribute), {})
    if authorize is not None and not authorize(policy.get('slug', field_slug(entity_type, attribute)), 'update'):
        raise PermissionError('Field update permission required.')
    row = conn.execute(select(table).where(table.c.id == entity_id, table.c.is_deleted.is_(False))
                       .with_for_update()).mappings().first()
    if row is None:
        raise LookupError('Object not found.')
    value = parse_value(column, raw_value)
    before, after = serialize(row[attribute]), serialize(value)
    confirming_assumed_slug = (entity_type == 'rider' and attribute == 'pcs_slug'
                               and row['pcs_slug_is_assumed'] and value is not None)
    if before == after and not confirming_assumed_slug:
        raise ValueError('The proposed value matches the current value.')
    changes = CDBChange.__table__
    existing = conn.execute(select(changes.c.id).where(
        changes.c.entity_type == entity_type, changes.c.entity_id == entity_id,
        changes.c.attribute_name == attribute, changes.c.status == 'pending',
        changes.c.source == 'attribute_editor')).first()
    if existing:
        raise AttributeConflict('This attribute already has a pending proposal. Review it first.')
    change_id = conn.execute(insert(changes).values(
        entity_type=entity_type, entity_id=entity_id, attribute_name=attribute,
        operation='update', before_value=before, after_value=after,
        source='attribute_editor', created_by_id=actor_id, stat_list_id=stat_list_id).returning(changes.c.id)).scalar_one()
    conn.execute(insert(CDBChangeEvent.__table__).values(
        change_id=change_id, event_type='submitted', actor_id=actor_id))
    if not policy.get('is_require_approval', True):
        decide(conn, entity_type, change_id, 'approve', actor_id, immediate=True)
    elif entity_type == 'rider':
        refresh_pending_flags(conn, [entity_id])
    return change_id


def review_queue(conn, entity_type):
    changes = CDBChange.__table__
    return conn.execute(select(changes).where(
        changes.c.entity_type == entity_type, changes.c.source == 'attribute_editor',
        changes.c.status == 'pending', ~changes.c.attribute_name.startswith('stat_'),
    ).order_by(changes.c.created_at, changes.c.id).limit(200)).mappings().all()


def history_events(conn, entity_type, applied_entity_id=None):
    changes, events = CDBChange.__table__, CDBChangeEvent.__table__
    query = select(
        changes.c.entity_type, changes.c.entity_id, changes.c.attribute_name,
        changes.c.before_value, changes.c.after_value, changes.c.source, changes.c.status,
        events.c.id, events.c.event_type, events.c.actor_id, events.c.occurred_at,
    ).select_from(events.join(changes, events.c.change_id == changes.c.id)).where(
        changes.c.entity_type == entity_type, ~changes.c.attribute_name.startswith('stat_'),
    )
    if applied_entity_id is not None:
        query = query.where(changes.c.entity_id == applied_entity_id, changes.c.applied_at.is_not(None))
    return conn.execute(
        query.order_by(events.c.occurred_at.desc(), events.c.id.desc()).limit(200)
    ).mappings().all()


def pending_list_changes(conn, list_id, *, lock=False):
    changes = CDBChange.__table__
    query = select(changes).where(
        changes.c.stat_list_id == list_id, changes.c.entity_type == 'rider',
        changes.c.source == 'attribute_editor', changes.c.status == 'pending',
    ).order_by(changes.c.entity_id, changes.c.id)
    if lock:
        query = query.with_for_update()
    return conn.execute(query).mappings().all()


def approve_list(conn, list_id, actor_id, *, authorize):
    """Apply a list's pending proposals in the caller's locked list transaction."""
    pending = pending_list_changes(conn, list_id, lock=True)
    if not pending:
        return 0
    members = set(conn.execute(select(RiderListItem.rider_id).where(
        RiderListItem.rider_list_id == list_id,
    )).scalars())
    rider_ids = {change['entity_id'] for change in pending}
    if not rider_ids <= members:
        raise AttributeConflict('A rider with pending changes was removed from this list. Add the rider back before approving.')
    # Lock shared riders in a stable order before applying any proposal.
    riders = object_table('rider')
    conn.execute(select(riders.c.id).where(riders.c.id.in_(rider_ids))
                 .order_by(riders.c.id).with_for_update()).all()
    for change in pending:
        decide(conn, 'rider', change['id'], 'approve', actor_id,
               authorize=authorize, stat_list_id=list_id)
    return len(pending)


def decide(conn, entity_type, change_id, action, actor_id, *, authorize=None, immediate=False, stat_list_id=None):
    if action not in ('approve', 'reject'):
        raise ValueError('Invalid review action.')
    changes = CDBChange.__table__
    change = conn.execute(select(changes).where(
        changes.c.id == change_id, changes.c.entity_type == entity_type,
        changes.c.source == 'attribute_editor').with_for_update()).mappings().first()
    if change is None:
        raise LookupError('Proposal not found.')
    configuration = stored_fields(conn, entity_type, lock=True)
    name = change['attribute_name']
    policy = configuration.get((entity_type, name), {})
    if authorize is not None and not authorize(policy.get('slug', field_slug(entity_type, name)), 'approve'):
        raise PermissionError('Field approval permission required.')
    if change['status'] != 'pending' or change['applied_at'] is not None:
        raise AttributeConflict('This proposal has already been reviewed.')
    if (action == 'approve' and not immediate and change['stat_list_id'] is not None
            and change['stat_list_id'] != stat_list_id):
        raise AttributeConflict('Approve this change together with its Attribute List in the Bulk Editor.')
    if change['attribute_name'].startswith('stat_') or change['operation'] != 'update':
        raise PermissionError('This change belongs to a different workflow.')
    now = datetime.now(timezone.utc)
    events = CDBChangeEvent.__table__
    if action == 'reject':
        conn.execute(update(changes).where(changes.c.id == change_id).values(status='rejected'))
        conn.execute(insert(events).values(change_id=change_id, event_type='rejected', actor_id=actor_id))
        if entity_type == 'rider':
            refresh_pending_flags(conn, [change['entity_id']])
        return
    table, column = _column(entity_type, name, configuration)
    row = conn.execute(select(table).where(table.c.id == change['entity_id'], table.c.is_deleted.is_(False))
                       .with_for_update()).mappings().first()
    if row is None:
        raise LookupError('Object not found.')
    current = serialize(row[column.name])
    if current not in (change['before_value'], change['after_value']):
        raise AttributeConflict('The current value changed. Reject this proposal and submit a fresh one.')
    value = parse_value(column, change['after_value'])
    values = {column.name: value}
    if entity_type == 'rider' and column.name == 'pcs_slug':
        values['pcs_slug_is_assumed'] = False
    if entity_type == 'rider' and current != change['after_value']:
        if column.name == 'pcs_slug':
            values.update(PCS_IDENTITY_RESET, pcs_identity_verified_override=False)
        elif column.name == 'birth_date':
            if row['pcs_birth_date'] is None:
                values['pcs_identity_status'] = 'unknown'
            elif row['pcs_identity_status'] in ('verified', 'review'):
                values['pcs_identity_status'] = pcs_dob_identity_status(value, row['pcs_birth_date'])
    conn.execute(update(table).where(table.c.id == change['entity_id']).values(values))
    if entity_type in ('race', 'rider') and column.name in ('first_cycling_id', 'pcs_slug'):
        mark_identity_sync(conn)
    conn.execute(update(changes).where(changes.c.id == change_id).values(
        status='approved', approved_by_id=None if immediate else actor_id,
        approved_at=None if immediate else now, applied_at=now))
    conn.execute(insert(events).values(
        change_id=change_id, event_type='applied' if immediate else 'approved', actor_id=actor_id,
        context={'actual_before_value': current}))
    if entity_type == 'rider':
        refresh_pending_flags(conn, [change['entity_id']])
