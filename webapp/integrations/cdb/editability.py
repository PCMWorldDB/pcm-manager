"""Shared field policy for manual CDB edits, independent of user permissions."""
from pathlib import Path

import yaml

from webapp.models.ns.cdb import CDB_TABLES


SYSTEM_FIELDS = {
    'is_deleted', 'pcs_identity_status', 'has_pending_stat_changes', 'has_pending_attribute_changes',
    'pcs_birth_date', 'pcs_latest_updated_at',
}


def get_editable_fields(entity_type):
    """Return the explicit allowlist; invalid configuration fails closed.

    This policy does not authorize a user or govern CDB import synchronization.
    Consumers must still enforce resource/action permissions and typed values.
    """
    with Path(__file__).with_name('editable_fields.yaml').open(encoding='utf-8') as stream:
        policy = yaml.safe_load(stream)
    if not isinstance(policy, dict):
        raise ValueError('CDB editable-field policy must be an object')
    models = {model.__tablename__: model for model in CDB_TABLES}
    for object_name, fields in policy.items():
        if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
            raise ValueError(f'Invalid editable fields for {object_name}')
        model = models.get(object_name)
        if model is None:
            if object_name != 'race' or fields:
                raise ValueError(f'No mapped CDB model for {object_name}')
            continue
        for field in fields:
            column = model.__table__.columns.get(field)
            if column is None or column.primary_key or field in SYSTEM_FIELDS:
                raise ValueError(f'Invalid editable field: {object_name}.{field}')
    return frozenset(policy.get(entity_type, ()))


def is_field_editable(entity_type, attribute_name):
    """Unknown objects and fields are read-only."""
    return attribute_name in get_editable_fields(entity_type)
