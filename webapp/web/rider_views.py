"""Read-only views for the namespace CDB rider roster."""
from sqlalchemy import text


RIDER_VIEWS = {
    'stats': 'Stats',
    'activity': 'Activity',
    'attributes': 'Attribute',
}


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
