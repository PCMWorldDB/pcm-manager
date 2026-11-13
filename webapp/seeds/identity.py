"""Namespace-local seed application and provider-only shared contributions."""
from sqlalchemy import case, func, or_
import yaml

from webapp.integrations.identity import identity_values, queue_pair


def seed_namespace(connection, table, path, kind, *, fill_blanks=False, assumed_slug_ids=()):
    with path.open(encoding='utf-8') as source:
        entries = yaml.safe_load(source)[f'{kind}s']
    if not isinstance(entries, list):
        raise ValueError('Identity seeds must contain a list.')
    seen = set()
    count = 0
    for entry in entries:
        allowed = {f'{kind}_id', f'{kind}_name', 'firstcycling_id', 'pcs_slug'}
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError('Invalid identity seed fields.')
        record_id = entry.get(f'{kind}_id')
        if record_id is not None:
            if type(record_id) is not int or record_id <= 0 or record_id in seen:
                raise ValueError(f'Invalid or duplicate {kind}_id: {record_id}')
            seen.add(record_id)
        fc_id, slug = entry.get('firstcycling_id'), entry.get('pcs_slug')
        if fc_id is not None and type(fc_id) is not int:
            raise ValueError('Invalid firstcycling_id in identity seed.')
        if fc_id is not None or slug is not None:
            normalized = identity_values(kind, {'firstcycling_id': fc_id, 'pcs_slug': slug})
            fc_id, slug = normalized['firstcycling_id'], normalized['pcs_slug']
        # Complete source links are useful even when the CDB object is absent.
        if fc_id is not None and slug is not None:
            queue_pair(connection, kind, fc_id, slug)
        if record_id is None:
            continue
        values = {key: value for key, value in (
            ('first_cycling_id', fc_id), ('pcs_slug', slug),
        ) if value is not None}
        if fill_blanks:
            missing = {
                'first_cycling_id': or_(table.c.first_cycling_id.is_(None), table.c.first_cycling_id == 0),
                'pcs_slug': or_(table.c.pcs_slug.is_(None), func.trim(table.c.pcs_slug).in_(('', '0')),
                                record_id in assumed_slug_ids),
            }
            values = {key: case((missing[key], value), else_=table.c[key])
                      for key, value in values.items()}
            if slug is not None:
                values['pcs_slug_is_assumed'] = case((missing['pcs_slug'], False),
                                                    else_=table.c.pcs_slug_is_assumed)
        if values:
            count += connection.execute(table.update().where(
                table.c.id == record_id, table.c.is_deleted.is_(False),
            ).values(**values)).rowcount
    return count
