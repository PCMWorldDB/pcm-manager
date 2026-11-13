"""Read-only views for the namespace CDB rider roster."""
from sqlalchemy import text


RIDER_VIEWS = {
    'stats': 'Stats',
    'activity': 'Activity',
    'attributes': 'Attributes',
    'identity': 'Identity',
}

IDENTITY_FILTERS = {
    '': 'All flags',
    'verified': 'PCS identity verified',
    'review': 'PCS identity needs review',
    'failed': 'PCS identity error',
    'unknown': 'PCS identity unknown',
    'override': 'Verified by manual override',
    'missing_fc': 'Missing FirstCycling ID',
}

IDENTITY_REVIEW_FILTERS = {key: label for key, label in IDENTITY_FILTERS.items() if key}
DEFAULT_IDENTITY_REVIEW_FLAGS = ('review', 'failed', 'unknown', 'missing_fc')


def identity_flag_condition(flag):
    """Match the effective identity flag, including DOB changes and overrides."""
    override = "COALESCE((to_jsonb(c)->>'pcs_identity_verified_override')::boolean, FALSE)"
    if flag == 'missing_fc':
        return 'COALESCE(c.first_cycling_id, 0) = 0'
    if flag == 'override':
        return override
    status = """(CASE
        WHEN COALESCE(to_jsonb(c)->>'pcs_identity_status', 'unknown') IN ('verified', 'review')
        THEN CASE WHEN REPLACE(c.birth_date::text, '-', '') = REPLACE(to_jsonb(c)->>'pcs_birth_date', '-', '')
                  THEN 'verified' ELSE 'review' END
        ELSE COALESCE(to_jsonb(c)->>'pcs_identity_status', 'unknown') END)"""
    return f'NOT {override} AND {status} = :identity_flag'


ACTIVITY_FILTERS = {
    '': 'All activity',
    'untracked': 'No monitor record',
    'awaiting': 'Awaiting first check',
    'checked': 'Has check history',
    'no_race': 'No recorded race evidence',
    'recent': 'Raced in the last 90 days',
    'inactive': 'Last race over 90 days ago',
}


def activity_sql(conn):
    """Join only current monitor identities; never provision missing history."""
    ready = conn.execute(text("""
        SELECT to_regclass('activity_monitor_riders') IS NOT NULL
           AND to_regclass('activity_monitor_source_checks') IS NOT NULL
           AND to_regclass('activity_monitor_results') IS NOT NULL
    """)).scalar_one()
    if not ready:
        return """LEFT JOIN LATERAL (
            SELECT NULL::integer AS monitor_id, NULL::timestamp AS latest_check_at,
                   NULL::text AS latest_check_status, NULL::date AS latest_result_date,
                   NULL::text AS latest_race_name, FALSE AS activity_available
        ) activity ON TRUE"""
    return """LEFT JOIN LATERAL (
        SELECT r.id AS monitor_id, latest_check.checked_at AS latest_check_at,
               latest_check.source_status AS latest_check_status,
               latest_result.result_date AS latest_result_date,
               latest_result.race_name AS latest_race_name, TRUE AS activity_available
        FROM activity_monitor_riders r
        LEFT JOIN LATERAL (
            SELECT checked_at, source_status FROM activity_monitor_source_checks
            WHERE rider_id = r.id AND identity_generation = r.identity_generation
            ORDER BY checked_at DESC, id DESC LIMIT 1
        ) latest_check ON TRUE
        LEFT JOIN LATERAL (
            SELECT e.result_date, e.race_name FROM activity_monitor_results e
            WHERE e.rider_id = r.id AND e.identity_generation = r.identity_generation
              AND e.identity_verified AND e.result_date <= CURRENT_DATE
              AND (e.participation_status IN ('confirmed_participation', 'confirmed')
                   OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ')
              AND (
                  LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'pcs_html', 'procyclingstats')
                  OR EXISTS (
                      SELECT 1 FROM activity_monitor_source_checks pcs_check
                      WHERE pcs_check.id = e.source_check_id AND pcs_check.rider_id = r.id
                        AND pcs_check.identity_generation = r.identity_generation
                        AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                        AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                            IN ('success', 'results parsed', 'results table empty')
                        AND (regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                             OR pcs_check.source_record_key LIKE 'source_checks:%')
                  )
              )
            ORDER BY e.result_date DESC, e.id DESC LIMIT 1
        ) latest_result ON TRUE
        WHERE r.source_worlddb_rider_id = c.id AND r.present_in_cdb
        ORDER BY r.identity_generation DESC, r.id DESC LIMIT 1
    ) activity ON TRUE"""


ACTIVITY_CONDITIONS = {
    'untracked': 'activity.monitor_id IS NULL',
    'awaiting': 'activity.monitor_id IS NOT NULL AND activity.latest_check_at IS NULL',
    'checked': 'activity.latest_check_at IS NOT NULL',
    'no_race': 'activity.monitor_id IS NOT NULL AND activity.latest_result_date IS NULL',
    'recent': "activity.latest_result_date >= CURRENT_DATE - 90",
    'inactive': "activity.latest_result_date < CURRENT_DATE - 90",
}


def filter_snapshot(riders, filters, normalize):
    """Apply the same filters to every column view, before pagination."""
    query = normalize(filters.get('q'))
    team = normalize(filters.get('team'))

    def matches(rider):
        if query and query not in normalize(rider.search_name) and query not in (
                str(rider.rider_id), str(rider.first_cycling_id)):
            return False
        if team and team not in normalize(rider.search_team):
            return False
        for key in ('country', 'division'):
            if filters.get(key) and str(getattr(rider, key + '_id')) != filters[key]:
                return False
        if filters.get('stat_key'):
            value = getattr(rider, 'stat_' + filters['stat_key'], None)
            for key, below in (('stat_min', True), ('stat_max', False)):
                if filters.get(key) and (value is None or (
                        value < int(filters[key]) if below else value > int(filters[key]))):
                    return False
        flag = filters.get('identity_flag')
        if flag == 'missing_fc':
            if rider.first_cycling_id:
                return False
        elif flag == 'override':
            if not rider.pcs_identity_verified_override:
                return False
        elif flag and (rider.pcs_identity_verified_override or rider.pcs_identity_status != flag):
            return False
        activity = filters.get('activity_status')
        days = rider.days_since_last_race
        checks = {
            'untracked': rider.monitor_id is None,
            'awaiting': rider.monitor_id is not None and rider.latest_check_at is None,
            'checked': rider.latest_check_at is not None,
            'no_race': rider.monitor_id is not None and days is None,
            'recent': days is not None and days <= 90,
            'inactive': days is not None and days > 90,
        }
        return not activity or checks.get(activity, False)

    return [rider for rider in riders if matches(rider)]
