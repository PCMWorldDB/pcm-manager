"""Web routes for UI"""
import hashlib
import html
import logging
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, abort, current_app, session, make_response, send_file
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine, text, cast, Integer, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from webapp.web import web_bp
from webapp.web.forms import LoginForm, NamespaceForm, UserCreateForm, PasswordChangeForm, PasswordResetForm
from webapp.models import User, Resource, Namespace, Role, Release, AuditLog, AuditAction, FirstCyclingRace, ExternalSyncLog
from webapp.integrations.firstcycling import FirstCyclingClient, FirstCyclingAPIError
from webapp.models.main.firstcycling import FirstCyclingRider, FirstCyclingTeam
from webapp.models.main.firstcycling import FirstCyclingRaceStage
from webapp.models.ns.schema import get_field_mapping
from webapp.extensions import db
from webapp.tasks import run_external_sync_task
from functools import wraps

# Get logger for this module
logger = logging.getLogger(__name__)

STAT_DELTA_FIELDS = (
    'mo',
    'mm',
    'dh',
    'hil',
    'tt',
    'prl',
    'fla',
    'spr',
    'acc',
    'end',
    'res',
    'rec',
    'cob',
    'att',
)

STAT_MIN_VALUE = 50
STAT_MAX_VALUE = 85

EXTERNAL_SYNC_TYPE_LABELS = {
    'all': 'All Syncs',
    'fc_race_calendar': 'Race Calendar',
    'fc_teams': 'FC Teams',
    'fc_riders': 'FC Riders',
    'fc_race_results': 'Race Results',
}

EXTERNAL_SYNC_RUN_TYPES = ('fc_race_calendar', 'fc_teams', 'fc_riders', 'fc_race_results')


def _external_sync_type_display(sync_type):
    return EXTERNAL_SYNC_TYPE_LABELS.get(sync_type, sync_type)


def _record_external_sync_log(
    *,
    provider,
    sync_type,
    status,
    executed_by_id,
    season_year=None,
    gender=None,
    requested_count=None,
    synced_count=None,
    failed_count=None,
    message=None,
    details=None,
):
    details_json = None
    if details is not None:
        details_json = json.dumps(details)

    row = ExternalSyncLog(
        provider=provider,
        sync_type=sync_type,
        season_year=season_year,
        gender=gender,
        status=status,
        requested_count=requested_count,
        synced_count=synced_count,
        failed_count=failed_count,
        executed_by_id=executed_by_id,
        message=message,
        details_json=details_json,
    )
    db.session.add(row)
    return row


def _slugify_namespace_part(value):
    text_value = (value or '').strip().lower()
    text_value = re.sub(r'[^a-z0-9\s-]', '', text_value)
    text_value = re.sub(r'[\s-]+', '-', text_value)
    return text_value.strip('-')

    
def _resolve_external_sync_season(seasons, year_value, gender_value):
    """Resolve selected season from params, falling back to configured default."""
    if not seasons:
        return None

    selected_year = _safe_int(year_value)
    selected_gender = (gender_value or '').strip()

    if selected_year is not None and selected_gender:
        for season in seasons:
            if season['year'] == selected_year and season['gender'] == selected_gender:
                return season

    return _resolve_default_season(seasons)


def _user_resources():
    if current_user.is_superadmin:
        return Resource.query.all()

    resources = []
    for role in current_user.roles:
        for permission in role.permissions:
            if permission.resource not in resources:
                resources.append(permission.resource)
    return resources


def _can_access_panel(panel_name):
    if current_user.is_superadmin:
        return True

    for resource in _user_resources():
        if resource.app_panel == panel_name:
            return True
    return False


def _get_panel_url(app_panel):
    panel_routes = {
        'stat_manager': 'web.stats_editor',
        'release_manager': 'web.releases',
        'team_manager': 'web.teams',
    }

    endpoint = panel_routes.get(app_panel)
    if not endpoint:
        return None
    return url_for(endpoint)


def _get_panel_display_name(app_panel):
    """Return user-facing panel name."""
    names = {
        'stat_manager': 'Stats Editor',
        'team_manager': 'Teams',
        'release_manager': 'Releases',
    }
    if app_panel in names:
        return names[app_panel]

    default_name = app_panel.replace('_', ' ').title()
    if default_name.endswith(' Manager'):
        default_name = default_name[:-8]

    if not default_name.endswith('s'):
        default_name = f'{default_name}s'

    return default_name


def _get_panel_menu_items():
    """Return all known app panels with access metadata for global navigation."""
    all_panel_names = {
        row[0]
        for row in db.session.query(Resource.app_panel).distinct().all()
        if row and row[0] and row[0] != 'admin'
    }

    if current_user.is_superadmin:
        accessible_panels = set(all_panel_names)
    else:
        accessible_panels = {
            resource.app_panel
            for resource in _user_resources()
            if resource.app_panel and resource.app_panel != 'admin'
        }

    panel_items = []
    for panel_name in sorted(all_panel_names):
        panel_url = _get_panel_url(panel_name)
        can_access = panel_name in accessible_panels
        panel_items.append({
            'name': panel_name,
            'display_name': _get_panel_display_name(panel_name),
            'icon': get_app_icon(panel_name),
            'url': panel_url,
            'can_access': can_access,
            'is_clickable': bool(panel_url) and can_access,
        })

    return panel_items


def _get_accessible_namespaces():
    if not current_user.is_authenticated:
        return []

    if current_user.is_superadmin:
        return Namespace.query.filter_by(is_deleted=False).order_by(Namespace.name.asc()).all()

    if hasattr(current_user, 'accessible_namespaces'):
        return current_user.accessible_namespaces.order_by(Namespace.name.asc()).all()

    return []


def _get_selected_namespace(available_namespaces=None):
    available_namespaces = available_namespaces if available_namespaces is not None else _get_accessible_namespaces()
    if not available_namespaces:
        session.pop('selected_namespace_id', None)
        return None

    selected_namespace_id = session.get('selected_namespace_id')
    if selected_namespace_id is not None:
        for namespace in available_namespaces:
            if namespace.id == selected_namespace_id:
                return namespace

    selected_namespace = available_namespaces[0]
    session['selected_namespace_id'] = selected_namespace.id
    return selected_namespace


def _resolve_namespace_season(selected_namespace):
    """Resolve year/gender from selected namespace with safe defaults."""
    if selected_namespace is None:
        return None, None

    year = _safe_int(getattr(selected_namespace, 'database_year', None))
    gender = (getattr(selected_namespace, 'gender', None) or 'Men').strip() or 'Men'
    return year, gender


def _normalize_gender_token(value):
    token = str(value or '').strip().lower()
    if token in ('m', 'men', 'male', 'man'):
        return 'men'
    if token in ('w', 'f', 'women', 'woman', 'female', 'ladies'):
        return 'women'
    return token


def _gender_matches(row_gender, selected_gender):
    normalized_selected = _normalize_gender_token(selected_gender)
    normalized_row = _normalize_gender_token(row_gender)
    if not normalized_selected:
        return True
    if not normalized_row:
        return True
    return normalized_row == normalized_selected


def _load_namespace_cyclist_stats(namespace, rider_ids):
    if namespace is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_id,
                        COALESCE(stat_mo, 0) AS stat_mo,
                        COALESCE(stat_mm, 0) AS stat_mm,
                        COALESCE(stat_dh, 0) AS stat_dh,
                        COALESCE(stat_hil, 0) AS stat_hil,
                        COALESCE(stat_tt, 0) AS stat_tt,
                        COALESCE(stat_prl, 0) AS stat_prl,
                        COALESCE(stat_fla, 0) AS stat_fla,
                        COALESCE(stat_spr, 0) AS stat_spr,
                        COALESCE(stat_acc, 0) AS stat_acc,
                        COALESCE(stat_end, 0) AS stat_end,
                        COALESCE(stat_res, 0) AS stat_res,
                        COALESCE(stat_rec, 0) AS stat_rec,
                        COALESCE(stat_cob, 0) AS stat_cob,
                        COALESCE(stat_att, 0) AS stat_att
                    FROM cdb_cyclist
                    WHERE first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist stats from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    stats_map = {}
    for row in rows:
        stats_map[row['first_cycling_id']] = {
            'mo': row['stat_mo'],
            'mm': row['stat_mm'],
            'dh': row['stat_dh'],
            'hil': row['stat_hil'],
            'tt': row['stat_tt'],
            'prl': row['stat_prl'],
            'fla': row['stat_fla'],
            'spr': row['stat_spr'],
            'acc': row['stat_acc'],
            'end': row['stat_end'],
            'res': row['stat_res'],
            'rec': row['stat_rec'],
            'cob': row['stat_cob'],
            'att': row['stat_att'],
        }

    return stats_map


def _load_namespace_cyclist_measurements(namespace, rider_ids):
    if namespace is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_id,
                        size_cm,
                        weight_kg
                    FROM cdb_cyclist
                    WHERE first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist measurements from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    measurements_map = {}
    for row in rows:
        measurements_map[row['first_cycling_id']] = {
            'size_cm': row.get('size_cm'),
            'weight_kg': row.get('weight_kg'),
        }

    return measurements_map


def _load_namespace_race_result_deltas(namespace, first_cycling_race_id, first_cycling_stage_id, rider_ids):
    """Load saved stat delta rows keyed by rider and stat name for one race."""
    if namespace is None or first_cycling_race_id is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_rider_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE first_cycling_race_id = :race_id
                      AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                      AND stat_list_id IS NULL
                      AND first_cycling_rider_id = ANY(:rider_ids)
                    """
                ),
                {
                    'race_id': first_cycling_race_id,
                    'stage_id': first_cycling_stage_id,
                    'rider_ids': list(rider_ids),
                },
            ).mappings().all()
    except Exception:
        # Table may not exist yet for older namespaces; treat as no deltas.
        logger.exception('Failed to load race result deltas from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['first_cycling_rider_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
        }

    return delta_map


def _load_namespace_race_delta_status(namespace, first_cycling_race_ids):
    """Load race-level delta/approval aggregates keyed by FirstCycling race_id."""
    if namespace is None or not first_cycling_race_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_race_id,
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE first_cycling_race_id = ANY(:race_ids)
                      AND stat_list_id IS NULL
                    GROUP BY first_cycling_race_id
                    """
                ),
                {'race_ids': list(first_cycling_race_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load race delta status from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    status_map = {}
    for row in rows:
        race_id = row['first_cycling_race_id']
        total_changes = row['total_changes'] or 0
        approved_changes = row['approved_changes'] or 0
        status_map[race_id] = {
            'total_changes': int(total_changes),
            'approved_changes': int(approved_changes),
        }

    return status_map


def _load_namespace_stage_delta_status(namespace, first_cycling_race_ids):
    """Load stage-level delta/approval aggregates keyed by FirstCycling stage_id."""
    if namespace is None or not first_cycling_race_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_stage_id,
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE first_cycling_race_id = ANY(:race_ids)
                      AND first_cycling_stage_id IS NOT NULL
                      AND stat_list_id IS NULL
                    GROUP BY first_cycling_stage_id
                    """
                ),
                {'race_ids': list(first_cycling_race_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stage delta status from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    status_map = {}
    for row in rows:
        stage_id = row['first_cycling_stage_id']
        if stage_id is None:
            continue

        total_changes = row['total_changes'] or 0
        approved_changes = row['approved_changes'] or 0
        status_map[int(stage_id)] = {
            'total_changes': int(total_changes),
            'approved_changes': int(approved_changes),
        }

    return status_map


def _load_namespace_scope_delta_status(namespace, first_cycling_race_id=None, first_cycling_stage_id=None, stat_list_id=None):
    """Load aggregate delta status for one scope (race/stage or list)."""
    if namespace is None:
        return {
            'total_changes': 0,
            'approved_changes': 0,
            'unapproved_changes': 0,
        }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE first_cycling_race_id IS NOT DISTINCT FROM :race_id
                      AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                      AND stat_list_id IS NOT DISTINCT FROM :list_id
                    """
                ),
                {
                    'race_id': first_cycling_race_id,
                    'stage_id': first_cycling_stage_id,
                    'list_id': stat_list_id,
                },
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load scope delta status from namespace %s', namespace.slug)
        return {
            'total_changes': 0,
            'approved_changes': 0,
            'unapproved_changes': 0,
        }
    finally:
        engine.dispose()

    total_changes = int((row or {}).get('total_changes') or 0)
    approved_changes = int((row or {}).get('approved_changes') or 0)
    unapproved_changes = max(total_changes - approved_changes, 0)

    return {
        'total_changes': total_changes,
        'approved_changes': approved_changes,
        'unapproved_changes': unapproved_changes,
    }


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _encode_release_item_key(scope_type, race_id=0, stage_id=0, list_id=0):
    return f"{scope_type}:{race_id or 0}:{stage_id or 0}:{list_id or 0}"


def _decode_release_item_key(raw_key):
    parts = (raw_key or '').split(':')
    if len(parts) != 4:
        return None

    scope_type = parts[0].strip().lower()
    if scope_type not in ('race', 'stage', 'list'):
        return None

    race_id = _safe_int(parts[1])
    stage_id = _safe_int(parts[2])
    list_id = _safe_int(parts[3])
    if race_id is None or stage_id is None or list_id is None:
        return None

    return {
        'scope_type': scope_type,
        'first_cycling_race_id': race_id or None,
        'first_cycling_stage_id': stage_id or None,
        'stat_list_id': list_id or None,
    }


def _load_namespace_approved_release_items(namespace, season_year=None, gender=None, selected_release_id=None):
    """Return approved inclusion candidates across race/stage/list scopes."""
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            release_scope_id = _safe_int(selected_release_id) or 0
            rows = conn.execute(
                text(
                    """
                    SELECT
                        COALESCE(first_cycling_race_id, 0) AS first_cycling_race_id,
                        COALESCE(first_cycling_stage_id, 0) AS first_cycling_stage_id,
                        COALESCE(stat_list_id, 0) AS stat_list_id,
                        SUM(CASE WHEN COALESCE(release_id, 0) = :selected_release_id THEN 1 ELSE 0 END) AS selected_rows,
                        COUNT(*) AS total_edits,
                        COUNT(DISTINCT first_cycling_rider_id) AS riders_edited
                    FROM stat_changes
                    WHERE COALESCE(release_id, 0) IN (0, :selected_release_id)
                    GROUP BY
                        COALESCE(first_cycling_race_id, 0),
                        COALESCE(first_cycling_stage_id, 0),
                        COALESCE(stat_list_id, 0)
                    HAVING COUNT(*) = SUM(CASE WHEN COALESCE(is_approved, FALSE) THEN 1 ELSE 0 END)
                       AND (
                            (COALESCE(stat_list_id, 0) = 0 AND COALESCE(first_cycling_race_id, 0) > 0)
                            OR (
                                COALESCE(stat_list_id, 0) > 0
                                AND COALESCE(first_cycling_race_id, 0) = 0
                                AND COALESCE(first_cycling_stage_id, 0) = 0
                            )
                       )
                    """
                ),
                {'selected_release_id': release_scope_id},
            ).mappings().all()

            list_ids = {
                int(row['stat_list_id'])
                for row in rows
                if (row.get('stat_list_id') or 0) > 0
            }
            list_name_map = {}
            if list_ids:
                list_query = """
                    SELECT id, list_name, season_year, gender, created_at
                    FROM stat_lists
                    WHERE id = ANY(:list_ids)
                """
                list_rows = conn.execute(
                    text(list_query),
                    {'list_ids': list(list_ids)},
                ).mappings().all()
                list_name_map = {int(row['id']): dict(row) for row in list_rows if row.get('id') is not None}
    except Exception:
        logger.exception('Failed to load approved inclusion items from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()

    race_ids = {
        int(row['first_cycling_race_id'])
        for row in rows
        if (row.get('first_cycling_race_id') or 0) > 0
    }
    stage_ids = {
        int(row['first_cycling_stage_id'])
        for row in rows
        if (row.get('first_cycling_stage_id') or 0) > 0
    }

    race_map = {}
    if race_ids:
        race_query = FirstCyclingRace.query.filter(FirstCyclingRace.race_id.in_(race_ids))
        if season_year is not None:
            race_query = race_query.filter(FirstCyclingRace.season_year == season_year)
        races = race_query.all()
        race_map = {
            int(race.race_id): race
            for race in races
            if race.race_id is not None and _gender_matches(race.gender, gender)
        }

    stage_map = {}
    if stage_ids:
        stages = FirstCyclingRaceStage.query.filter(FirstCyclingRaceStage.id.in_(stage_ids)).all()
        stage_map = {int(stage.id): stage for stage in stages if stage.id is not None}

    items = []
    for row in rows:
        race_id = int(row.get('first_cycling_race_id') or 0)
        stage_id = int(row.get('first_cycling_stage_id') or 0)
        list_id = int(row.get('stat_list_id') or 0)

        if list_id > 0:
            list_meta = list_name_map.get(list_id)
            if not list_meta:
                continue
            if season_year is not None and _safe_int(list_meta.get('season_year')) != season_year:
                continue
            if not _gender_matches(list_meta.get('gender'), gender):
                continue

            scope_type = 'list'
            display_name = list_meta.get('list_name') or f'List #{list_id}'
            race_obj = None
            item_date = list_meta.get('created_at') or '-'
            category = '-'
            classification_label = '-'
        elif stage_id > 0:
            race_obj = race_map.get(race_id)
            stage_obj = stage_map.get(stage_id)
            if race_obj is None or stage_obj is None:
                continue
            scope_type = 'stage'
            stage_name = stage_obj.stage_name or f'Stage {stage_obj.stage_number or "?"}'
            display_name = f"{race_obj.name or f'Race #{race_id}'} - {stage_name}"
            item_date = stage_obj.stage_date if stage_obj is not None else (race_obj.start_date if race_obj is not None else '-')
            category = race_obj.category if race_obj is not None else '-'
            classification_label = race_obj.classification_label if race_obj is not None else '-'
        else:
            race_obj = race_map.get(race_id)
            if race_obj is None:
                continue
            scope_type = 'race'
            display_name = race_obj.name or f'Race #{race_id}'
            item_date = race_obj.start_date if race_obj is not None else '-'
            category = race_obj.category if race_obj is not None else '-'
            classification_label = race_obj.classification_label if race_obj is not None else '-'

        items.append({
            'item_key': _encode_release_item_key(scope_type, race_id, stage_id, list_id),
            'scope_type': scope_type,
            'type_label': 'List' if scope_type == 'list' else 'Race',
            'is_selected': (int(row.get('selected_rows') or 0) > 0) if selected_release_id is not None else False,
            'date': item_date,
            'name': display_name,
            'category': category,
            'classification_label': classification_label,
            'riders_edited': int(row.get('riders_edited') or 0),
            'total_edits': int(row.get('total_edits') or 0),
        })

    return sorted(
        items,
        key=lambda item: (
            str(item.get('date') or ''),
            str(item.get('scope_type') or ''),
            str(item.get('name') or ''),
        ),
    )


def _sync_release_item_keys(namespace, release_id, selected_item_keys, visible_item_keys):
    if namespace is None or release_id is None or not visible_item_keys:
        return 0

    selected_keys = set(selected_item_keys or set())
    parsed_by_key = {}
    for key in sorted(visible_item_keys):
        parsed = _decode_release_item_key(key)
        if parsed is not None:
            parsed_by_key[key] = parsed

    if not parsed_by_key:
        return 0

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    updated_rows = 0
    try:
        with engine.begin() as conn:
            for key, item in parsed_by_key.items():
                target_release_id = release_id if key in selected_keys else 0
                result = conn.execute(
                    text(
                        """
                        UPDATE stat_changes
                        SET release_id = :target_release_id
                        WHERE COALESCE(release_id, 0) IN (0, :current_release_id)
                          AND COALESCE(release_id, 0) <> :target_release_id
                          AND COALESCE(is_approved, FALSE) = TRUE
                          AND first_cycling_race_id IS NOT DISTINCT FROM :race_id
                          AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                          AND stat_list_id IS NOT DISTINCT FROM :list_id
                        """
                    ),
                    {
                        'target_release_id': target_release_id,
                        'current_release_id': release_id,
                        'race_id': item['first_cycling_race_id'],
                        'stage_id': item['first_cycling_stage_id'],
                        'list_id': item['stat_list_id'],
                    },
                )
                updated_rows += int(result.rowcount or 0)
    finally:
        engine.dispose()

    return updated_rows


def _sanitize_release_filename_part(value):
    normalized = re.sub(r'[^a-zA-Z0-9._-]+', '-', (value or '').strip())
    return normalized.strip('-') or 'release'


def _resolve_namespace_release_file_path(namespace, stored_path):
    """Resolve stored namespace-relative release artifact paths to absolute paths."""
    if namespace is None or not stored_path:
        return None

    candidate = (stored_path or '').strip()
    if not candidate:
        return None

    if os.path.isabs(candidate):
        return candidate if os.path.exists(candidate) else None

    from webapp.config import get_namespace_path

    absolute_path = os.path.join(get_namespace_path(namespace.slug), candidate)
    return absolute_path if os.path.exists(absolute_path) else None


def _resolve_namespace_sqlite_file_path(namespace, stored_path):
    return _resolve_namespace_release_file_path(namespace, stored_path)


def _resolve_release_base_sqlite_path(namespace, release):
    """Pick baseline SQLite from previous release; fallback to namespace initial import file."""
    if namespace is None or release is None:
        return None

    previous_releases = (
        Release.query
        .filter(
            Release.namespace_id == namespace.id,
            Release.id != release.id,
            Release.sqlite_file_path.isnot(None),
            Release.sqlite_file_path != '',
        )
        .order_by(Release.created_at.desc(), Release.id.desc())
        .all()
    )

    for previous_release in previous_releases:
        previous_path = _resolve_namespace_sqlite_file_path(namespace, previous_release.sqlite_file_path)
        if previous_path:
            return previous_path

    return _resolve_namespace_sqlite_file_path(namespace, namespace.sqlite_file_path)


def _load_namespace_release_versions(namespace, release_id):
    """Load generated release artifact records from the namespace database."""
    if namespace is None or release_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        release_id,
                        file_type,
                        display_name,
                        relative_path,
                        content_type,
                        file_size_bytes,
                        md5_checksum,
                        sha256_checksum,
                        created_at
                    FROM release_versions
                    WHERE release_id = :release_id
                    ORDER BY created_at ASC NULLS LAST, id ASC
                    """
                ),
                {'release_id': release_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load release artifact records for namespace %s release %s', namespace.slug, release_id)
        return []
    finally:
        engine.dispose()

    label_map = {
        'cdb_sqlite': 'CDB SQLite',
        'rider_stats_sql': 'Rider Stats SQL',
        'release_overview_html': 'Release Overview HTML',
    }

    normalized_rows = []
    for row in rows:
        row_dict = dict(row)
        row_dict['file_type_label'] = label_map.get(row_dict.get('file_type'), row_dict.get('file_type') or '-')
        normalized_rows.append(row_dict)

    return normalized_rows


def _replace_namespace_release_versions(namespace, release_id, files):
    """Replace generated release artifact records for one release in the namespace database."""
    if namespace is None or release_id is None:
        return

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM release_versions WHERE release_id = :release_id"),
                {'release_id': release_id},
            )

            for file_info in files:
                conn.execute(
                    text(
                        """
                        INSERT INTO release_versions (
                            release_id,
                            file_type,
                            display_name,
                            relative_path,
                            content_type,
                            file_size_bytes,
                            md5_checksum,
                            sha256_checksum,
                            created_at
                        )
                        VALUES (
                            :release_id,
                            :file_type,
                            :display_name,
                            :relative_path,
                            :content_type,
                            :file_size_bytes,
                            :md5_checksum,
                            :sha256_checksum,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        'release_id': release_id,
                        'file_type': file_info.get('file_type'),
                        'display_name': file_info.get('display_name'),
                        'relative_path': file_info.get('relative_path'),
                        'content_type': file_info.get('content_type'),
                        'file_size_bytes': file_info.get('file_size_bytes'),
                        'md5_checksum': file_info.get('md5_checksum'),
                        'sha256_checksum': file_info.get('sha256_checksum'),
                    },
                )
    finally:
        engine.dispose()


def _load_release_stat_updates(namespace, release_id):
    """Load approved final stat values assigned to the release, keyed by cyclist/stat."""
    if namespace is None or release_id is None:
        return {}

    pcm_version = namespace.pcm_version or 'PCM2025'
    field_mapping = get_field_mapping(pcm_version, 'cyclist')
    cyclist_table_name = field_mapping.get('cdb_table_name')
    cyclist_fields = field_mapping.get('fields') or {}
    cyclist_id_column = cyclist_fields.get('id')

    stat_column_by_code = {}
    for internal_name, source_column in cyclist_fields.items():
        if not internal_name.startswith('stat_'):
            continue
        stat_code = internal_name.replace('stat_', '').strip().lower()
        if stat_code:
            stat_column_by_code[stat_code] = source_column

    if not cyclist_table_name or not cyclist_id_column:
        raise ValueError(f'Missing cyclist table mapping for PCM version {pcm_version}')

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cyclist_pcm_id,
                        stat_name,
                        final_value
                    FROM stat_changes
                    WHERE release_id = :release_id
                      AND COALESCE(is_approved, FALSE) = TRUE
                    ORDER BY id ASC
                    """
                ),
                {'release_id': release_id},
            ).mappings().all()
    finally:
        engine.dispose()

    updates = {}
    for row in rows:
        cyclist_pcm_id = _safe_int(row.get('cyclist_pcm_id'))
        raw_stat_name = (row.get('stat_name') or '').strip().lower()
        final_value = _safe_int(row.get('final_value'))

        if cyclist_pcm_id is None or not raw_stat_name or final_value is None:
            continue

        stat_code = raw_stat_name[5:] if raw_stat_name.startswith('stat_') else raw_stat_name
        if stat_code not in STAT_DELTA_FIELDS:
            continue

        source_column = stat_column_by_code.get(stat_code)
        if not source_column:
            continue

        updates[(cyclist_pcm_id, source_column)] = final_value

    return {
        'table_name': cyclist_table_name,
        'id_column': cyclist_id_column,
        'updates': updates,
    }


def _apply_release_updates_to_sqlite(sqlite_file_path, table_name, id_column, updates):
    """Apply stat updates to the copied SQLite CDB file."""
    if not updates:
        return 0

    grouped_updates = {}
    for (cyclist_pcm_id, stat_column), final_value in updates.items():
        grouped_updates.setdefault(stat_column, []).append((final_value, cyclist_pcm_id))

    applied_values = 0
    connection = sqlite3.connect(sqlite_file_path)
    try:
        cursor = connection.cursor()
        for stat_column, pairs in grouped_updates.items():
            cursor.executemany(
                f'UPDATE "{table_name}" SET "{stat_column}" = ? WHERE "{id_column}" = ?',
                pairs,
            )
            applied_values += len(pairs)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return applied_values


def _compute_file_hashes(file_path):
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()

    with open(file_path, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            md5_hash.update(chunk)
            sha256_hash.update(chunk)

    return md5_hash.hexdigest(), sha256_hash.hexdigest()


def _build_release_artifact_basename(namespace, release):
    from webapp.config import ensure_namespace_directories

    ensure_namespace_directories(namespace.slug)

    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    safe_namespace = _sanitize_release_filename_part(namespace.slug or namespace.name)
    safe_version = _sanitize_release_filename_part(release.version)
    return f'{safe_namespace}-{safe_version}-{timestamp}'


def _create_release_text_artifact(namespace, output_filename, content, content_type):
    from webapp.config import ensure_namespace_directories, get_cdb_releases_path

    ensure_namespace_directories(namespace.slug)
    output_dir = get_cdb_releases_path(namespace.slug)
    output_abs_path = os.path.join(output_dir, output_filename)

    with open(output_abs_path, 'w', encoding='utf-8') as handle:
        handle.write(content)

    md5_checksum, sha256_checksum = _compute_file_hashes(output_abs_path)
    relative_path = f'releases/{output_filename}'

    return {
        'display_name': output_filename,
        'relative_path': relative_path,
        'content_type': content_type,
        'file_size_bytes': os.path.getsize(output_abs_path),
        'md5_checksum': md5_checksum,
        'sha256_checksum': sha256_checksum,
    }


def _build_review_sqlite_for_release(namespace, release, artifact_basename):
    """Create a release SQLite file from previous baseline and assigned stat changes."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    baseline_path = _resolve_release_base_sqlite_path(namespace, release)
    if not baseline_path:
        raise ValueError(
            'No baseline SQLite database found. Upload a namespace CDB or create an earlier release with a SQLite file first.'
        )

    from webapp.config import ensure_namespace_directories, get_cdb_releases_path

    ensure_namespace_directories(namespace.slug)

    output_filename = f'{artifact_basename}.sqlite'
    output_dir = get_cdb_releases_path(namespace.slug)
    output_abs_path = os.path.join(output_dir, output_filename)

    shutil.copy2(baseline_path, output_abs_path)

    try:
        updates_payload = _load_release_stat_updates(namespace, release.id)
        applied_changes = _apply_release_updates_to_sqlite(
            output_abs_path,
            updates_payload['table_name'],
            updates_payload['id_column'],
            updates_payload['updates'],
        )
    except Exception:
        if os.path.exists(output_abs_path):
            os.remove(output_abs_path)
        raise

    md5_checksum, sha256_checksum = _compute_file_hashes(output_abs_path)
    relative_path = f'releases/{output_filename}'

    return {
        'display_name': output_filename,
        'relative_path': relative_path,
        'content_type': 'application/vnd.sqlite3',
        'file_size_bytes': os.path.getsize(output_abs_path),
        'md5_checksum': md5_checksum,
        'sha256_checksum': sha256_checksum,
        'applied_changes': applied_changes,
    }


def _build_release_stat_sql_for_release(namespace, release, artifact_basename):
    """Create a standalone SQL script that applies the release's rider stat updates."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    updates_payload = _load_release_stat_updates(namespace, release.id)
    table_name = updates_payload['table_name']
    id_column = updates_payload['id_column']
    updates = updates_payload['updates']

    rider_updates = {}
    for (cyclist_pcm_id, stat_column), final_value in sorted(updates.items()):
        rider_updates.setdefault(cyclist_pcm_id, []).append((stat_column, final_value))

    release_title = f'{release.release_name or "Release"} {release.version or ""}'.strip()
    lines = [
        f'-- Rider stats update SQL for {release_title}',
        f'-- Generated at {datetime.utcnow().isoformat()}Z',
        'BEGIN TRANSACTION;',
    ]

    for cyclist_pcm_id in sorted(rider_updates):
        assignments = ', '.join(
            f'"{column_name}" = {int(final_value)}'
            for column_name, final_value in sorted(rider_updates[cyclist_pcm_id])
        )
        lines.append(
            f'UPDATE "{table_name}" SET {assignments} WHERE "{id_column}" = {int(cyclist_pcm_id)};'
        )

    lines.append('COMMIT;')
    lines.append('')

    file_info = _create_release_text_artifact(
        namespace,
        f'{artifact_basename}-rider-stats.sql',
        '\n'.join(lines),
        'application/sql',
    )
    file_info['applied_changes'] = len(updates)
    return file_info


def _build_release_overview_html_for_release(namespace, release, artifact_basename, generated_files):
    """Create a static HTML overview for the release and its assigned items."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    season_year, gender = _resolve_namespace_season(namespace)
    selected_items = [
        item
        for item in _load_namespace_approved_release_items(
            namespace,
            season_year=season_year,
            gender=gender,
            selected_release_id=release.id,
        )
        if item.get('is_selected')
    ]

    release_title = f'{release.release_name or "Release"} {release.version or ""}'.strip()
    release_notes = html.escape(release.release_notes or '-')
    item_rows = []
    for item in selected_items:
        item_rows.append(
            '<tr>'
            f'<td>{html.escape(item.get("type_label") or "-")}</td>'
            f'<td>{html.escape(str(item.get("date") or "-"))}</td>'
            f'<td>{html.escape(item.get("name") or "-")}</td>'
            f'<td>{int(item.get("riders_edited") or 0)}</td>'
            f'<td>{int(item.get("total_edits") or 0)}</td>'
            f'<td>{html.escape(item.get("category") or "-")}</td>'
            f'<td>{html.escape(item.get("classification_label") or "-")}</td>'
            '</tr>'
        )

    file_rows = []
    for file_info in generated_files:
        file_rows.append(
            '<tr>'
            f'<td>{html.escape(file_info.get("file_type_label") or "-")}</td>'
            f'<td>{html.escape(file_info.get("display_name") or "-")}</td>'
            f'<td>{int(file_info.get("file_size_bytes") or 0)}</td>'
            '</tr>'
        )

    html_content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(release_title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
    h1, h2 {{ margin-bottom: 0.5rem; }}
    p {{ margin-top: 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .meta {{ margin-bottom: 1.5rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(release_title)}</h1>
  <p>Namespace: {html.escape(namespace.name or namespace.slug or '-')}</p>
  <div class="meta">
    <p>Generated at: {html.escape(datetime.utcnow().isoformat())}Z</p>
    <p>Release notes: {release_notes}</p>
  </div>
  <h2>Release Files</h2>
  <table>
    <thead>
      <tr><th>Type</th><th>File</th><th>Size (bytes)</th></tr>
    </thead>
    <tbody>
      {''.join(file_rows) or '<tr><td colspan="3">No files generated.</td></tr>'}
    </tbody>
  </table>
  <h2>Included Items</h2>
  <table>
    <thead>
      <tr><th>Type</th><th>Date</th><th>Name</th><th>Rider Edits</th><th>Total Edits</th><th>Category</th><th>Classification</th></tr>
    </thead>
    <tbody>
      {''.join(item_rows) or '<tr><td colspan="7">No included items assigned.</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""

    return _create_release_text_artifact(
        namespace,
        f'{artifact_basename}-overview.html',
        html_content,
        'text/html',
    )


def _build_review_artifacts_for_release(namespace, release):
    """Generate all release artifacts when moving a release from draft to review."""
    artifact_basename = _build_release_artifact_basename(namespace, release)

    sqlite_file = _build_review_sqlite_for_release(namespace, release, artifact_basename)
    sqlite_file['file_type'] = 'cdb_sqlite'
    sqlite_file['file_type_label'] = 'CDB SQLite'

    sql_file = _build_release_stat_sql_for_release(namespace, release, artifact_basename)
    sql_file['file_type'] = 'rider_stats_sql'
    sql_file['file_type_label'] = 'Rider Stats SQL'

    overview_file = _build_release_overview_html_for_release(
        namespace,
        release,
        artifact_basename,
        [sqlite_file, sql_file],
    )
    overview_file['file_type'] = 'release_overview_html'
    overview_file['file_type_label'] = 'Release Overview HTML'

    generated_files = [sqlite_file, sql_file, overview_file]
    _replace_namespace_release_versions(namespace, release.id, generated_files)

    return {
        'sqlite_file': sqlite_file,
        'files': generated_files,
        'applied_changes': sqlite_file.get('applied_changes', 0),
    }


def _apply_scope_stat_changes_to_cdb(conn, where_clause, params):
    """Apply pending stat_changes.final_value into cdb_cyclist for one scope."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT id, cyclist_pcm_id, stat_name, final_value
            FROM stat_changes
            WHERE {where_clause}
              AND is_approved = FALSE
            ORDER BY id ASC
            """
        ),
        params,
    ).mappings().all()

    applied_count = 0
    for row in pending_rows:
        cyclist_pcm_id = _safe_int(row.get('cyclist_pcm_id'))
        final_value = _safe_int(row.get('final_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = f'stat_{stat_code}' if stat_code in STAT_DELTA_FIELDS else None

        if cyclist_pcm_id is None or final_value is None or not column_name:
            continue

        conn.execute(
            text(
                f'''
                UPDATE cdb_cyclist
                SET "{column_name}" = :final_value
                WHERE id = :cyclist_pcm_id
                '''
            ),
            {
                'final_value': final_value,
                'cyclist_pcm_id': cyclist_pcm_id,
            },
        )
        applied_count += 1

    return applied_count


def _load_namespace_fully_approved_race_ids(namespace):
    """Return FirstCycling race IDs where all race-scope stat changes are approved."""
    if namespace is None:
        return set()

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT first_cycling_race_id
                    FROM stat_changes
                    WHERE stat_list_id IS NULL
                      AND first_cycling_race_id IS NOT NULL
                    GROUP BY first_cycling_race_id
                    HAVING COUNT(*) = SUM(CASE WHEN is_approved THEN 1 ELSE 0 END)
                    """
                )
            ).mappings().all()
        return {
            int(row['first_cycling_race_id'])
            for row in rows
            if row.get('first_cycling_race_id') is not None
        }
    except Exception:
        logger.exception('Failed to load approved race ids from namespace %s', namespace.slug)
        return set()
    finally:
        engine.dispose()


def _load_namespace_race_edit_aggregates(namespace, first_cycling_race_ids):
    """Return per-race edit aggregates for release inclusion table."""
    if namespace is None or not first_cycling_race_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_race_id,
                        COUNT(*) AS total_edits,
                        COUNT(DISTINCT first_cycling_rider_id) AS riders_edited
                    FROM stat_changes
                    WHERE stat_list_id IS NULL
                      AND first_cycling_race_id = ANY(:race_ids)
                    GROUP BY first_cycling_race_id
                    """
                ),
                {'race_ids': list(first_cycling_race_ids)},
            ).mappings().all()

        aggregate_map = {}
        for row in rows:
            race_id = row.get('first_cycling_race_id')
            if race_id is None:
                continue
            aggregate_map[int(race_id)] = {
                'riders_edited': int(row.get('riders_edited') or 0),
                'total_edits': int(row.get('total_edits') or 0),
            }
        return aggregate_map
    except Exception:
        logger.exception('Failed to load race edit aggregates from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()


def _load_namespace_stat_lists(namespace, season_year=None, gender=None):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        params = {}
        with engine.connect() as conn:
            stat_list_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'stat_lists'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                return []

            selected_columns = ['id', 'list_name']
            if 'season_year' in stat_list_columns:
                selected_columns.append('season_year')
            if 'gender' in stat_list_columns:
                selected_columns.append('gender')
            if 'created_at' in stat_list_columns:
                selected_columns.append('created_at')

            query = """
                SELECT {selected_columns}
                FROM stat_lists
                WHERE 1=1
            """.format(selected_columns=', '.join(selected_columns))

            if season_year is not None and 'season_year' in stat_list_columns:
                query += " AND season_year = :season_year"
                params['season_year'] = season_year
            if gender and 'gender' in stat_list_columns:
                query += " AND LOWER(gender) = LOWER(:gender)"
                params['gender'] = gender
            query += " ORDER BY id DESC"

            rows = conn.execute(text(query), params).mappings().all()

        normalized_rows = []
        for row in rows:
            row_dict = dict(row)
            if 'season_year' not in row_dict:
                row_dict['season_year'] = None
            if 'gender' not in row_dict:
                row_dict['gender'] = None
            if 'created_at' not in row_dict:
                row_dict['created_at'] = None
            normalized_rows.append(row_dict)

        # Enrich with rider counts and stat change aggregates
        list_ids = [int(r['id']) for r in normalized_rows]
        if list_ids:
            with engine.connect() as conn:
                # Riders per list
                rider_counts = conn.execute(
                    text(
                        """
                        SELECT stat_list_id, COUNT(*) AS riders_count
                        FROM stat_list_items
                        WHERE stat_list_id = ANY(:list_ids)
                        GROUP BY stat_list_id
                        """
                    ),
                    {'list_ids': list_ids},
                ).mappings().all()

                rider_count_map = {int(r['stat_list_id']): int(r['riders_count'] or 0) for r in rider_counts}

                # Stat changes aggregates per list
                change_aggs = conn.execute(
                    text(
                        """
                        SELECT stat_list_id,
                               COUNT(*) AS total_changes,
                               SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                        FROM stat_changes
                        WHERE stat_list_id = ANY(:list_ids)
                        GROUP BY stat_list_id
                        """
                    ),
                    {'list_ids': list_ids},
                ).mappings().all()

                change_map = {
                    int(r['stat_list_id']): {
                        'total_changes': int(r['total_changes'] or 0),
                        'approved_changes': int(r['approved_changes'] or 0),
                    }
                    for r in change_aggs
                }

            # Attach counts and status indicators
            enriched = []
            for r in normalized_rows:
                lid = int(r['id'])
                riders = rider_count_map.get(lid, 0)
                aggs = change_map.get(lid, {'total_changes': 0, 'approved_changes': 0})
                total_changes = aggs['total_changes']
                approved_changes = aggs['approved_changes']

                # Determine status flags similar to race results
                has_changes = total_changes > 0
                all_approved = has_changes and (approved_changes == total_changes)
                status = {
                    'has_stat_changes': has_changes,
                    'is_approved': all_approved,
                    'has_namespace': namespace is not None,
                }

                r.update({
                    'riders_count': riders,
                    'total_changes': total_changes,
                    'approved_changes': approved_changes,
                    'status': status,
                })
                enriched.append(r)

            return enriched

        return normalized_rows
    except Exception:
        logger.exception('Failed to load stat lists from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()


def _load_namespace_stat_list_items(namespace, stat_list_id):
    if namespace is None or stat_list_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT first_cycling_rider_id
                    FROM stat_list_items
                    WHERE stat_list_id = :list_id
                    ORDER BY id ASC
                    """
                ),
                {'list_id': stat_list_id},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        logger.exception('Failed to load stat list items from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()


def _load_namespace_list_result_deltas(namespace, stat_list_id, rider_ids):
    """Load saved stat changes for one ad-hoc stat list keyed by rider/stat."""
    if namespace is None or stat_list_id is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_rider_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE stat_list_id = :list_id
                      AND first_cycling_race_id IS NULL
                      AND first_cycling_stage_id IS NULL
                      AND first_cycling_rider_id = ANY(:rider_ids)
                    """
                ),
                {
                    'list_id': stat_list_id,
                    'rider_ids': list(rider_ids),
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load list stat changes from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['first_cycling_rider_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
        }

    return delta_map


def _load_config_seasons():
    configured = current_app.config.get('SEASONS', []) or []
    seasons = []
    for entry in configured:
        if not isinstance(entry, dict):
            continue
        year_value = entry.get('year')
        gender_value = entry.get('gender')
        try:
            year = int(year_value)
        except (TypeError, ValueError):
            continue
        if not isinstance(gender_value, str) or not gender_value.strip():
            continue
        gender = gender_value.strip()
        seasons.append({
            'year': year,
            'gender': gender,
            'is_default': bool(entry.get('is_default', False)),
        })

    deduped = {}
    for entry in seasons:
        key = (entry['year'], entry['gender'])
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = dict(entry)
            continue
        if entry['is_default']:
            existing['is_default'] = True

    return [
        deduped[key]
        for key in sorted(deduped.keys(), key=lambda value: (value[0], value[1]))
    ]


def _resolve_default_season(seasons):
    if not seasons:
        return None

    for entry in seasons:
        if entry.get('is_default'):
            return entry

    return seasons[0]


def _build_race_result_state(race):
    race_stage_ids = {
        stage.id
        for stage in race.race_stages
        if stage.id is not None
    }

    saved_stage_ids = {
        result.stage_id
        for result in race.race_results
        if result.stage_id is not None
    }
    has_race_scope_result = any(
        result.result_scope == 'race' and result.stage_id is None
        for result in race.race_results
    )

    is_stage_race = len(race_stage_ids) > 0
    all_stages_saved = is_stage_race and race_stage_ids.issubset(saved_stage_ids)
    has_partial_stage_results = is_stage_race and len(saved_stage_ids.intersection(race_stage_ids)) > 0 and not all_stages_saved
    is_result_saved = all_stages_saved if is_stage_race else has_race_scope_result
    is_future_race = race.start_date is not None and race.start_date > datetime.utcnow().date()

    return {
        'is_stage_race': is_stage_race,
        'is_result_saved': is_result_saved,
        'has_partial_stage_results': has_partial_stage_results,
        'is_future_race': is_future_race,
    }


@web_bp.app_context_processor
def inject_namespace_selector_context():
    site_version = current_app.config.get('SITE_VERSION', 'Version ERROR')

    if not current_user.is_authenticated:
        return {
            'available_namespaces': [],
            'selected_namespace': None,
            'panel_menu_items': [],
            'site_version': site_version,
        }

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    panel_menu_items = _get_panel_menu_items()
    return {
        'available_namespaces': available_namespaces,
        'selected_namespace': selected_namespace,
        'panel_menu_items': panel_menu_items,
        'site_version': site_version,
    }

def admin_required(f):
    """Decorator to require admin access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('web.login'))
        if not (current_user.is_superadmin or current_user.has_role('admin')):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@web_bp.route('/')
def index():
    """Home page - redirect to dashboard if logged in, otherwise to login"""
    if current_user.is_authenticated:
        return redirect(url_for('web.dashboard'))
    return redirect(url_for('web.login'))


@web_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login page"""
    if current_user.is_authenticated:
        # Check if user has temporary password and needs to change it
        if current_user.is_temporary_password:
            return redirect(url_for('web.change_password'))
        return redirect(url_for('web.dashboard'))
    
    form = LoginForm()
    
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        
        if user and user.check_password(form.password.data):
            if not user.is_active:
                flash('Your account has been deactivated. Contact an administrator.', 'error')
                return redirect(url_for('web.login'))
            
            login_user(user, remember=form.remember.data)
            
            # Update last login time
            user.last_login = datetime.utcnow()
            
            # Log successful login
            AuditLog.log_action(
                action=AuditAction.USER_LOGIN,
                user=user,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent')
            )
            
            db.session.commit()
            
            # Check if user has temporary password
            if user.is_temporary_password:
                flash('You must change your temporary password before continuing.', 'warning')
                return redirect(url_for('web.change_password'))
            
            flash(f'Welcome back, {user.username}!', 'success')
            
            # Redirect to next page if specified, otherwise dashboard
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('web.dashboard'))
        else:
            # Log failed login attempt
            if user:
                AuditLog.log_action(
                    action=AuditAction.USER_LOGIN_FAILED,
                    user=user,
                    ip_address=request.remote_addr,
                    user_agent=request.headers.get('User-Agent'),
                    details={'reason': 'invalid_password'}
                )
                db.session.commit()
            flash('Invalid username or password', 'error')
    
    return render_template('login.html', form=form)


@web_bp.route('/logout')
@login_required
def logout():
    """User logout"""
    # Log logout before clearing session
    AuditLog.log_action(
        action=AuditAction.USER_LOGOUT,
        user=current_user,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )
    db.session.commit()
    
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('web.login'))


@web_bp.route('/dashboard')
@login_required
def dashboard():
    """User dashboard showing available app panels"""
    # Get all unique app panels the user has access to
    app_panels = {}
    resources = _user_resources()
    
    # Group by app_panel, excluding 'admin' panel
    for resource in resources:
        panel = resource.app_panel
        # Skip admin panel - it's now in settings dropdown
        if panel == 'admin':
            continue

        resource_description = (resource.description or '').strip()
        fallback_description = f'Manage and view {panel.replace("_", " ")}'
            
        if panel not in app_panels:
            app_panels[panel] = {
                'name': panel,
                'display_name': _get_panel_display_name(panel),
                'description': resource_description or fallback_description,
                'icon': get_app_icon(panel),
                'url': _get_panel_url(panel),
                'resources': []
            }
        elif resource_description and app_panels[panel]['description'] == fallback_description:
            # Prefer seeded resource descriptions over generated fallback text.
            app_panels[panel]['description'] = resource_description
        app_panels[panel]['resources'].append(resource.name)
    
    # Convert to list for template
    apps = list(app_panels.values())
    
    return render_template('dashboard.html', apps=apps)


def get_app_icon(app_panel):
    """Get icon emoji for app panel"""
    icons = {
        'admin': '⚙️',
        'stat_manager': '📊',
        'team_manager': '👥',
        'photo_manager': '📷',
        'palmares_manager': '🏆',
        'release_manager': '📦',
    }
    return icons.get(app_panel, '📁')


@web_bp.route('/teams')
@login_required
def teams():
    """Teams panel using selected namespace season/gender."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None

    division_options = []
    teams = []

    division_in_query = 'division' in request.args
    selected_division = request.args.get('division', type=str) if division_in_query else ''
    if selected_division is None:
        selected_division = ''
    selected_division = selected_division.strip()

    if season_selected:
        base_query = FirstCyclingTeam.query.filter_by(
            team_year=selected_year,
            gender=selected_gender,
        )

        division_options = [
            value
            for (value,) in base_query.with_entities(FirstCyclingTeam.division)
            .filter(FirstCyclingTeam.division.isnot(None))
            .distinct()
            .order_by(FirstCyclingTeam.division.asc())
            .all()
            if value
        ]

        if selected_division and selected_division not in division_options:
            selected_division = ''

        teams_query = base_query
        if selected_division:
            teams_query = teams_query.filter(FirstCyclingTeam.division == selected_division)

        teams = teams_query.order_by(
            cast(FirstCyclingTeam.ranking_rank, Integer).asc().nullslast(),
            FirstCyclingTeam.team_name.asc().nullslast(),
            FirstCyclingTeam.team_id.asc(),
        ).all()

    return render_template(
        'teams.html',
        selected_namespace=selected_namespace,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        teams=teams,
        division_options=division_options,
        selected_division=selected_division,
    )


@web_bp.route('/teams/<int:team_id>')
@login_required
def team_detail(team_id):
    """Team detail page by FirstCycling team_id."""
    if not _can_access_panel('team_manager'):
        abort(403)

    team = FirstCyclingTeam.query.filter_by(team_id=team_id).first_or_404()
    riders = FirstCyclingRider.query.filter_by(team_id=team_id).order_by(
        FirstCyclingRider.last_name.asc().nullslast(),
        FirstCyclingRider.first_name.asc().nullslast(),
        FirstCyclingRider.name.asc().nullslast(),
        FirstCyclingRider.rider_id.asc(),
    ).all()

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider_ids = [rider.rider_id for rider in riders if rider.rider_id is not None]
    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, rider_ids)

    return render_template(
        'teams_detail.html',
        team=team,
        riders=riders,
        rider_stats_map=rider_stats_map,
        stat_fields=STAT_DELTA_FIELDS,
        back_params={
            'division': request.args.get('division', type=str) or '',
        },
    )


@web_bp.route('/riders/<int:rider_id>')
@login_required
def rider_detail(rider_id):
    """Rider detail page by FirstCycling rider_id."""
    if not _can_access_panel('team_manager'):
        abort(403)

    rider = FirstCyclingRider.query.filter_by(rider_id=rider_id).first_or_404()
    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)

    team = None
    if rider.team_id is not None:
        team = FirstCyclingTeam.query.filter_by(team_id=rider.team_id).first()

    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, [rider_id])
    rider_stats = rider_stats_map.get(rider_id) or {}
    rider_measurements_map = _load_namespace_cyclist_measurements(selected_namespace, [rider_id])
    rider_measurements = rider_measurements_map.get(rider_id) or {}

    back_team_id = request.args.get('team_id', type=int)
    if back_team_id is None and rider.team_id is not None:
        back_team_id = rider.team_id

    return render_template(
        'rider_detail.html',
        rider=rider,
        team=team,
        rider_stats=rider_stats,
        rider_measurements=rider_measurements,
        selected_namespace=selected_namespace,
        back_params={
            'team_id': back_team_id,
            'division': request.args.get('division', type=str) or '',
        },
    )


@web_bp.route('/stats-editor')
@login_required
def stats_editor():
    """Stats editor panel with two methods: by race result and by ad-hoc list."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    active_method = (request.args.get('method', type=str) or 'by_race').strip().lower()
    if active_method not in ('by_race', 'by_list'):
        active_method = 'by_race'

    default_category = 'ME'
    default_classification = 'WorldTour'

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None

    races = []
    race_status_map = {}
    stage_status_map = {}
    list_entries = []
    selected_list_id = request.args.get('list_id', type=int)
    selected_list = None
    list_items = []
    list_riders = []
    list_search_rider = (request.args.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.args.get('list_search_team', type=str) or '').strip()
    # Backward compatible alias from older query param.
    if not list_search_rider:
        list_search_rider = (request.args.get('list_search', type=str) or '').strip()
    list_search_results = []
    list_item_rider_ids = set()
    list_scope_status = {
        'total_changes': 0,
        'approved_changes': 0,
        'unapproved_changes': 0,
    }
    filter_choices = {
        'categories': [],
        'classifications': [],
        'nations': [],
    }

    category_in_query = 'category' in request.args
    classification_in_query = 'classification' in request.args
    valid_edit_statuses = ('pending_edits', 'pending_approval', 'approved')
    selected_edit_statuses = [
        value
        for value in request.args.getlist('edit_status')
        if value in valid_edit_statuses
    ]

    selected_filters = {
        'race_name': request.args.get('race_name', type=str) or '',
        'category': request.args.get('category', type=str) if category_in_query else default_category,
        'classification': request.args.get('classification', type=str) if classification_in_query else default_classification,
        'nation': request.args.get('nation', type=str) or '',
        'start_date_from': request.args.get('start_date_from', type=str) or '',
        'start_date_to': request.args.get('start_date_to', type=str) or '',
        'is_stage_race': request.args.get('is_stage_race', type=str) or '',
        'edit_statuses': selected_edit_statuses,
    }

    if season_selected and active_method == 'by_race':
        if not selected_filters['start_date_from']:
            selected_filters['start_date_from'] = f'{selected_year}-01-01'
        if not selected_filters['start_date_to']:
            selected_filters['start_date_to'] = f'{selected_year}-12-31'

        base_query = FirstCyclingRace.query.filter_by(
            season_year=selected_year,
            gender=selected_gender,
        )

        filter_choices['categories'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.category).distinct().order_by(FirstCyclingRace.category).all()
            if value
        ]
        filter_choices['classifications'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.classification_label).distinct().order_by(FirstCyclingRace.classification_label).all()
            if value
        ]
        filter_choices['nations'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.nation_name).distinct().order_by(FirstCyclingRace.nation_name).all()
            if value
        ]

        # Apply configured defaults only when filters are missing from URL params,
        # and map to existing option values for stable UI selection.
        if not category_in_query and selected_filters['category']:
            default_category_match = next(
                (
                    value
                    for value in filter_choices['categories']
                    if value.strip().lower() == selected_filters['category'].strip().lower()
                ),
                None,
            )
            selected_filters['category'] = default_category_match or ''

        if not classification_in_query and selected_filters['classification']:
            wanted = selected_filters['classification'].replace(' ', '').strip().lower()
            default_classification_match = next(
                (
                    value
                    for value in filter_choices['classifications']
                    if value.replace(' ', '').strip().lower() == wanted
                ),
                None,
            )
            selected_filters['classification'] = default_classification_match or ''

        query = base_query.options(selectinload(FirstCyclingRace.race_stages))

        if selected_filters['race_name']:
            query = query.filter(FirstCyclingRace.name.ilike(f"%{selected_filters['race_name']}%"))
        if selected_filters['category']:
            query = query.filter(FirstCyclingRace.category == selected_filters['category'])
        if selected_filters['classification']:
            query = query.filter(FirstCyclingRace.classification_label == selected_filters['classification'])
        if selected_filters['nation']:
            query = query.filter(FirstCyclingRace.nation_name == selected_filters['nation'])

        parsed_start_date_from = None
        parsed_start_date_to = None
        if selected_filters['start_date_from']:
            try:
                parsed_start_date_from = datetime.strptime(selected_filters['start_date_from'], '%Y-%m-%d').date()
            except ValueError:
                pass
        if selected_filters['start_date_to']:
            try:
                parsed_start_date_to = datetime.strptime(selected_filters['start_date_to'], '%Y-%m-%d').date()
            except ValueError:
                pass

        if parsed_start_date_from is not None:
            query = query.filter(FirstCyclingRace.start_date >= parsed_start_date_from)
        if parsed_start_date_to is not None:
            query = query.filter(FirstCyclingRace.start_date <= parsed_start_date_to)

        if selected_filters['is_stage_race'] == 'true':
            query = query.filter(FirstCyclingRace.stage_count > 1)
        elif selected_filters['is_stage_race'] == 'false':
            query = query.filter((FirstCyclingRace.stage_count.is_(None)) | (FirstCyclingRace.stage_count <= 1))

        races = query.order_by(
            FirstCyclingRace.start_date.asc().nullslast(),
            FirstCyclingRace.name.asc(),
        ).all()

        race_ids = {
            race.race_id
            for race in races
            if race.race_id is not None
        }
        race_delta_status = _load_namespace_race_delta_status(selected_namespace, race_ids)
        stage_delta_status = _load_namespace_stage_delta_status(selected_namespace, race_ids)

        filtered_races = []
        for race in races:
            result_state = _build_race_result_state(race)
            delta_state = race_delta_status.get(race.race_id) or {
                'total_changes': 0,
                'approved_changes': 0,
            }

            has_changes = delta_state['total_changes'] > 0
            all_approved = has_changes and delta_state['approved_changes'] == delta_state['total_changes']
            has_any_results = result_state['is_result_saved'] or result_state.get('has_partial_stage_results', False)
            is_pending_edits = has_any_results and not has_changes
            is_pending_approval = has_changes and not all_approved
            is_approved = all_approved

            if selected_edit_statuses:
                matches_status_filter = (
                    ('pending_edits' in selected_edit_statuses and is_pending_edits)
                    or ('pending_approval' in selected_edit_statuses and is_pending_approval)
                    or ('approved' in selected_edit_statuses and is_approved)
                )
                if not matches_status_filter:
                    continue

            race_status_map[race.id] = {
                'is_result_downloaded': result_state['is_result_saved'],
                'is_partial_result_downloaded': result_state.get('has_partial_stage_results', False),
                'has_stat_changes': has_changes,
                'is_approved': is_approved,
                'has_namespace': selected_namespace is not None,
            }
            filtered_races.append(race)

            race_stage_result_ids = {
                result.stage_id
                for result in race.race_results
                if result.stage_id is not None
            }

            for stage in race.race_stages:
                stage_lookup_id = _safe_int(stage.id)
                if stage_lookup_id is None:
                    continue

                stage_delta_state = stage_delta_status.get(stage_lookup_id) or {
                    'total_changes': 0,
                    'approved_changes': 0,
                }
                stage_has_changes = stage_delta_state['total_changes'] > 0
                stage_all_approved = (
                    stage_has_changes
                    and stage_delta_state['approved_changes'] == stage_delta_state['total_changes']
                )

                stage_status_map[stage.id] = {
                    'is_result_downloaded': stage.id in race_stage_result_ids,
                    'has_stat_changes': stage_has_changes,
                    'is_approved': stage_all_approved,
                    'has_namespace': selected_namespace is not None,
                }

        races = filtered_races

    if active_method == 'by_list':
        list_entries = _load_namespace_stat_lists(selected_namespace)

    return render_template(
        'stats_editor.html',
        active_method=active_method,
        selected_namespace=selected_namespace,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        races=races,
        race_status_map=race_status_map,
        stage_status_map=stage_status_map,
        list_entries=list_entries,
        selected_list_id=selected_list_id,
        selected_list=selected_list,
        list_riders=list_riders,
        list_item_rider_ids=list_item_rider_ids,
        list_search_rider=list_search_rider,
        list_search_team=list_search_team,
        list_search_results=list_search_results,
        list_scope_status=list_scope_status,
        filter_choices=filter_choices,
        selected_filters=selected_filters,
    )


@web_bp.route('/stats-editor/lists/create', methods=['POST'])
@login_required
def stats_editor_create_list():
    if not _can_access_panel('stat_manager'):
        abort(403)

    list_name = (request.form.get('list_name', type=str) or '').strip()
    if not list_name:
        flash('List Name is required.', 'warning')
        return redirect(url_for('web.stats_editor', method='by_list'))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor', method='by_list'))

    season_year, gender = _resolve_namespace_season(selected_namespace)
    if season_year is None or not gender:
        flash('Selected namespace must have Database Year and Gender before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor', method='by_list'))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            stat_list_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'stat_lists'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                raise RuntimeError('stat_lists table is missing in namespace database')

            insert_columns = ['list_name']
            insert_values = [':list_name']
            insert_params = {'list_name': list_name}

            if 'season_year' in stat_list_columns:
                insert_columns.append('season_year')
                insert_values.append(':season_year')
                insert_params['season_year'] = season_year

            if 'gender' in stat_list_columns:
                insert_columns.append('gender')
                insert_values.append(':gender')
                insert_params['gender'] = gender

            if 'created_by_id' in stat_list_columns:
                insert_columns.append('created_by_id')
                insert_values.append(':created_by_id')
                insert_params['created_by_id'] = current_user.id

            if 'created_at' in stat_list_columns:
                insert_columns.append('created_at')
                insert_values.append('CURRENT_TIMESTAMP')

            row = conn.execute(
                text(
                    "INSERT INTO stat_lists ({columns}) VALUES ({values}) RETURNING id".format(
                        columns=', '.join(insert_columns),
                        values=', '.join(insert_values),
                    )
                ),
                insert_params,
            ).mappings().first()
        new_list_id = row['id'] if row else None
    except Exception:
        logger.exception('Failed to create stat list in namespace %s', selected_namespace.slug)
        flash('Failed to create stat list.', 'error')
        return redirect(url_for('web.stats_editor', method='by_list'))
    finally:
        engine.dispose()

    flash(f"Created list '{list_name}'.", 'success')
    return redirect(url_for('web.stats_editor_list_detail', list_id=new_list_id))


@web_bp.route('/stats-editor/lists/<int:list_id>/add-rider', methods=['POST'])
@login_required
def stats_editor_add_list_rider(list_id):
    if not _can_access_panel('stat_manager'):
        abort(403)

    rider_id = request.form.get('rider_id', type=int)
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if rider_id is None:
        flash('Select a rider to add.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before adding riders.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO stat_list_items (stat_list_id, first_cycling_rider_id)
                    VALUES (:list_id, :rider_id)
                    ON CONFLICT (stat_list_id, first_cycling_rider_id)
                    DO NOTHING
                    """
                ),
                {
                    'list_id': list_id,
                    'rider_id': rider_id,
                },
            )
    except Exception:
        logger.exception('Failed to add rider to stat list in namespace %s', selected_namespace.slug)
        flash('Failed to add rider to list.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


@web_bp.route('/stats-editor/lists/<int:list_id>/toggle-rider', methods=['POST'])
@login_required
def stats_editor_toggle_list_rider(list_id):
    if not _can_access_panel('stat_manager'):
        abort(403)

    rider_id = request.form.get('rider_id', type=int)
    is_checked = request.form.get('is_checked', type=str) == '1'
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if rider_id is None:
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before editing list riders.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            if is_checked:
                conn.execute(
                    text(
                        """
                        INSERT INTO stat_list_items (stat_list_id, first_cycling_rider_id)
                        VALUES (:list_id, :rider_id)
                        ON CONFLICT (stat_list_id, first_cycling_rider_id)
                        DO NOTHING
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE stat_list_id = :list_id
                          AND first_cycling_race_id IS NULL
                          AND first_cycling_stage_id IS NULL
                          AND first_cycling_rider_id = :rider_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                    },
                )
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_list_items
                        WHERE stat_list_id = :list_id
                          AND first_cycling_rider_id = :rider_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                    },
                )
    except Exception:
        logger.exception('Failed to toggle rider in stat list in namespace %s', selected_namespace.slug)
        flash('Failed to update list rider selection.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


@web_bp.route('/stats-editor/lists/<int:list_id>')
@login_required
def stats_editor_list_detail(list_id):
    if not _can_access_panel('stat_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        abort(404)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    list_row = None
    list_items = []
    try:
        with engine.connect() as conn:
            list_row = conn.execute(
                text(
                    """
                    SELECT id, list_name, season_year, gender
                    FROM stat_lists
                    WHERE id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().first()
            list_items = conn.execute(
                text(
                    """
                    SELECT first_cycling_rider_id
                    FROM stat_list_items
                    WHERE stat_list_id = :list_id
                    ORDER BY id ASC
                    """
                ),
                {'list_id': list_id},
            ).mappings().all()
    finally:
        engine.dispose()

    if not list_row:
        abort(404)

    list_search_rider = (request.args.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.args.get('list_search_team', type=str) or '').strip()
    list_search_results = []

    rider_ids = [item['first_cycling_rider_id'] for item in list_items if item.get('first_cycling_rider_id') is not None]
    list_item_rider_ids = set(rider_ids)
    riders = []
    rider_map = {}
    if rider_ids:
        riders = FirstCyclingRider.query.filter(FirstCyclingRider.rider_id.in_(rider_ids)).all()
        rider_map = {r.rider_id: r for r in riders}

    if list_search_rider or list_search_team:
        query = FirstCyclingRider.query

        list_gender = list_row.get('gender') if hasattr(list_row, 'get') else None
        normalized_gender = _normalize_gender_token(list_gender)
        if normalized_gender:
            gender_tokens = [normalized_gender]
            if normalized_gender == 'men':
                gender_tokens.extend(['m', 'male', 'man'])
            elif normalized_gender == 'women':
                gender_tokens.extend(['w', 'f', 'female', 'woman', 'ladies'])

            query = query.filter(
                or_(
                    FirstCyclingRider.gender.is_(None),
                    func.lower(FirstCyclingRider.gender).in_(gender_tokens),
                )
            )

        if list_search_rider:
            rider_term = f'%{list_search_rider}%'
            query = query.filter(
                or_(
                    FirstCyclingRider.name.ilike(rider_term),
                    FirstCyclingRider.first_name.ilike(rider_term),
                    FirstCyclingRider.last_name.ilike(rider_term),
                )
            )

        if list_search_team:
            query = query.filter(FirstCyclingRider.team_name.ilike(f'%{list_search_team}%'))

        list_search_results = query.order_by(
            FirstCyclingRider.last_name.asc().nullslast(),
            FirstCyclingRider.first_name.asc().nullslast(),
            FirstCyclingRider.name.asc().nullslast(),
            FirstCyclingRider.rider_id.asc(),
        ).limit(50).all()

    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, rider_ids)
    rider_delta_map = _load_namespace_list_result_deltas(selected_namespace, list_id, rider_ids)
    scope_delta_status = _load_namespace_scope_delta_status(
        selected_namespace,
        first_cycling_race_id=None,
        first_cycling_stage_id=None,
        stat_list_id=list_id,
    )

    rows = []
    for rider_id in rider_ids:
        rider = rider_map.get(rider_id)
        rider_name = f'Rider #{rider_id}' if rider is None else (rider.name or ' '.join([part for part in [rider.first_name, rider.last_name] if part]).strip() or f'Rider #{rider_id}')

        rider_in_namespace = rider_id in rider_stats_map
        if rider_in_namespace:
            base_stats = rider_stats_map.get(rider_id) or {}
            original_stats = dict(base_stats)
            final_stats = dict(base_stats)
            for stat_name, delta_info in (rider_delta_map.get(rider_id) or {}).items():
                final_value = delta_info.get('final_value')
                if final_value is None:
                    continue
                final_stats[stat_name] = final_value
            for stat_name in STAT_DELTA_FIELDS:
                if original_stats.get(stat_name) is None:
                    original_stats[stat_name] = 0
                if final_stats.get(stat_name) is None:
                    final_stats[stat_name] = 0
        else:
            original_stats = {}
            final_stats = {}

        rows.append({
            'rider_id': rider_id,
            'rider_name': rider_name,
            'team_id': rider.team_id if rider is not None else None,
            'team_name': rider.team_name if rider is not None and rider.team_name else '-',
            'rider_in_namespace': rider_in_namespace,
            'stats': final_stats,
            'original_stats': original_stats,
        })

    return render_template(
        'stat_list_detail.html',
        stat_list=list_row,
        rows=rows,
        list_search_rider=list_search_rider,
        list_search_team=list_search_team,
        list_search_results=list_search_results,
        list_item_rider_ids=list_item_rider_ids,
        selected_namespace=selected_namespace,
        scope_delta_status=scope_delta_status,
    )


@web_bp.route('/stats-editor/lists/<int:list_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_list_stat_deltas(list_id):
    if not _can_access_panel('stat_manager'):
        abort(403)

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    rider_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    rider_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        rider_to_pcm_id = {}
        if rider_ids:
            with engine.connect() as conn:
                rider_rows = conn.execute(
                    text(
                        """
                        SELECT id, first_cycling_id
                        FROM cdb_cyclist
                        WHERE first_cycling_id = ANY(:rider_ids)
                        """
                    ),
                    {'rider_ids': list(rider_ids)},
                ).mappings().all()
            rider_to_pcm_id = {row['first_cycling_id']: row['id'] for row in rider_rows}

        saved_count = 0
        skipped_count = 0
        with engine.begin() as conn:
            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                rider_id = _safe_int(item.get('first_cycling_rider_id'))
                stat_name = (item.get('stat_name') or '').strip().lower()
                original_value = _safe_int(item.get('original_value'))
                final_value = _safe_int(item.get('final_value'))

                if (
                    rider_id is None
                    or stat_name not in STAT_DELTA_FIELDS
                    or original_value is None
                    or final_value is None
                    or final_value < STAT_MIN_VALUE
                    or final_value > STAT_MAX_VALUE
                ):
                    skipped_count += 1
                    continue

                cyclist_pcm_id = rider_to_pcm_id.get(rider_id)
                if cyclist_pcm_id is None:
                    skipped_count += 1
                    continue

                delta_value = final_value - original_value
                if delta_value == 0:
                    conn.execute(
                        text(
                            """
                            DELETE FROM stat_changes
                            WHERE first_cycling_race_id IS NULL
                              AND first_cycling_stage_id IS NULL
                              AND stat_list_id = :list_id
                              AND cyclist_pcm_id = :cyclist_pcm_id
                              AND first_cycling_rider_id = :rider_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'list_id': list_id,
                            'cyclist_pcm_id': cyclist_pcm_id,
                            'rider_id': rider_id,
                            'stat_name': stat_name,
                        },
                    )
                    continue

                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE first_cycling_race_id IS NULL
                          AND first_cycling_stage_id IS NULL
                          AND stat_list_id = :list_id
                          AND cyclist_pcm_id = :cyclist_pcm_id
                          AND first_cycling_rider_id = :rider_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cyclist_pcm_id': cyclist_pcm_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            first_cycling_race_id,
                            first_cycling_stage_id,
                            stat_list_id,
                            cyclist_pcm_id,
                            first_cycling_rider_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value,
                            is_approved
                        )
                        VALUES (
                            NULL,
                            NULL,
                            :list_id,
                            :cyclist_pcm_id,
                            :rider_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            FALSE
                        )
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cyclist_pcm_id': cyclist_pcm_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                    },
                )
                saved_count += 1

        if saved_count > 0:
            flash(f'Saved {saved_count} stat change(s).', 'success')
        else:
            flash('No stat changes were saved.', 'info')
        if skipped_count > 0:
            flash(f'Skipped {skipped_count} invalid or unmatched change(s).', 'warning')

    except Exception:
        logger.exception('Failed to save stat edits for list %s', list_id)
        flash('Failed to save stat edits.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))


@web_bp.route('/stats-editor/lists/<int:list_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_list_stat_deltas(list_id):
    if not _can_access_panel('stat_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    approved_count = 0
    applied_count = 0
    try:
        with engine.begin() as conn:
            applied_count = _apply_scope_stat_changes_to_cdb(
                conn,
                """
                first_cycling_race_id IS NULL
                AND first_cycling_stage_id IS NULL
                AND stat_list_id = :list_id
                """,
                {'list_id': list_id},
            )
            result = conn.execute(
                text(
                    """
                    UPDATE stat_changes
                    SET is_approved = TRUE
                    WHERE first_cycling_race_id IS NULL
                      AND first_cycling_stage_id IS NULL
                      AND stat_list_id = :list_id
                      AND is_approved = FALSE
                    """
                ),
                {'list_id': list_id},
            )
            approved_count = result.rowcount or 0
    except Exception:
        logger.exception('Failed to approve stat changes for list %s', list_id)
        flash('Failed to approve stat changes.', 'error')
    finally:
        engine.dispose()

    if approved_count > 0:
        flash(f'Approved {approved_count} stat change(s) and applied {applied_count} update(s) to CDB rider stats.', 'success')
    else:
        flash('No pending stat changes to approve.', 'info')

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))


@web_bp.route('/releases', methods=['GET', 'POST'])
@login_required
def releases():
    """Releases panel with tabbed sections and overview actions."""
    if not _can_access_panel('release_manager'):
        abort(403)

    valid_tabs = ('catalog', 'create', 'overview', 'stats', 'palmares', 'photos')
    active_tab = (request.args.get('tab', type=str) or 'catalog').strip().lower()
    if active_tab not in valid_tabs:
        return redirect(url_for('web.releases', tab='catalog'))
    detail_tabs = ('overview', 'stats', 'palmares', 'photos')

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)

    selected_release_id = request.args.get('release_id', type=int)

    if request.method == 'POST' and active_tab == 'create':
        if selected_namespace is None:
            flash('Select a namespace before creating a release.', 'warning')
            return redirect(url_for('web.releases', tab='create'))

        version = (request.form.get('version', type=str) or '').strip()
        release_name = (request.form.get('release_name', type=str) or '').strip() or None
        release_type = (request.form.get('release_type', type=str) or 'minor').strip().lower()
        release_notes = (request.form.get('release_notes', type=str) or '').strip() or None

        if not version:
            flash('Release version is required.', 'error')
            return redirect(url_for('web.releases', tab='create'))

        if release_type not in ('major', 'minor', 'patch', 'beta'):
            release_type = 'minor'

        # Output SQLite path is produced when moving draft -> review; keep a draft placeholder.
        sqlite_file_path = ''

        release = Release(
            namespace_id=selected_namespace.id,
            version=version,
            release_name=release_name,
            release_type=release_type,
            sqlite_file_path=sqlite_file_path,
            status='draft',
            release_notes=release_notes,
            created_by_id=current_user.id,
        )

        try:
            db.session.add(release)
            db.session.commit()
            selected_release_id = release.id
            flash(f'Release {version} created as draft.', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('Release version already exists for this namespace.', 'error')
            return redirect(url_for('web.releases', tab='create'))
        except Exception:
            db.session.rollback()
            logger.exception('Failed to create release')
            flash('Failed to create release.', 'error')
            return redirect(url_for('web.releases', tab='create'))

        return redirect(url_for('web.releases', tab='catalog'))

    releases = []
    summary = {
        'total': 0,
        'draft': 0,
        'published': 0,
        'archived': 0,
    }

    if selected_namespace is not None:
        releases = Release.query.filter_by(
            namespace_id=selected_namespace.id
        ).order_by(Release.created_at.desc()).all()

        summary['total'] = len(releases)
        summary['draft'] = sum(1 for item in releases if item.status == 'draft')
        summary['published'] = sum(1 for item in releases if item.status == 'published')
        summary['archived'] = sum(1 for item in releases if item.status == 'archived')

    release_by_id = {item.id: item for item in releases}

    namespace_release_selections = session.get('selected_release_id_by_namespace', {})
    namespace_key = str(selected_namespace.id) if selected_namespace is not None else None

    if selected_release_id is None and namespace_key and active_tab in detail_tabs:
        selected_release_id = _safe_int(namespace_release_selections.get(namespace_key))

    selected_release = release_by_id.get(selected_release_id)
    if selected_release_id is not None and selected_release is None:
        selected_release_id = None

    if namespace_key and active_tab in detail_tabs:
        if selected_release_id is None:
            namespace_release_selections.pop(namespace_key, None)
        else:
            namespace_release_selections[namespace_key] = selected_release_id
        session['selected_release_id_by_namespace'] = namespace_release_selections

    if active_tab in detail_tabs and selected_release is None:
        flash('Select a release from the Releases page first.', 'warning')
        return redirect(url_for('web.releases', tab='catalog'))

    if request.method == 'POST' and active_tab == 'overview':
        if request.form.get('save_release_notes'):
            selected_release.release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
            try:
                db.session.commit()
                flash('Release notes updated.', 'success')
            except Exception:
                db.session.rollback()
                logger.exception('Failed to update release notes for release %s', selected_release.id)
                flash('Failed to update release notes.', 'error')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        current_status = (selected_release.status or 'draft').strip().lower()
        requested_target_status = (request.form.get('status_target', type=str) or '').strip().lower()
        if not requested_target_status:
            # Backward compatibility for older form payloads.
            requested_action = (request.form.get('status_action', type=str) or '').strip().lower()
            action_to_target = {
                'review': 'review',
                'approve': 'approved',
                'publish': 'published',
                'draft': 'draft',
            }
            requested_target_status = action_to_target.get(requested_action, '')

        allowed_transitions = {
            'draft': {'review'},
            'review': {'draft', 'approved'},
            'approved': {'published'},
            'published': set(),
        }

        next_status = requested_target_status
        if next_status not in allowed_transitions.get(current_status, set()):
            flash('Invalid release status transition.', 'warning')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        review_build_result = None
        try:
            if current_status == 'draft' and next_status == 'review':
                review_build_result = _build_review_artifacts_for_release(selected_namespace, selected_release)
                sqlite_file = review_build_result['sqlite_file']
                selected_release.sqlite_file_path = sqlite_file['relative_path']
                selected_release.file_size_bytes = sqlite_file['file_size_bytes']
                selected_release.md5_checksum = sqlite_file['md5_checksum']
                selected_release.sha256_checksum = sqlite_file['sha256_checksum']

            selected_release.status = next_status
            if next_status == 'published':
                selected_release.published_by_id = current_user.id
                selected_release.published_at = datetime.utcnow()
            db.session.commit()

            if review_build_result is not None:
                flash(
                    (
                        f'Release files generated with '
                        f'{review_build_result["applied_changes"]} applied stat change(s): '
                        f'{len(review_build_result["files"])} file(s)'
                    ),
                    'success',
                )
            flash(f'Release moved to {next_status.title()}.', 'success')
        except ValueError as err:
            db.session.rollback()
            flash(str(err), 'error')
        except Exception:
            db.session.rollback()
            logger.exception('Failed to update release lifecycle status for release %s', selected_release.id)
            flash('Failed to update release status.', 'error')

        return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

    stats_approved_items = []
    included_item_keys = set()
    release_files = []

    if selected_namespace is not None and active_tab == 'stats':
        year, gender = _resolve_namespace_season(selected_namespace)
        stats_approved_items = _load_namespace_approved_release_items(
            selected_namespace,
            season_year=year,
            gender=gender,
            selected_release_id=selected_release.id,
        )
        included_item_keys = {
            item['item_key']
            for item in stats_approved_items
            if item.get('is_selected')
        }

    if selected_namespace is not None and selected_release is not None and active_tab == 'overview':
        release_files = _load_namespace_release_versions(selected_namespace, selected_release.id)

    if request.method == 'POST' and active_tab == 'stats':
        if selected_namespace is None:
            flash('Select a namespace before editing release stats.', 'warning')
            return redirect(url_for('web.releases', tab='stats', release_id=selected_release_id))

        available_item_keys = {
            item['item_key']
            for item in stats_approved_items
            if item.get('item_key')
        }
        selected_values = request.form.getlist('included_item_keys')
        valid_selected_keys = {
            item_key
            for item_key in selected_values
            if item_key in available_item_keys
        }

        try:
            updated_rows = _sync_release_item_keys(
                selected_namespace,
                selected_release.id,
                valid_selected_keys,
                available_item_keys,
            )
            flash(
                f'Synced {updated_rows} stat change row assignment(s) for release {selected_release.version}.',
                'success',
            )
        except Exception:
            logger.exception('Failed to assign release stat changes for release %s', selected_release.id)
            flash('Failed to assign selected items to this release.', 'error')

        return redirect(url_for('web.releases', tab='stats', release_id=selected_release.id))

    return render_template(
        'releases.html',
        active_tab=active_tab,
        selected_namespace=selected_namespace,
        releases=releases,
        selected_release=selected_release,
        selected_release_id=selected_release_id,
        summary=summary,
        stats_approved_items=stats_approved_items,
        included_item_keys=included_item_keys,
        release_files=release_files,
    )


@web_bp.route('/releases/<int:release_id>/files/<int:file_id>/download')
@login_required
def release_file_download(release_id, file_id):
    if not _can_access_panel('release_manager'):
        abort(403)

    release = Release.query.get_or_404(release_id)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None or release.namespace_id != selected_namespace.id:
        abort(404)

    release_files = _load_namespace_release_versions(selected_namespace, release.id)
    release_file = next((item for item in release_files if _safe_int(item.get('id')) == file_id), None)
    if release_file is None:
        abort(404)

    absolute_path = _resolve_namespace_release_file_path(selected_namespace, release_file.get('relative_path'))
    if not absolute_path:
        flash('Release file could not be found on disk.', 'error')
        return redirect(url_for('web.releases', tab='overview', release_id=release.id))

    return send_file(
        absolute_path,
        as_attachment=True,
        download_name=release_file.get('display_name') or os.path.basename(absolute_path),
        mimetype=release_file.get('content_type') or None,
    )


@web_bp.route('/stats-editor/races/<int:race_id>')
@web_bp.route('/stats-editor/races/<int:race_id>/<int:stage_id>')
@login_required
def stats_editor_race_detail(race_id, stage_id=None):
    """Race detail page with optional stage-specific view."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.options(
        selectinload(FirstCyclingRace.race_stages),
        selectinload(FirstCyclingRace.race_results),
    ).filter_by(race_id=race_id).first_or_404()

    stage_rows = sorted(
        race.race_stages,
        key=lambda stage: (
            stage.stage_number is None,
            stage.stage_number if stage.stage_number is not None else 99999,
        ),
    )
    stage_lookup = {stage.id: stage for stage in stage_rows if stage.id is not None}
    selected_stage = stage_lookup.get(stage_id) if stage_id is not None else None

    result_state = _build_race_result_state(race)

    if selected_stage is not None:
        result_rows = [result for result in race.race_results if result.stage_id == selected_stage.id]
        result_title = f"Stage #{selected_stage.stage_number or '?'} Results"
    else:
        race_scope_rows = [
            result
            for result in race.race_results
            if result.result_scope == 'race' and result.stage_id is None
        ]
        if race_scope_rows:
            result_rows = race_scope_rows
        else:
            final_stage = None
            if stage_rows:
                final_stage = max(
                    stage_rows,
                    key=lambda stage: stage.stage_number if stage.stage_number is not None else -1,
                )
            if final_stage is None:
                result_rows = []
            else:
                result_rows = [result for result in race.race_results if result.stage_id == final_stage.id]
        result_title = 'Overall Results'

    rider_ids = {result.rider_id for result in result_rows if result.rider_id is not None}
    rider_map = {}
    if rider_ids:
        riders = FirstCyclingRider.query.filter(FirstCyclingRider.rider_id.in_(rider_ids)).all()
        rider_map = {rider.rider_id: rider for rider in riders}

    team_ids = {
        result.team_id
        for result in result_rows
        if result.team_id is not None
    }
    team_ids.update(
        rider.team_id
        for rider in rider_map.values()
        if rider.team_id is not None
    )
    team_map = {}
    if team_ids:
        teams = FirstCyclingTeam.query.filter(FirstCyclingTeam.team_id.in_(team_ids)).all()
        team_map = {team.team_id: team for team in teams}

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    stage_status_map = {}
    if selected_namespace is not None and race.race_id is not None:
        raw_stage_status = _load_namespace_stage_delta_status(selected_namespace, {race.race_id})
        saved_stage_ids = {
            result.stage_id
            for result in race.race_results
            if result.stage_id is not None
        }
        for stage in stage_rows:
            if stage.id is None:
                continue
            delta_state = raw_stage_status.get(stage.id) or {
                'total_changes': 0,
                'approved_changes': 0,
            }
            has_changes = delta_state['total_changes'] > 0
            is_approved = has_changes and delta_state['approved_changes'] == delta_state['total_changes']
            stage_status_map[stage.id] = {
                'is_result_downloaded': stage.id in saved_stage_ids,
                'has_stat_changes': has_changes,
                'is_approved': is_approved,
            }

    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, rider_ids)
    stage_scope_id = selected_stage.id if selected_stage is not None else None
    scope_delta_status = _load_namespace_scope_delta_status(selected_namespace, race.race_id, stage_scope_id)
    rider_delta_map = _load_namespace_race_result_deltas(selected_namespace, race.race_id, stage_scope_id, rider_ids)
    delta_change_count = sum(len(changes) for changes in rider_delta_map.values())

    display_rows = []
    for result in result_rows:
        rider = rider_map.get(result.rider_id)
        team = team_map.get(result.team_id)
        if team is None and rider is not None and rider.team_id is not None:
            team = team_map.get(rider.team_id)

        rider_name = f'Rider #{result.rider_id}'
        if rider is not None:
            rider_name = rider.name or ' '.join(
                part for part in [rider.first_name, rider.last_name] if part
            ).strip() or rider_name

        rider_in_namespace = result.rider_id in rider_stats_map
        if rider_in_namespace:
            base_stats = rider_stats_map.get(result.rider_id) or {}
            original_stats = dict(base_stats)
            final_stats = dict(base_stats)
            for stat_name, delta_info in (rider_delta_map.get(result.rider_id) or {}).items():
                original_value = delta_info.get('original_value')
                final_value = delta_info.get('final_value')
                if original_value is not None:
                    original_stats[stat_name] = original_value
                if final_value is None:
                    continue
                final_stats[stat_name] = final_value

            # Keep a complete stat set for rendering/editing so every column shows a value.
            for stat_name in STAT_DELTA_FIELDS:
                if original_stats.get(stat_name) is None:
                    original_stats[stat_name] = 0
                if final_stats.get(stat_name) is None:
                    final_stats[stat_name] = 0
        else:
            original_stats = {}
            final_stats = {}

        display_rows.append({
            'result': result,
            'rider_name': rider_name,
            'rider_in_namespace': rider_in_namespace,
            'team_id': team.team_id if team is not None else (rider.team_id if rider is not None else result.team_id),
            'team_name': team.team_name if team is not None else (rider.team_name if rider is not None and rider.team_name else '-'),
            'stats': final_stats,
            'original_stats': original_stats,
        })

    if selected_stage is not None:
        display_rows = sorted(
            display_rows,
            key=lambda row: (
                row['result'].stage_position is None,
                row['result'].stage_position if row['result'].stage_position is not None else 99999,
            ),
        )
    else:
        display_rows = sorted(
            display_rows,
            key=lambda row: (
                row['result'].overall_position is None,
                row['result'].overall_position if row['result'].overall_position is not None else 99999,
            ),
        )

    return render_template(
        'stat_race_detail.html',
        race=race,
        stage_rows=stage_rows,
        selected_stage=selected_stage,
        result_rows=display_rows,
        result_title=result_title,
        result_state=result_state,
        delta_change_count=delta_change_count,
        scope_delta_status=scope_delta_status,
        stage_status_map=stage_status_map,
        selected_namespace=selected_namespace,
        back_params={
            'race_name': request.args.get('race_name', type=str) or '',
            'category': request.args.get('category', type=str) or '',
            'classification': request.args.get('classification', type=str) or '',
            'nation': request.args.get('nation', type=str) or '',
            'start_date_from': request.args.get('start_date_from', type=str) or '',
            'start_date_to': request.args.get('start_date_to', type=str) or '',
            'is_stage_race': request.args.get('is_stage_race', type=str) or '',
            'edit_statuses': request.args.getlist('edit_status'),
        },
    )


@web_bp.route('/namespace/select', methods=['POST'])
@login_required
def select_namespace():
    """Persist the selected namespace in session for cross-panel usage."""
    namespace_id = request.form.get('namespace_id', type=int)
    next_url = request.form.get('next') or request.referrer or url_for('web.dashboard')

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = None
    if namespace_id is not None:
        for namespace in available_namespaces:
            if namespace.id == namespace_id:
                selected_namespace = namespace
                break

    if selected_namespace is None:
        flash('Invalid namespace selection.', 'warning')
        return redirect(next_url)

    session['selected_namespace_id'] = selected_namespace.id
    return redirect(next_url)


@web_bp.route('/stats-editor/races/<int:race_id>/get-result', methods=['POST'])
@login_required
def stats_editor_get_result(race_id):
    """Fetch and persist race results for one race from FirstCycling."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

    category = request.form.get('category', type=str) or ''
    race_name = request.form.get('race_name', type=str) or ''
    classification = request.form.get('classification', type=str) or ''
    nation = request.form.get('nation', type=str) or ''
    start_date_from = request.form.get('start_date_from', type=str) or ''
    start_date_to = request.form.get('start_date_to', type=str) or ''
    is_stage_race_filter = request.form.get('is_stage_race', type=str) or ''
    edit_status_filters = request.form.getlist('edit_status')
    stage_id = request.form.get('stage_id', type=int)

    try:
        client = FirstCyclingClient()
        summary = client.sync_race_results(race_id=race.race_id, season_year=race.season_year, commit=True)
        flash(
            (
                f"Saved race results for '{race.name or race.race_id}' "
                f"(overall={summary['overall_results']}, stage={summary['stage_results']})."
            ),
            'success',
        )
    except ValueError as exc:
        flash(f'FirstCycling integration is not configured: {exc}', 'error')
    except FirstCyclingAPIError as exc:
        flash(f'Could not fetch FirstCycling results: {exc}', 'error')
    except Exception as exc:
        flash(f'Unexpected error while saving race results: {exc}', 'error')

    if stage_id is not None:
        return redirect(url_for(
            'web.stats_editor_race_detail',
            race_id=race.race_id,
            stage_id=stage_id,
            race_name=race_name,
            category=category,
            classification=classification,
            nation=nation,
            start_date_from=start_date_from,
            start_date_to=start_date_to,
            is_stage_race=is_stage_race_filter,
            edit_status=edit_status_filters,
        ))

    return redirect(url_for(
        'web.stats_editor_race_detail',
        race_id=race.race_id,
        race_name=race_name,
        category=category,
        classification=classification,
        nation=nation,
        start_date_from=start_date_from,
        start_date_to=start_date_to,
        is_stage_race=is_stage_race_filter,
        edit_status=edit_status_filters,
    ))


@web_bp.route('/stats-editor/races/<int:race_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_stat_deltas(race_id):
    """Persist edited stat deltas for this race into the selected namespace database."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

    category = request.form.get('category', type=str) or ''
    race_name = request.form.get('race_name', type=str) or ''
    classification = request.form.get('classification', type=str) or ''
    nation = request.form.get('nation', type=str) or ''
    start_date_from = request.form.get('start_date_from', type=str) or ''
    start_date_to = request.form.get('start_date_to', type=str) or ''
    is_stage_race_filter = request.form.get('is_stage_race', type=str) or ''
    edit_status_filters = request.form.getlist('edit_status')
    stage_id = request.form.get('stage_id', type=int)
    stage_scope_id = stage_id

    redirect_target = {
        'race_id': race.race_id,
        'race_name': race_name,
        'category': category,
        'classification': classification,
        'nation': nation,
        'start_date_from': start_date_from,
        'start_date_to': start_date_to,
        'is_stage_race': is_stage_race_filter,
        'edit_status': edit_status_filters,
    }
    if stage_id is not None:
        redirect_target['stage_id'] = stage_id

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    rider_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    rider_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)

    try:

        rider_to_pcm_id = {}
        if rider_ids:
            with engine.connect() as conn:
                rider_rows = conn.execute(
                    text(
                        """
                        SELECT id, first_cycling_id
                        FROM cdb_cyclist
                        WHERE first_cycling_id = ANY(:rider_ids)
                        """
                    ),
                    {'rider_ids': list(rider_ids)},
                ).mappings().all()
            rider_to_pcm_id = {
                row['first_cycling_id']: row['id']
                for row in rider_rows
            }

        saved_count = 0
        skipped_count = 0

        with engine.begin() as conn:
            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                rider_id = _safe_int(item.get('first_cycling_rider_id'))
                stat_name = (item.get('stat_name') or '').strip().lower()
                original_value = _safe_int(item.get('original_value'))
                final_value = _safe_int(item.get('final_value'))

                if (
                    rider_id is None
                    or stat_name not in STAT_DELTA_FIELDS
                    or original_value is None
                    or final_value is None
                    or final_value < STAT_MIN_VALUE
                    or final_value > STAT_MAX_VALUE
                ):
                    skipped_count += 1
                    continue

                cyclist_pcm_id = rider_to_pcm_id.get(rider_id)
                if cyclist_pcm_id is None:
                    skipped_count += 1
                    continue

                delta_value = final_value - original_value
                if delta_value == 0:
                    conn.execute(
                        text(
                            """
                                                        DELETE FROM stat_changes
                            WHERE first_cycling_race_id = :race_id
                              AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                              AND stat_list_id IS NULL
                              AND cyclist_pcm_id = :cyclist_pcm_id
                              AND first_cycling_rider_id = :rider_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'race_id': race.race_id,
                            'stage_id': stage_scope_id,
                            'cyclist_pcm_id': cyclist_pcm_id,
                            'rider_id': rider_id,
                            'stat_name': stat_name,
                        },
                    )
                    continue

                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE first_cycling_race_id = :race_id
                          AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                          AND stat_list_id IS NULL
                          AND cyclist_pcm_id = :cyclist_pcm_id
                          AND first_cycling_rider_id = :rider_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'race_id': race.race_id,
                        'stage_id': stage_scope_id,
                        'cyclist_pcm_id': cyclist_pcm_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            first_cycling_race_id,
                            first_cycling_stage_id,
                            stat_list_id,
                            cyclist_pcm_id,
                            first_cycling_rider_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value,
                            is_approved
                        )
                        VALUES (
                            :race_id,
                            :stage_id,
                            NULL,
                            :cyclist_pcm_id,
                            :rider_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            FALSE
                        )
                        """
                    ),
                    {
                        'race_id': race.race_id,
                        'stage_id': stage_scope_id,
                        'cyclist_pcm_id': cyclist_pcm_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                    },
                )
                saved_count += 1

        if saved_count > 0:
            flash(f'Saved {saved_count} stat edit(s).', 'success')
        else:
            flash('No stat delta changes were saved.', 'info')

        if skipped_count > 0:
            flash(f'Skipped {skipped_count} invalid or unmatched change(s).', 'warning')

    except Exception:
        logger.exception('Failed to save stat deltas for race %s', race.race_id)
        flash('Failed to save stat delta changes.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_race_detail', **redirect_target))


@web_bp.route('/stats-editor/races/<int:race_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_stat_deltas(race_id):
    """Mark saved stat deltas as approved for this race or selected stage scope."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

    category = request.form.get('category', type=str) or ''
    race_name = request.form.get('race_name', type=str) or ''
    classification = request.form.get('classification', type=str) or ''
    nation = request.form.get('nation', type=str) or ''
    start_date_from = request.form.get('start_date_from', type=str) or ''
    start_date_to = request.form.get('start_date_to', type=str) or ''
    is_stage_race_filter = request.form.get('is_stage_race', type=str) or ''
    edit_status_filters = request.form.getlist('edit_status')
    stage_id = request.form.get('stage_id', type=int)
    stage_scope_id = stage_id

    redirect_target = {
        'race_id': race.race_id,
        'race_name': race_name,
        'category': category,
        'classification': classification,
        'nation': nation,
        'start_date_from': start_date_from,
        'start_date_to': start_date_to,
        'is_stage_race': is_stage_race_filter,
        'edit_status': edit_status_filters,
    }
    if stage_id is not None:
        redirect_target['stage_id'] = stage_id

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    applied_count = 0
    try:
        with engine.begin() as conn:
            applied_count = _apply_scope_stat_changes_to_cdb(
                conn,
                """
                first_cycling_race_id = :race_id
                AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                AND stat_list_id IS NULL
                """,
                {
                    'race_id': race.race_id,
                    'stage_id': stage_scope_id,
                },
            )
            result = conn.execute(
                text(
                    """
                                        UPDATE stat_changes
                    SET is_approved = TRUE
                    WHERE first_cycling_race_id = :race_id
                      AND first_cycling_stage_id IS NOT DISTINCT FROM :stage_id
                      AND stat_list_id IS NULL
                      AND is_approved = FALSE
                    """
                ),
                {
                    'race_id': race.race_id,
                    'stage_id': stage_scope_id,
                },
            )
        approved_count = result.rowcount or 0
    except Exception:
        logger.exception('Failed to approve stat deltas for race %s (stage=%s)', race.race_id, stage_scope_id)
        flash('Failed to approve stat changes.', 'error')
    finally:
        engine.dispose()

    if approved_count > 0:
        flash(f'Approved {approved_count} stat edit(s) and applied {applied_count} update(s) to CDB rider stats.', 'success')
    else:
        flash('No pending stat edit to approve.', 'info')

    return redirect(url_for('web.stats_editor_race_detail', **redirect_target))


# ==================== Admin Panel Routes ====================

@web_bp.route('/admin')
@login_required
@admin_required
def admin():
    """Admin panel home - redirect to users section"""
    return redirect(url_for('web.admin_users'))


@web_bp.route('/admin/users')
@login_required
@admin_required
def admin_users():
    """Admin panel - Users"""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users, active_section='users')


@web_bp.route('/admin/namespaces')
@login_required
@admin_required
def admin_namespaces():
    """Admin panel - Namespaces"""
    namespaces = Namespace.query.order_by(Namespace.created_at.desc()).all()
    return render_template('admin/namespaces.html', namespaces=namespaces, active_section='namespaces')


@web_bp.route('/admin/external-sync')
@web_bp.route('/admin/external-sync/<sync_view>')
@login_required
@admin_required
def admin_external_sync(sync_view='all'):
    """Admin panel - External sync controls and history."""
    requested_sync_view = (request.args.get('sync_view') or '').strip()
    if requested_sync_view:
        sync_view = requested_sync_view

    if sync_view not in EXTERNAL_SYNC_TYPE_LABELS:
        abort(404)

    # Ensure this request reads current values written by background workers.
    db.session.expire_all()

    configured_seasons = _load_config_seasons()
    selected_season = _resolve_external_sync_season(
        configured_seasons,
        request.args.get('year'),
        request.args.get('gender'),
    )

    history_query = ExternalSyncLog.query
    if selected_season is not None:
        history_query = history_query.filter_by(
            season_year=selected_season['year'],
            gender=selected_season['gender'],
        )
    if sync_view != 'all':
        history_query = history_query.filter_by(sync_type=sync_view)

    sync_history = history_query.order_by(ExternalSyncLog.timestamp.desc()).limit(200).all()
    has_running_sync = any(log.status in ('queued', 'running') for log in sync_history)

    selected_start_date = (request.args.get('start_date') or '').strip()
    selected_end_date = (request.args.get('end_date') or '').strip()

    response = make_response(render_template(
        'admin/external_sync.html',
        active_section='external_sync',
        active_sync_view=sync_view,
        sync_views=EXTERNAL_SYNC_TYPE_LABELS,
        run_sync_types=EXTERNAL_SYNC_RUN_TYPES,
        sync_history=sync_history,
        configured_seasons=configured_seasons,
        selected_season=selected_season,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
        sync_type_display=_external_sync_type_display,
        has_running_sync=has_running_sync,
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@web_bp.route('/admin/external-sync/run', methods=['POST'])
@login_required
@admin_required
def admin_external_sync_run():
    """Queue one external sync type job and write execution history."""
    sync_type = (request.form.get('sync_type') or '').strip()
    if sync_type not in EXTERNAL_SYNC_RUN_TYPES:
        flash('Invalid sync type selected.', 'error')
        return redirect(url_for('web.admin_external_sync', sync_view='all'))
    sync_log = None
    sync_log_id = None

    seasons = _load_config_seasons()
    if not seasons:
        _record_external_sync_log(
            provider='firstcycling',
            sync_type=sync_type,
            status='failed',
            executed_by_id=current_user.id,
            message='No valid seasons configured in SEASONS',
        )
        db.session.commit()
        flash('No valid SEASONS entries are configured; sync was not started.', 'error')
        return redirect(url_for('web.admin_external_sync', sync_view=sync_type))

    selected_season = _resolve_external_sync_season(
        seasons,
        request.form.get('year'),
        request.form.get('gender'),
    )
    if selected_season is None:
        flash('No season selected for sync execution.', 'error')
        return redirect(url_for('web.admin_external_sync', sync_view=sync_type))

    selected_year = selected_season['year']
    selected_gender = selected_season['gender']
    selected_start_date = (request.form.get('start_date') or '').strip()
    selected_end_date = (request.form.get('end_date') or '').strip()

    if sync_type == 'fc_race_results':
        if not selected_start_date or not selected_end_date:
            flash('Start date and end date are required for race results sync.', 'error')
            return redirect(
                url_for(
                    'web.admin_external_sync',
                    sync_view=sync_type,
                    year=selected_year,
                    gender=selected_gender,
                    start_date=selected_start_date or None,
                    end_date=selected_end_date or None,
                )
            )

        try:
            parsed_start_date = datetime.strptime(selected_start_date, '%Y-%m-%d').date()
            parsed_end_date = datetime.strptime(selected_end_date, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid date format. Use YYYY-MM-DD for start and end date.', 'error')
            return redirect(
                url_for(
                    'web.admin_external_sync',
                    sync_view=sync_type,
                    year=selected_year,
                    gender=selected_gender,
                    start_date=selected_start_date,
                    end_date=selected_end_date,
                )
            )

        if parsed_start_date > parsed_end_date:
            flash('Start date must be on or before end date.', 'error')
            return redirect(
                url_for(
                    'web.admin_external_sync',
                    sync_view=sync_type,
                    year=selected_year,
                    gender=selected_gender,
                    start_date=selected_start_date,
                    end_date=selected_end_date,
                )
            )

    api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
    if not api_path:
        _record_external_sync_log(
            provider='firstcycling',
            sync_type=sync_type,
            status='failed',
            executed_by_id=current_user.id,
            message='FIRST_CYCLING_API_PATH is not configured',
        )
        db.session.commit()
        flash('FIRST_CYCLING_API_PATH is not configured.', 'error')
        return redirect(url_for('web.admin_external_sync', sync_view=sync_type))

    sync_log = _record_external_sync_log(
        provider='firstcycling',
        sync_type=sync_type,
        status='queued',
        executed_by_id=current_user.id,
        season_year=selected_year,
        gender=selected_gender,
        message=(
            f'Queued {_external_sync_type_display(sync_type)} for {selected_year} [{selected_gender}]'
            + (
                f' ({selected_start_date} to {selected_end_date})'
                if sync_type == 'fc_race_results'
                else ''
            )
        ),
    )
    db.session.commit()
    sync_log_id = sync_log.id

    try:
        run_external_sync_task.delay(
            log_id=sync_log_id,
            sync_type=sync_type,
            season_year=selected_year,
            gender=selected_gender,
            api_path=api_path,
            start_date=selected_start_date or None,
            end_date=selected_end_date or None,
        )
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            'External sync failed for type=%s season=%s gender=%s',
            sync_type,
            selected_year,
            selected_gender,
        )

        if sync_log_id is not None:
            failed_log = ExternalSyncLog.query.get(sync_log_id)
            if failed_log is not None:
                failed_log.status = 'failed'
                failed_log.message = str(exc)
                failed_log.failed_count = 1
        else:
            _record_external_sync_log(
                provider='firstcycling',
                sync_type=sync_type,
                status='failed',
                executed_by_id=current_user.id,
                season_year=selected_year,
                gender=selected_gender,
                message=str(exc),
            )
        db.session.commit()
        flash(
            f"{_external_sync_type_display(sync_type)} failed for {selected_year} [{selected_gender}].",
            'error',
        )
        return redirect(
            url_for(
                'web.admin_external_sync',
                sync_view=sync_type,
                year=selected_year,
                gender=selected_gender,
                start_date=selected_start_date or None,
                end_date=selected_end_date or None,
            )
        )

    db.session.commit()

    flash(
        f"{_external_sync_type_display(sync_type)} queued for {selected_year} [{selected_gender}]. "
        'Progress will appear in sync history.',
        'info',
    )
    return redirect(
        url_for(
            'web.admin_external_sync',
            sync_view=sync_type,
            year=selected_year,
            gender=selected_gender,
            start_date=selected_start_date or None,
            end_date=selected_end_date or None,
        )
    )


@web_bp.route('/admin/namespaces/<int:namespace_id>')
@login_required
@admin_required
def admin_namespace_detail(namespace_id):
    """Admin panel - View and manage namespace details"""
    namespace = Namespace.query.get_or_404(namespace_id)
    
    # Get all users who have access to this namespace
    users_with_access = namespace.authorized_users.all() if hasattr(namespace, 'authorized_users') else []
    
    return render_template('admin/namespace_detail.html',
                         namespace=namespace,
                         users_with_access=users_with_access,
                         active_section='namespaces')


@web_bp.route('/admin/namespaces/create', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_namespace_create():
    """Admin panel - Create new namespace"""
    form = NamespaceForm()
    
    # Populate PCM version choices from config
    form.pcm_version.choices = [('', '-- Select Version --')] + current_app.config['PCM_VERSIONS']
    
    if form.validate_on_submit():
        normalized_slug = _slugify_namespace_part(form.slug.data)
        form.slug.data = normalized_slug

        if not normalized_slug:
            flash('Slug cannot be empty.', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces')

        # Check if namespace with same name or slug exists
        existing = Namespace.query.filter(
            (Namespace.name == form.name.data) | (Namespace.slug == normalized_slug)
        ).first()
        
        if existing:
            flash('A namespace with this name or slug already exists', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces')
        
        # Use slug directly as namespace database name
        database_name = normalized_slug
        
        # Handle CDB file upload
        sqlite_file_path = None
        if form.cdb_file.data:
            from werkzeug.utils import secure_filename
            from webapp.config import ensure_namespace_directories, get_cdb_import_path
            import os
            
            # Create namespace directories
            ensure_namespace_directories(normalized_slug)
            
            # Get the upload file
            file = form.cdb_file.data
            filename = secure_filename(file.filename)
            
            # Generate a unique filename with timestamp if needed
            base_name, ext = os.path.splitext(filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{base_name}_{timestamp}{ext}"
            
            # Save to namespace's import directory
            import_path = get_cdb_import_path(normalized_slug)
            file_path = os.path.join(import_path, unique_filename)
            
            file.save(file_path)
            
            # Store relative path from namespace root
            sqlite_file_path = f"cdb/import/{unique_filename}"
            
            flash(f'CDB database file "{filename}" uploaded successfully', 'success')
        
        # Create namespace
        namespace = Namespace(
            name=form.name.data,
            slug=normalized_slug,
            description=form.description.data,
            database_name=database_name,
            sqlite_file_path=sqlite_file_path,  # Store path to uploaded CDB file
            pcm_version=form.pcm_version.data,
            database_year=form.database_year.data,
            gender=form.gender.data,
            status=form.status.data,
            created_by_id=current_user.id
        )
        
        db.session.add(namespace)
        db.session.commit()
        
        logger.info(f"Namespace created: '{namespace.name}' (slug: {namespace.slug}) by user {current_user.username}")
        
        # Log namespace creation
        AuditLog.log_action(
            action=AuditAction.NAMESPACE_CREATED,
            user=current_user,
            resource_type='namespace',
            resource_id=namespace.id,
            resource_name=namespace.name,
            details={
                'slug': namespace.slug,
                'pcm_version': namespace.pcm_version,
                'database_year': namespace.database_year,
                'gender': namespace.gender,
                'has_cdb_file': bool(sqlite_file_path)
            },
            ip_address=request.remote_addr
        )
        db.session.commit()
        
        flash(f'Namespace "{namespace.name}" created successfully', 'success')
        
        # Automatically provision the namespace database
        from webapp.namespace.setup import NamespaceProvisioner, CDBImporter
        
        logger.info(f"Starting automatic provisioning for namespace: {namespace.slug}")
        
        try:
            # Provision database
            flash('Provisioning namespace database...', 'info')
            logger.info(f"Provisioning database for namespace: {namespace.slug}")
            
            provisioner = NamespaceProvisioner()
            result = provisioner.provision_namespace(namespace.database_name)
            
            # Log namespace provisioning
            AuditLog.log_action(
                action=AuditAction.NAMESPACE_PROVISIONED,
                user=current_user,
                resource_type='namespace',
                resource_id=namespace.id,
                resource_name=namespace.name,
                details={'database_name': result['database_name']},
                ip_address=request.remote_addr
            )
            
            db.session.commit()
            
            logger.info(f"Database provisioned successfully: {result['database_name']}")
            flash(f'✓ Database provisioned: {result["database_name"]}', 'success')
            
            # Import CDB data if file was uploaded
            if sqlite_file_path:
                logger.info(f"Starting CDB import for namespace: {namespace.slug}")
                flash('Importing CDB data...', 'info')
                
                # Build full path to CDB file
                cdb_filename = os.path.basename(sqlite_file_path)
                cdb_path = os.path.join(get_cdb_import_path(namespace.slug), cdb_filename)
                
                if os.path.exists(cdb_path):
                    logger.info(f"CDB file found at: {cdb_path}")
                    
                    with CDBImporter(cdb_path, namespace.build_connection_string(), namespace.pcm_version) as importer:
                        stats = importer.import_all_tables()
                    
                    # Show import statistics
                    for table, count in stats.items():
                        logger.info(f"Imported {count} records to {table}")
                        flash(f'✓ Imported {count} {table}', 'success')
                    
                    # Log CDB import
                    AuditLog.log_action(
                        action=AuditAction.NAMESPACE_CDB_IMPORTED,
                        user=current_user,
                        resource_type='namespace',
                        resource_id=namespace.id,
                        resource_name=namespace.name,
                        details={'import_stats': stats, 'cdb_file': cdb_filename},
                        ip_address=request.remote_addr
                    )
                    db.session.commit()
                    
                    logger.info(f"CDB import completed successfully for namespace: {namespace.slug}")
                    flash('CDB import completed successfully!', 'success')
                else:
                    error_msg = f'CDB file not found at {cdb_path}'
                    logger.error(error_msg)
                    flash(f'Warning: {error_msg}', 'warning')
            
        except Exception as e:
            error_msg = f'Error during provisioning/import: {str(e)}'
            logger.error(f"Namespace provisioning/import failed for {namespace.slug}: {error_msg}", exc_info=True)
            flash(error_msg, 'error')
            db.session.rollback()
        
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))
    
    return render_template('admin/namespace_form.html', form=form, active_section='namespaces')


@web_bp.route('/admin/namespaces/<int:namespace_id>/status', methods=['POST'])
@login_required
@admin_required
def admin_namespace_update_status(namespace_id):
    """Admin panel - Update namespace status"""
    namespace = Namespace.query.get_or_404(namespace_id)
    new_status = request.form.get('status')
    
    if new_status not in ['active', 'draft', 'archived']:
        return {'error': 'Invalid status'}, 400
    
    namespace.status = new_status
    db.session.commit()
    
    flash(f'Namespace "{namespace.name}" status updated to {new_status}', 'success')
    
    # Redirect back to detail page if coming from there, otherwise to list
    return redirect(request.referrer or url_for('web.admin_namespaces'))


# ==================== User Management Routes ====================

@web_bp.route('/admin/users/create', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_user_create():
    """Admin panel - Create new user"""
    form = UserCreateForm()
    
    if form.validate_on_submit():
        # Check if username already exists
        existing = User.query.filter_by(username=form.username.data).first()
        if existing:
            flash('A user with this username already exists', 'error')
            return render_template('admin/user_form.html', form=form, active_section='users')
        
        # Create new user with temporary password
        user = User(
            username=form.username.data,
            is_active=False,  # Inactive by default
            is_temporary_password=True  # Flag for password change requirement
        )
        user.set_password(form.temporary_password.data)
        
        db.session.add(user)
        db.session.commit()
        
        flash(f'User "{user.username}" created successfully with temporary password. User must activate account and change password on first login.', 'success')
        return redirect(url_for('web.admin_user_detail', user_id=user.id))
    
    return render_template('admin/user_form.html', form=form, active_section='users')


@web_bp.route('/admin/user/<int:user_id>')
@login_required
@admin_required
def admin_user_detail(user_id):
    """Admin panel - View and manage user details"""
    user = User.query.get_or_404(user_id)
    
    # Get all roles for assignment
    all_roles = Role.query.order_by(Role.display_name).all()
    
    # Get all namespaces for assignment
    all_namespaces = Namespace.query.order_by(Namespace.name).all()
    
    # Get user's current roles and namespaces
    user_roles = user.roles.all() if hasattr(user, 'roles') else []
    user_namespaces = user.accessible_namespaces.all() if hasattr(user, 'accessible_namespaces') else []
    
    return render_template('admin/user_detail.html', 
                         user=user, 
                         all_roles=all_roles,
                         all_namespaces=all_namespaces,
                         user_roles=user_roles,
                         user_namespaces=user_namespaces,
                         active_section='users')


@web_bp.route('/admin/user/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@admin_required
def admin_user_toggle_status(user_id):
    """Admin panel - Toggle user active status"""
    user = User.query.get_or_404(user_id)
    
    # Prevent deactivating yourself
    if user.id == current_user.id:
        flash('You cannot deactivate your own account', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    user.is_active = not user.is_active
    db.session.commit()
    
    status = 'activated' if user.is_active else 'deactivated'
    flash(f'User "{user.username}" has been {status}', 'success')
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/reset-password', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_user_reset_password(user_id):
    """Admin panel - Reset user password"""
    user = User.query.get_or_404(user_id)
    form = PasswordResetForm()
    
    if form.validate_on_submit():
        user.set_password(form.new_password.data)
        user.is_temporary_password = True  # Mark as temporary        
        # Log password reset
        AuditLog.log_action(
            action=AuditAction.USER_PASSWORD_RESET,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'reset_by_admin': True},
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        db.session.commit()
        
        flash(f'Password reset for "{user.username}". User must change password on next login.', 'success')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    return render_template('admin/user_reset_password.html', form=form, user=user, active_section='users')


@web_bp.route('/admin/user/<int:user_id>/add-role', methods=['POST'])
@login_required
@admin_required
def admin_user_add_role(user_id):
    """Admin panel - Add role to user"""
    user = User.query.get_or_404(user_id)
    role_id = request.form.get('role_id')
    
    if not role_id:
        flash('No role selected', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    role = Role.query.get_or_404(role_id)
    
    # Check if user already has this role
    if user.roles.filter_by(id=role.id).first():
        flash(f'User already has the "{role.display_name}" role', 'warning')
    else:
        user.roles.append(role)
        
        # Log role added
        AuditLog.log_action(
            action=AuditAction.USER_ROLE_ADDED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'role': role.display_name, 'role_id': role.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Role "{role.display_name}" added to user "{user.username}"', 'success')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/remove-role/<int:role_id>', methods=['POST'])
@login_required
@admin_required
def admin_user_remove_role(user_id, role_id):
    """Admin panel - Remove role from user"""
    user = User.query.get_or_404(user_id)
    role = Role.query.get_or_404(role_id)
    
    if user.roles.filter_by(id=role.id).first():
        user.roles.remove(role)
        
        # Log role removed
        AuditLog.log_action(
            action=AuditAction.USER_ROLE_REMOVED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'role': role.display_name, 'role_id': role.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Role "{role.display_name}" removed from user "{user.username}"', 'success')
    else:
        flash(f'User does not have the "{role.display_name}" role', 'warning')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/add-namespace', methods=['POST'])
@login_required
@admin_required
def admin_user_add_namespace(user_id):
    """Admin panel - Grant namespace access to user"""
    user = User.query.get_or_404(user_id)
    namespace_id = request.form.get('namespace_id')
    
    if not namespace_id:
        flash('No namespace selected', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    namespace = Namespace.query.get_or_404(namespace_id)
    
    # Check if user already has access
    if user.accessible_namespaces.filter_by(id=namespace.id).first():
        flash(f'User already has access to "{namespace.name}"', 'warning')
    else:
        user.accessible_namespaces.append(namespace)
        
        # Log namespace access granted
        AuditLog.log_action(
            action=AuditAction.USER_NAMESPACE_ACCESS_GRANTED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'namespace': namespace.name, 'namespace_id': namespace.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Access to namespace "{namespace.name}" granted to user "{user.username}"', 'success')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/remove-namespace/<int:namespace_id>', methods=['POST'])
@login_required
@admin_required
def admin_user_remove_namespace(user_id, namespace_id):
    """Admin panel - Revoke namespace access from user"""
    user = User.query.get_or_404(user_id)
    namespace = Namespace.query.get_or_404(namespace_id)
    
    if user.accessible_namespaces.filter_by(id=namespace.id).first():
        user.accessible_namespaces.remove(namespace)
        
        # Log namespace access revoked
        AuditLog.log_action(
            action=AuditAction.USER_NAMESPACE_ACCESS_REVOKED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'namespace': namespace.name, 'namespace_id': namespace.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Access to namespace "{namespace.name}" revoked from user "{user.username}"', 'success')
    else:
        flash(f'User does not have access to "{namespace.name}"', 'warning')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


# ==================== Password Change Route ====================

@web_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """User password change page (for temporary passwords)"""
    form = PasswordChangeForm()
    
    if form.validate_on_submit():
        # Verify current password
        if not current_user.check_password(form.current_password.data):
            flash('Current password is incorrect', 'error')
            return render_template('change_password.html', form=form)
        
        # Set new password
        current_user.set_password(form.new_password.data)
        current_user.is_temporary_password = False  # Remove temporary flag
        
        # Log password change
        AuditLog.log_action(
            action=AuditAction.USER_PASSWORD_CHANGED,
            user=current_user,
            resource_type='user',
            resource_id=current_user.id,
            resource_name=current_user.username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        
        db.session.commit()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('web.dashboard'))
    
    return render_template('change_password.html', form=form)
