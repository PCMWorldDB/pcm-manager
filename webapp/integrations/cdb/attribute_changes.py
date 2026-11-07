"""Non-stat attribute proposals. Callers supply a verified namespace transaction.

User permissions are enforced by routes. This module never opens a connection
or commits, so proposal/event writes and approval/application remain atomic.
"""
from datetime import date, datetime, timezone
import math

from sqlalchemy import Boolean, Float, Integer, String, insert, select, update

from webapp.integrations.cdb.editability import get_editable_fields
from webapp.models.ns.cdb import CDB_TABLES, CDBChange, CDBChangeEvent


class AttributeConflict(ValueError):
    pass


def object_table(entity_type):
    for model in CDB_TABLES:
        if model.__tablename__ == entity_type:
            return model.__table__
    raise LookupError('This object has no mapped CDB table.')


def editable_attributes(entity_type):
    return sorted(field for field in get_editable_fields(entity_type) if not field.startswith('stat_'))


def serialize(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def parse_value(column, raw):
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


def _column(entity_type, attribute):
    table = object_table(entity_type)
    if attribute not in editable_attributes(entity_type):
        raise PermissionError('This attribute is read-only or belongs to the stats workflow.')
    return table, table.c[attribute]


def editor_fields(conn, entity_type, entity_id):
    table = object_table(entity_type)
    row = conn.execute(select(table).where(table.c.id == entity_id, table.c.is_deleted.is_(False))).mappings().first()
    if row is None:
        raise LookupError('Object not found.')
    changes = CDBChange.__table__
    pending = {change['attribute_name']: change for change in conn.execute(select(changes).where(
        changes.c.entity_type == entity_type, changes.c.entity_id == entity_id,
        changes.c.status == 'pending', changes.c.source == 'attribute_editor',
    )).mappings()}
    fields = []
    editable = editable_attributes(entity_type)
    for name in sorted(set(editable) | (set(pending) & set(table.c.keys()))):
        if name.startswith('stat_'):
            continue
        value = serialize(row[name])
        if name == 'birth_date' and value:
            try:
                value = datetime.strptime(value, '%Y%m%d').strftime('%Y-%m-%d')
            except ValueError:
                value = ''
        fields.append({'name': name, 'label': name.replace('_', ' ').title(),
                       'value': value, 'type': 'date' if name == 'birth_date' else 'text',
                       'editable': name in editable, 'pending': pending.get(name)})
    return fields


def propose(conn, entity_type, entity_id, attribute, raw_value, actor_id):
    table, column = _column(entity_type, attribute)
    row = conn.execute(select(table).where(table.c.id == entity_id, table.c.is_deleted.is_(False))
                       .with_for_update()).mappings().first()
    if row is None:
        raise LookupError('Object not found.')
    value = parse_value(column, raw_value)
    before, after = serialize(row[attribute]), serialize(value)
    if before == after:
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
        source='attribute_editor', created_by_id=actor_id).returning(changes.c.id)).scalar_one()
    conn.execute(insert(CDBChangeEvent.__table__).values(
        change_id=change_id, event_type='created', actor_id=actor_id))
    return change_id


def review_queue(conn, entity_type):
    changes = CDBChange.__table__
    return conn.execute(select(changes).where(
        changes.c.entity_type == entity_type, changes.c.source == 'attribute_editor',
        changes.c.status == 'pending', ~changes.c.attribute_name.startswith('stat_'),
    ).order_by(changes.c.created_at, changes.c.id).limit(200)).mappings().all()


def history_events(conn, entity_type):
    changes, events = CDBChange.__table__, CDBChangeEvent.__table__
    return conn.execute(select(
        changes.c.entity_type, changes.c.entity_id, changes.c.attribute_name,
        changes.c.before_value, changes.c.after_value, changes.c.source, changes.c.status,
        events.c.id, events.c.event_type, events.c.actor_id, events.c.occurred_at,
    ).select_from(events.join(changes, events.c.change_id == changes.c.id)).where(
        changes.c.entity_type == entity_type, ~changes.c.attribute_name.startswith('stat_'),
    ).order_by(events.c.occurred_at.desc(), events.c.id.desc()).limit(200)).mappings().all()


def decide(conn, entity_type, change_id, action, actor_id):
    if action not in ('approve', 'reject'):
        raise ValueError('Invalid review action.')
    changes = CDBChange.__table__
    change = conn.execute(select(changes).where(
        changes.c.id == change_id, changes.c.entity_type == entity_type,
        changes.c.source == 'attribute_editor').with_for_update()).mappings().first()
    if change is None:
        raise LookupError('Proposal not found.')
    if change['status'] != 'pending' or change['applied_at'] is not None:
        raise AttributeConflict('This proposal has already been reviewed.')
    if change['attribute_name'].startswith('stat_') or change['operation'] != 'update':
        raise PermissionError('This change belongs to a different workflow.')
    now = datetime.now(timezone.utc)
    events = CDBChangeEvent.__table__
    if action == 'reject':
        conn.execute(update(changes).where(changes.c.id == change_id).values(status='rejected'))
        conn.execute(insert(events).values(change_id=change_id, event_type='rejected', actor_id=actor_id))
        return
    table, column = _column(entity_type, change['attribute_name'])
    row = conn.execute(select(table).where(table.c.id == change['entity_id'], table.c.is_deleted.is_(False))
                       .with_for_update()).mappings().first()
    if row is None:
        raise LookupError('Object not found.')
    current = serialize(row[column.name])
    if current not in (change['before_value'], change['after_value']):
        raise AttributeConflict('The current value changed. Reject this proposal and submit a fresh one.')
    value = parse_value(column, change['after_value'])
    conn.execute(update(table).where(table.c.id == change['entity_id']).values({column.name: value}))
    conn.execute(update(changes).where(changes.c.id == change_id).values(
        status='approved', approved_by_id=actor_id, approved_at=now, applied_at=now))
    conn.execute(insert(events), [
        {'change_id': change_id, 'event_type': 'approved', 'actor_id': actor_id, 'context': {}},
        {'change_id': change_id, 'event_type': 'applied', 'actor_id': actor_id,
         'context': {'actual_before_value': current}},
    ])
