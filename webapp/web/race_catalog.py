"""Build a namespace race catalog using exact external identities only."""
from collections import Counter
from datetime import date
from types import SimpleNamespace


def unified_races(cdb_races, fc_races, identities, pcs_races=()):
    """One row per known identity, retaining unmatched and conflicting CDB rows.

    WDBO IDs must never be joined directly to arbitrary namespace-local IDs.
    FC editions supplied by the caller are already scoped to season and gender.
    """
    by_fc = {row.firstcycling_id: row for row in identities if row.firstcycling_id}
    by_pcs = {row.pcs_slug: row for row in identities if row.pcs_slug}
    rows = {}

    def empty(key, identity=None):
        return SimpleNamespace(
            id=key, race_id=None, name=(identity.pcs_slug or f'FC #{identity.firstcycling_id}') if identity else None,
            classification=None, nation_name=None, start_date=None, end_date=None,
            stage_count=None, race_stages=[], race_results=[],
            cdb_id=None, firstcycling_id=identity.firstcycling_id if identity else None,
            pcs_slug=identity.pcs_slug if identity else None,
            identity_id=identity.id if identity else None, mapping_conflict=False,
            pcs_race=False,
            search_names=[identity.pcs_slug] if identity and identity.pcs_slug else [],
        )

    for identity in identities:
        key = f'identity_{identity.id}'
        rows[key] = empty(key, identity)
    for race in fc_races:
        identity = by_fc.get(race.race_id)
        key = f'identity_{identity.id}' if identity else f'fc_{race.race_id}'
        row = rows.get(key) or empty(key)
        for name in ('name', 'classification', 'nation_name', 'start_date', 'end_date',
                     'stage_count', 'race_stages', 'race_results', 'race_id'):
            setattr(row, name, getattr(race, name))
        # Keep existing FC status/stage DOM keys compatible.
        row.id = race.id
        row.firstcycling_id = race.race_id
        row.search_names.append(race.name or '')
        rows[key] = row

    for race in pcs_races:
        identity = by_pcs.get(race.race_slug)
        key = f'identity_{identity.id}' if identity else f'pcs_{race.race_slug}'
        row = rows.get(key) or empty(key, identity)
        if row.race_id is None:
            for name in ('name', 'classification', 'nation_name', 'start_date', 'end_date', 'stage_count'):
                setattr(row, name, getattr(race, name))
        row.pcs_slug = race.race_slug
        row.pcs_race = True
        row.search_names.append(race.name)
        rows[key] = row

    targets = []
    for race in cdb_races:
        fc_identity = by_fc.get(race['first_cycling_id'])
        pcs_identity = by_pcs.get(race['pcs_slug'])
        conflict = bool(fc_identity and pcs_identity and fc_identity.id != pcs_identity.id)
        identity = fc_identity or pcs_identity
        if identity:
            conflict = conflict or bool(
                race['first_cycling_id'] and identity.firstcycling_id
                and race['first_cycling_id'] != identity.firstcycling_id
            ) or bool(race['pcs_slug'] and identity.pcs_slug and race['pcs_slug'] != identity.pcs_slug)
        key = (f'identity_{identity.id}' if identity else
               f"fc_{race['first_cycling_id']}" if race['first_cycling_id'] else
               f"pcs_{race['pcs_slug']}" if race['pcs_slug'] else f"cdb_{race['id']}")
        targets.append((race, key, conflict))
    counts = Counter(key for _, key, _ in targets)
    for race, key, conflict in targets:
        conflict = conflict or counts[key] > 1
        if conflict:
            if key in rows:
                rows[key].mapping_conflict = True
            key = f"cdb_{race['id']}"
        row = rows.get(key) or empty(key)
        row.cdb_id = race['id']
        row.search_names.append(race['race_name'] or '')
        row.mapping_conflict = conflict
        row.name = (race['race_name'] or row.name) if row.race_id is None and not row.pcs_race else row.name
        row.name = row.name or f"CDB race #{race['id']}"
        row.stage_count = row.stage_count if row.stage_count is not None else race['number_stages']
        row.firstcycling_id = row.firstcycling_id or race['first_cycling_id'] or None
        row.pcs_slug = row.pcs_slug or race['pcs_slug'] or None
        rows[key] = row
    return sorted(rows.values(), key=lambda row: (row.start_date or date.max, (row.name or '').casefold()))


def filter_races(races, filters, normalize):
    """Unknown edition dates stay visible instead of hiding CDB-only races."""
    result = []
    for race in races:
        if filters['race_name'] and not any(normalize(filters['race_name']) in normalize(name)
                                            for name in race.search_names):
            continue
        if filters['nation'] and race.nation_name != filters['nation']:
            continue
        if filters['is_stage_race'] and race.stage_count is not None:
            if (race.stage_count > 1) != (filters['is_stage_race'] == 'true'):
                continue
        for field in ('start_date_from', 'start_date_to'):
            try:
                boundary = date.fromisoformat(filters[field])
            except ValueError:
                continue
            value = race.start_date if field.endswith('from') else race.end_date or race.start_date
            if value and ((field.endswith('from') and value < boundary)
                          or (field.endswith('to') and value >= boundary)):
                break
        else:
            result.append(race)
    return result
