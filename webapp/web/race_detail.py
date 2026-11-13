"""Presentation adapters for race sources; CDB IDs remain namespace-local."""
from datetime import date
from types import SimpleNamespace


def cdb_race_view(race, stages, year):
    def stage_date(stage):
        try:
            return date(year, stage['month'], stage['day'])
        except (TypeError, ValueError):
            return None

    stage_rows = [SimpleNamespace(
        id=row['id'], stage_number=row['stage_number'], stage_date=stage_date(row),
        stage_name=row['constant'], stage_start_location=None, stage_end_location=None,
        stage_profile=None, stage_distance_km=None,
    ) for row in stages]
    dates = [row.stage_date for row in stage_rows if row.stage_date]
    return SimpleNamespace(
        race_id=race['first_cycling_id'], name=race['race_name'],
        category=None, classification=race['class_constant'],
        nation_name=race['country_constant'], description=None, profile=None,
        distance_km=None, start_date=min(dates) if dates else None,
        end_date=max(dates) if dates else None, start_location=None, end_location=None,
        stage_count=race['number_stages'], race_stages=stage_rows, race_results=[],
    )


def merge_race_view(fc_race, cdb_view):
    """Fill missing FC display facts without modifying persisted provider records."""
    names = ('race_id', 'name', 'category', 'classification', 'nation_name', 'description',
             'profile', 'distance_km', 'start_date', 'end_date', 'start_location',
             'end_location', 'stage_count', 'race_stages', 'race_results')
    return SimpleNamespace(**{
        name: (getattr(fc_race, name) if getattr(fc_race, name) is not None
               and getattr(fc_race, name) != '' else getattr(cdb_view, name))
        for name in names
    })


def pcs_result_scope(row, slug, year):
    """Match an exact PCS edition and classification, never a race name."""
    from urllib.parse import urlsplit

    path = urlsplit(row.race_url or '').path.strip('/').split('/')
    if path[:3] != ['race', slug, str(year)] or len(path) != 4:
        return None
    scope = path[3]
    if scope in ('result', 'gc'):
        return 'overall'
    if scope.startswith('stage-') and scope[6:].isdigit():
        return int(scope[6:])
    return None
