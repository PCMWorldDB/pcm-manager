"""Read-only rider selection for Activity Monitor runs."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

from webapp.tools.activity_monitor_schedule import (
    apply_attempt_gates,
    due_from_new_activity,
)


def _rider_division_priority(rider):
    """Prefer higher-level teams when scheduled review dates are tied."""
    constant = (rider.get('division_constant') or '').strip().lower()
    compact = ''.join(character for character in constant if character.isalnum())
    if rider.get('team_id') == 119 or compact in {'fa', 'free', 'freeagent', 'freeagents'}:
        return 4
    if compact == 'wt' or 'worldtour' in compact:
        return 0
    if compact == 'pt' or 'protour' in compact:
        return 1
    if compact == 'ct' or 'continental' in compact:
        return 2
    if compact == 'u23' or 'under23' in compact:
        return 3
    return 5


def due_activity_riders(
    connection, as_of, limit=100, source_mode=None, include_upcoming=False,
    include_checked_today=False, count_through=None, rider_ids=None,
):
    """Return due riders, optionally followed by the next scheduled riders."""
    as_of = date.fromisoformat(as_of[:10]) if isinstance(as_of, str) else as_of
    amsterdam = ZoneInfo('Europe/Amsterdam')
    day_start = datetime.combine(as_of, time.min, tzinfo=amsterdam).astimezone(timezone.utc).replace(tzinfo=None)
    day_end = datetime.combine(
        as_of + timedelta(days=1), time.min, tzinfo=amsterdam
    ).astimezone(timezone.utc).replace(tzinfo=None)
    rider_filter = ''
    query_params = {
        'as_of': as_of.isoformat(),
        'day_start': day_start,
        'day_end': day_end,
    }
    query = text("""
        SELECT r.id AS monitor_rider_id,
               r.source_worlddb_rider_id AS worlddb_rider_id,
               r.first_cycling_id,
               (EXISTS (
                   SELECT 1 FROM activity_monitor_profile_links p
                   WHERE p.rider_id = r.id AND p.is_current
                     AND p.identity_generation = r.identity_generation
                     AND NULLIF(COALESCE(p.approved_profile_path, p.profile_path,
                                         p.override_url), '') IS NOT NULL
               )) AS has_pcs_link,
               r.first_name, r.last_name, r.team_name, r.team_id,
               d.constant AS division_constant,
               r.identity_generation,
               s.reference_date AS plan_date,
               s.schedule_data->>'next_research_date' AS next_research_date,
               s.schedule_data->>'research_deadline' AS research_deadline,
               s.schedule_data->>'research_reason' AS research_reason,
               s.schedule_data->>'latest_participation_date' AS latest_participation_date,
               activity.latest_activity_date,
               latest_check.last_check_date,
               NOT EXISTS (
                   SELECT 1 FROM activity_monitor_source_checks today_check
                   WHERE today_check.rider_id = r.id
                     AND today_check.identity_generation = r.identity_generation
                     AND today_check.checked_at >= :day_start
                     AND today_check.checked_at < :day_end
               ) AS not_checked_today,
               research.full_research_date,
               research.attempt_date,
               cycle.cycle_date, cycle.cycle_activity_date
        FROM activity_monitor_riders r
        CROSS JOIN LATERAL (
            SELECT s.rider_id, s.reference_date, s.recorded_at, s.schedule_data
            FROM activity_monitor_rider_schedules s
            WHERE s.rider_id = r.id
            ORDER BY s.reference_date DESC, s.recorded_at DESC, s.id DESC
            LIMIT 1
        ) s
        LEFT JOIN team t ON t.id = r.team_id
        LEFT JOIN division d ON d.id = t.division_id
        LEFT JOIN LATERAL (
            SELECT MAX(e.result_date) AS latest_activity_date
            FROM activity_monitor_results e
            WHERE e.rider_id = r.id
              AND e.identity_generation = r.identity_generation
              AND e.identity_verified
              AND (
                  e.participation_status IN ('confirmed_participation', 'confirmed')
                  OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
              )
              AND e.result_date <= :as_of
              AND (
                  lower(e.source) NOT IN ('pcs', 'pcs_html', 'procyclingstats')
                  OR EXISTS (
                      SELECT 1 FROM activity_monitor_source_checks c
                      WHERE c.id = e.source_check_id
                        AND lower(c.source) IN ('pcs', 'pcs_html', 'procyclingstats')
                        AND lower(replace(COALESCE(c.source_status, ''), '_', ' '))
                            IN ('success', 'results parsed', 'results table empty')
                        AND (
                            lower(COALESCE(c.request_path, '')) LIKE '%/road'
                            OR COALESCE(c.source_record_key, '') LIKE 'source_checks:%'
                        )
                  )
              )
        ) activity ON TRUE
        LEFT JOIN LATERAL (
            SELECT (c.checked_at AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Amsterdam')::date AS last_check_date
            FROM activity_monitor_source_checks c
            WHERE c.rider_id = r.id
              AND c.identity_generation = r.identity_generation
              AND c.checked_at < :day_end
            ORDER BY c.checked_at DESC, c.id DESC
            LIMIT 1
        ) latest_check ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(l.occurred_at::date) AS attempt_date,
                   MAX(l.occurred_at::date) FILTER (
                       WHERE l.record_data->>'check_quality' = 'complete'
                         AND l.record_data->>'race_basis' = activity.latest_activity_date::text
                   ) AS full_research_date
            FROM activity_monitor_legacy_records l
            WHERE l.rider_id = r.id
              AND l.record_type = 'research_findings'
              AND COALESCE(l.record_data->>'identity_generation', '0')::integer = r.identity_generation
              AND l.occurred_at::date <= :as_of
        ) research ON TRUE
        LEFT JOIN LATERAL (
            SELECT l.occurred_at::date AS cycle_date,
                   l.record_data->>'latest_participation_date' AS cycle_activity_date
            FROM activity_monitor_legacy_records l
            WHERE l.rider_id = r.id
              AND l.record_type = 'activity_source_cycle_history'
              AND COALESCE(l.record_data->>'identity_generation', '0')::integer = r.identity_generation
              AND l.occurred_at::date <= :as_of
            ORDER BY l.occurred_at DESC, l.id DESC
            LIMIT 1
        ) cycle ON TRUE
        WHERE r.eligible AND r.present_in_cdb AND NOT r.identity_hold
    """)
    if rider_ids is not None:
        normalized_rider_ids = sorted({int(rider_id) for rider_id in rider_ids})
        if not normalized_rider_ids:
            return 0, []
        rider_filter = ' AND r.id IN :rider_ids'
        query = text(str(query).replace(
            'WHERE r.eligible AND r.present_in_cdb AND NOT r.identity_hold',
            'WHERE r.eligible AND r.present_in_cdb AND NOT r.identity_hold'
            + rider_filter,
        )).bindparams(bindparam('rider_ids', expanding=True))
        query_params['rider_ids'] = normalized_rider_ids
    rows = connection.execute(query, query_params).mappings()

    due_rows = []
    due_count = 0
    count_through = (
        date.fromisoformat(count_through[:10])
        if isinstance(count_through, str) else count_through
    )
    for row in rows:
        item = dict(row)
        snapshot_basis = item.get('latest_participation_date')
        if isinstance(snapshot_basis, str):
            snapshot_basis = snapshot_basis.strip()
            if snapshot_basis:
                try:
                    snapshot_basis = date.fromisoformat(snapshot_basis[:10])
                except ValueError:
                    snapshot_basis = None
            else:
                snapshot_basis = None
        elif isinstance(snapshot_basis, datetime):
            snapshot_basis = snapshot_basis.date()
        latest_activity = item.get('latest_activity_date')
        if isinstance(latest_activity, str):
            try:
                latest_activity = date.fromisoformat(latest_activity[:10])
            except ValueError:
                latest_activity = None
        if latest_activity:
            recalculated = due_from_new_activity(
                latest_activity, as_of, item.get('team_id'),
                item.get('full_research_date'),
            )
            if recalculated:
                item.update(recalculated)
                item['latest_participation_date'] = latest_activity.isoformat()
                item['plan_recalculated'] = bool(
                    snapshot_basis is None or latest_activity > snapshot_basis
                    or item.get('full_research_date')
                )

        # The latest-check and source-attempt lookups have identical filters.
        item['source_attempt_date'] = item.get('last_check_date')
        attempt_dates = [
            value for value in (
                item.get('attempt_date'), item.get('source_attempt_date')
            ) if value
        ]
        latest_attempt = {'date': max(attempt_dates)} if attempt_dates else None
        source_attempt_date = item.get('last_check_date')
        cycle_date = item.get('cycle_date')
        if source_attempt_date and (not cycle_date or source_attempt_date >= cycle_date):
            latest_cycle = {
                'date': source_attempt_date,
                'latest_participation_date': latest_activity.isoformat() if latest_activity else '',
            }
        else:
            latest_cycle = ({
                'date': cycle_date,
                'latest_participation_date': item.get('cycle_activity_date') or '',
            } if cycle_date else None)
        apply_attempt_gates(
            item, as_of, item.get('team_id'),
            latest_attempt=latest_attempt,
            latest_cycle=latest_cycle,
            latest_activity=latest_activity,
        )
        item['plan_recalculated'] = bool(item.get('plan_recalculated'))
        next_date = item.get('next_research_date')
        is_due = bool(next_date and next_date <= as_of.isoformat())
        is_upcoming = bool(next_date and next_date > as_of.isoformat())
        if is_due or (include_upcoming and is_upcoming):
            if item.get('not_checked_today') or include_checked_today:
                item['is_due'] = is_due
                source_is_available = (
                    not source_mode
                    or (
                        bool(item.get('has_pcs_link'))
                        if source_mode == 'pcs_only' or item.get('team_id') == 119
                        else item.get('first_cycling_id') is not None
                        or bool(item.get('has_pcs_link'))
                    )
                )
                if count_through is not None:
                    if source_is_available and next_date and next_date[:10] <= count_through.isoformat():
                        due_count += 1
                elif source_is_available:
                    due_rows.append(item)

    if count_through is not None:
        return due_count, []

    due_rows.sort(key=lambda item: (
        item.get('next_research_date') or '',
        _rider_division_priority(item),
        item.get('worlddb_rider_id') or 0,
        item.get('monitor_rider_id') or 0,
    ))
    if source_mode:
        due_rows = [
            item for item in due_rows
            if (
                bool(item.get('has_pcs_link'))
                if source_mode == 'pcs_only' or item.get('team_id') == 119
                else item.get('first_cycling_id') is not None or bool(item.get('has_pcs_link'))
            )
        ]
    return len(due_rows), due_rows[:max(0, int(limit))]


def count_due_activity_riders(
    connection, as_of, through, include_upcoming=False, include_checked_today=False,
):
    """Count planned riders through a date without building a sorted rider list."""
    count, _ = due_activity_riders(
        connection,
        as_of,
        limit=0,
        include_upcoming=include_upcoming,
        include_checked_today=include_checked_today,
        count_through=through,
    )
    return count
