"""Web routes for UI"""
import hashlib
import html
import logging
import json
import os
import re
import shutil
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import render_template, redirect, url_for, flash, request, abort, current_app, session, make_response, send_file, g
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine, text, Integer, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from webapp.web import web_bp
from webapp.web.forms import (
    AdminPasswordResetForm,
    LoginForm,
    NamespaceForm,
    PasswordChangeForm,
    PasswordSetupForm,
    UserCreateForm,
)
from webapp.models import User, UserSecurityToken, Resource, Namespace, Role, DiscordSettings, DISCORD_WORKFLOW_CHOICES, AuditLog, AuditAction, FirstCyclingRace, ExternalSyncLog, RaceResultMap
from webapp.integrations.cdb import apply_updates_to_sqlite as _apply_release_updates_to_sqlite
from webapp.integrations.discord import (
    load_namespace_stat_list_name,
    notify_discord_race_stat_approved,
    notify_discord_race_stat_saved,
    notify_discord_release_review_requested,
    notify_discord_stat_list_approved,
    notify_discord_stat_list_saved,
    test_discord_channel_permissions,
)
from webapp.integrations.firstcycling.api import FirstCyclingClient, FirstCyclingAPIError
from webapp.models.main.firstcycling import FirstCyclingRider, FirstCyclingTeam
from webapp.models.main.firstcycling import FirstCyclingRaceStage
from webapp.models.ns.schema import get_field_mapping
from webapp.extensions import db
from webapp.models.main.user import TOKEN_INVITE, TOKEN_PASSWORD_RESET
from webapp.security import validate_password_policy
from webapp.worker import run_external_sync_task
from webapp.web.wiki_content import find_wiki_page, find_wiki_section, load_wiki_page_content, load_wiki_sections
from functools import wraps

# Get logger for this module
logger = logging.getLogger(__name__)

STAT_DELTA_FIELDS = (
    'fla',
    'mo',
    'mm',
    'hil',
    'dh',
    'cob',
    'tt',
    'prl',
    'spr',
    'acc',
    'end',
    'res',
    'rec',
    'att',
)

STAT_MIN_VALUE = 50
STAT_MAX_VALUE = 85
STAT_COLUMN_MAP = {stat_name: f'stat_{stat_name}' for stat_name in STAT_DELTA_FIELDS}

EXTERNAL_SYNC_TYPE_LABELS = {
    'all': 'All Syncs',
    'fc_race_calendar': 'Race Calendar',
    'fc_teams': 'Teams',
    'fc_riders': 'Riders',
    'fc_race_results': 'Race Results',
}

EXTERNAL_SYNC_RUN_TYPES = ('fc_race_calendar', 'fc_teams', 'fc_riders', 'fc_race_results')
INTEGRATION_PROVIDER_LABELS = {
    'firstcycling': 'First Cycling',
    'discord': 'Discord',
}


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


APP_PANELS = {
    'stat_editor': {
        'display_name': 'Stats Editor',
        'description': 'Maintain rider stats from race results',
        'endpoint': 'web.stats_editor',
        'resources': ['stats'],
        'required_permissions': [('stats', 'read')],
    },
    'team_manager': {
        'display_name': 'Teams',
        'description': 'Manage teams and rosters',
        'endpoint': 'web.teams',
        'resources': ['teams'],
        'required_permissions': [('teams', 'read')],
    },
    'release_manager': {
        'display_name': 'Releases',
        'description': 'Manage database releases',
        'endpoint': 'web.releases',
        'resources': ['releases'],
        'required_permissions': [('releases', 'read')],
    },
}


def _has_any_permission(permission_pairs):
    if current_user.is_superadmin:
        return True

    return any(
        current_user.has_permission(resource_name, action_name)
        for resource_name, action_name in permission_pairs
    )


def _can_access_panel(panel_name):
    panel = APP_PANELS.get(panel_name)
    return bool(panel and _has_any_permission(panel['required_permissions']))


def _can_approve_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'approve')


def _can_create_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'create')


def _can_update_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'update')


def _can_download_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'download')


def _can_approve_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'approve')


def _can_publish_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'publish')


def _can_update_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'update')


def _can_access_firstcycling_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-firstcycling', 'read')
    )


def _can_run_firstcycling_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-firstcycling', 'download')
    )


def _can_update_discord_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-discord', 'update')
    )


def _can_access_discord_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-discord', 'read')
    )


def _can_access_integrations_provider(provider):
    if provider == 'firstcycling':
        return _can_access_firstcycling_integrations()
    if provider == 'discord':
        return _can_access_discord_integrations()
    return False


def _integration_provider_access_map():
    return {
        'firstcycling': _can_access_firstcycling_integrations(),
        'discord': _can_access_discord_integrations(),
    }


def _can_create_users():
    return (
        current_user.is_superadmin
        or current_user.has_permission('users', 'create')
    )


def _can_update_users():
    return (
        current_user.is_superadmin
        or current_user.has_permission('users', 'update')
    )


def _can_access_user_management():
    return _can_create_users() or _can_update_users() or current_user.has_permission('users', 'read')


def _can_read_roles():
    return current_user.is_superadmin or current_user.has_permission('roles', 'read')


def _can_access_namespace_management():
    return (
        current_user.is_superadmin
        or current_user.has_permission('namespaces', 'read')
        or current_user.has_permission('namespaces', 'create')
        or current_user.has_permission('namespaces', 'update')
        or current_user.has_permission('namespaces', 'delete')
    )


def _can_create_namespaces():
    return current_user.is_superadmin or current_user.has_permission('namespaces', 'create')


def _can_update_namespaces():
    return current_user.is_superadmin or current_user.has_permission('namespaces', 'update')


def _get_panel_url(panel_name):
    panel = APP_PANELS.get(panel_name, {})
    endpoint = panel.get('endpoint')
    if not endpoint:
        return None
    return url_for(endpoint)


def _get_panel_display_name(panel_name):
    """Return user-facing panel name."""
    panel = APP_PANELS.get(panel_name)
    if panel:
        return panel['display_name']

    default_name = panel_name.replace('_', ' ').title()
    if default_name.endswith(' Manager'):
        default_name = default_name[:-8]

    if not default_name.endswith('s'):
        default_name = f'{default_name}s'

    return default_name


def _get_panel_menu_items():
    """Return all known app panels with access metadata for global navigation."""
    panel_items = []
    panel_order = {'stat_editor': 0, 'team_manager': 1, 'release_manager': 2}
    for panel_name in sorted(APP_PANELS, key=lambda name: (panel_order.get(name, 3), name)):
        panel_url = _get_panel_url(panel_name)
        can_access = _can_access_panel(panel_name)
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

    return (
        Namespace.query
        .filter_by(status='active', is_deleted=False, is_template=False)
        .order_by(Namespace.name.asc())
        .all()
    )


def _namespace_from_slug(available_namespaces, namespace_slug):
    requested_slug = (namespace_slug or '').strip().lower()
    if not requested_slug:
        return None

    for namespace in available_namespaces:
        if (namespace.slug or '').strip().lower() == requested_slug:
            return namespace
    return None


def _get_selected_namespace(available_namespaces=None):
    available_namespaces = available_namespaces if available_namespaces is not None else _get_accessible_namespaces()
    if not available_namespaces:
        session.pop('selected_namespace_id', None)
        if getattr(g, 'requested_namespace_slug', None):
            abort(404)
        return None

    requested_namespace = _namespace_from_slug(
        available_namespaces,
        getattr(g, 'requested_namespace_slug', None),
    )
    if requested_namespace is not None:
        session['selected_namespace_id'] = requested_namespace.id
        g.selected_namespace = requested_namespace
        return requested_namespace

    if getattr(g, 'requested_namespace_slug', None):
        abort(404)

    selected_namespace_id = session.get('selected_namespace_id')
    if selected_namespace_id is not None:
        for namespace in available_namespaces:
            if namespace.id == selected_namespace_id:
                g.selected_namespace = namespace
                return namespace

    selected_namespace = available_namespaces[0]
    session['selected_namespace_id'] = selected_namespace.id
    g.selected_namespace = selected_namespace
    return selected_namespace


@web_bp.url_value_preprocessor
def pull_namespace_slug(endpoint, values):
    if not values:
        return

    namespace_slug = values.pop('namespace_slug', None)
    if namespace_slug:
        g.requested_namespace_slug = namespace_slug


def _endpoint_accepts_namespace_slug(endpoint):
    if not endpoint:
        return False

    try:
        return any(
            rule.endpoint == endpoint and 'namespace_slug' in rule.arguments
            for rule in current_app.url_map.iter_rules(endpoint)
        )
    except LookupError:
        return False


def _selected_namespace_for_url_defaults():
    selected_namespace = getattr(g, 'selected_namespace', None)
    if selected_namespace is not None:
        return selected_namespace

    selected_namespace_id = session.get('selected_namespace_id')
    if selected_namespace_id is None:
        return None

    selected_namespace = Namespace.query.filter_by(
        id=selected_namespace_id,
        is_deleted=False,
    ).first()
    if selected_namespace is not None:
        g.selected_namespace = selected_namespace
    return selected_namespace


@web_bp.url_defaults
def add_namespace_slug(endpoint, values):
    if (
        not current_user.is_authenticated
        or 'namespace_slug' in values
        or not _endpoint_accepts_namespace_slug(endpoint)
    ):
        return

    selected_namespace = _selected_namespace_for_url_defaults()
    if selected_namespace is not None and selected_namespace.slug:
        values['namespace_slug'] = selected_namespace.slug


def _with_namespace_slug_in_url(target_url, namespace):
    if namespace is None or not namespace.slug:
        return target_url

    parsed = urllib.parse.urlsplit(target_url or '')
    if parsed.scheme or parsed.netloc:
        current_host = request.host
        if parsed.netloc and parsed.netloc != current_host:
            return url_for('web.dashboard')

    path = parsed.path or url_for('web.dashboard')
    query = parsed.query
    fragment = parsed.fragment

    if path == '/':
        path = url_for('web.dashboard')

    parts = path.strip('/').split('/') if path.strip('/') else []
    if len(parts) >= 2 and parts[0] == 'n':
        parts[1] = namespace.slug
        path = '/' + '/'.join(parts)
    else:
        path = f'/n/{namespace.slug}{path if path.startswith("/") else "/" + path}'

    return urllib.parse.urlunsplit(('', '', path, query, fragment))


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


def _snapshot_stat_select_list(table_alias=''):
    prefix = f'{table_alias}.' if table_alias else ''
    return ', '.join(
        f'COALESCE({prefix}{column_name}, 0) AS {column_name}'
        for column_name in STAT_COLUMN_MAP.values()
    )


def _snapshot_stats_from_row(row):
    return {
        stat_name: row.get(column_name)
        for stat_name, column_name in STAT_COLUMN_MAP.items()
    }


def _normalize_external_id(value):
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _get_or_create_race_result_map(provider, external_race_id, external_stage_id=None):
    external_race_id = _normalize_external_id(external_race_id)
    external_stage_id = _normalize_external_id(external_stage_id)
    if not provider or external_race_id is None:
        return None

    race_result_map = RaceResultMap.query.filter_by(
        provider=provider,
        external_race_id=external_race_id,
        external_stage_id=external_stage_id,
    ).first()
    if race_result_map is None:
        race_result_map = RaceResultMap(
            provider=provider,
            external_race_id=external_race_id,
            external_stage_id=external_stage_id,
        )
        db.session.add(race_result_map)
        db.session.flush()
    return race_result_map


def _get_firstcycling_race_result_map(race_id, stage_id=None):
    return _get_or_create_race_result_map('firstcycling', race_id, stage_id)


def _load_firstcycling_race_result_map_ids(first_cycling_race_id=None, first_cycling_stage_id=None, include_all_race_stages=False):
    race_id = _normalize_external_id(first_cycling_race_id)
    if race_id is None:
        return []

    query = RaceResultMap.query.filter_by(provider='firstcycling', external_race_id=race_id)
    if not include_all_race_stages:
        stage_id = _normalize_external_id(first_cycling_stage_id)
        if stage_id is None:
            query = query.filter(RaceResultMap.external_stage_id.is_(None))
        else:
            query = query.filter(RaceResultMap.external_stage_id == stage_id)

    return [row.race_result_id for row in query.all()]


def _load_namespace_scope_snapshots(namespace, race_result_ids=None, stat_list_id=None, cdb_cyclist_ids=None):
    """Load rider stat snapshots keyed by CDB cyclist id for one approval scope."""
    if namespace is None or not cdb_cyclist_ids:
        return {}

    normalized_race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    normalized_cdb_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in cdb_cyclist_ids}
        if cdb_cyclist_id is not None
    ]
    if not normalized_cdb_ids:
        return {}

    if stat_list_id is None and not normalized_race_result_ids:
        return {}

    race_filter = (
        "race_result_id = ANY(:race_result_ids)"
        if normalized_race_result_ids
        else "race_result_id IS NULL"
    )
    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        cdb_cyclist_id,
                        {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE {race_filter}
                      AND stat_list_id IS NOT DISTINCT FROM :list_id
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {
                    'race_result_ids': normalized_race_result_ids,
                    'list_id': stat_list_id,
                    'cdb_cyclist_ids': normalized_cdb_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat snapshots from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    return {
        row['cdb_cyclist_id']: _snapshot_stats_from_row(row)
        for row in rows
        if row.get('cdb_cyclist_id') is not None
    }


def _ensure_scope_stat_snapshots(conn, race_result_id=None, stat_list_id=None, cdb_cyclist_ids=None):
    """Capture current CDB stats for riders in a scope that has no snapshot yet."""
    snapshot_entire_roster = cdb_cyclist_ids is None
    normalized_cdb_ids = []
    if not snapshot_entire_roster:
        normalized_cdb_ids = [
            cdb_cyclist_id
            for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
            if cdb_cyclist_id is not None
        ]

    if not snapshot_entire_roster and not normalized_cdb_ids:
        return 0

    result = conn.execute(
        text(
            f"""
            INSERT INTO stat_change_snapshot (
                race_result_id,
                stat_list_id,
                cdb_cyclist_id,
                {', '.join(STAT_COLUMN_MAP.values())},
                created_at
            )
            SELECT
                :race_result_id,
                :list_id,
                c.id,
                {_snapshot_stat_select_list('c')},
                NOW()
            FROM cdb_cyclist c
            WHERE (:snapshot_entire_roster OR c.id = ANY(:cdb_cyclist_ids))
              AND NOT EXISTS (
                  SELECT 1
                  FROM stat_change_snapshot s
                  WHERE s.race_result_id IS NOT DISTINCT FROM :race_result_id
                    AND s.stat_list_id IS NOT DISTINCT FROM :list_id
                    AND s.cdb_cyclist_id = c.id
              )
            """
        ),
        {
            'race_result_id': race_result_id,
            'list_id': stat_list_id,
            'snapshot_entire_roster': snapshot_entire_roster,
            'cdb_cyclist_ids': normalized_cdb_ids,
        },
    )
    return result.rowcount or 0


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


def _load_namespace_cyclist_link_map(namespace, first_cycling_rider_ids):
    if namespace is None or not first_cycling_rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        c.first_cycling_id,
                        c.id AS cyclist_id,
                        c.team_id,
                        COALESCE(t.team_name, t.short_name) AS team_name
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    WHERE c.first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(first_cycling_rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist link map from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    return {
        row['first_cycling_id']: {
            'cyclist_id': row.get('cyclist_id'),
            'team_id': row.get('team_id'),
            'team_name': row.get('team_name'),
        }
        for row in rows
        if row.get('first_cycling_id') is not None
    }


def _cdb_rider_display_name(row):
    explicit_name = (row.get('first_last_name') or '').strip()
    if explicit_name:
        return explicit_name
    return ' '.join(
        part
        for part in (
            (row.get('first_name') or '').strip(),
            (row.get('last_name') or '').strip(),
        )
        if part
    )


def _namespace_roster_team_from_row(row):
    return SimpleNamespace(
        team_id=row.get('team_id'),
        team_name=row.get('team_name'),
        short_name=row.get('short_name'),
        abbreviation=row.get('abbreviation') or row.get('uci_code'),
        country_id=row.get('country_id'),
        division_id=row.get('division_id'),
        nation_name=row.get('nation_name'),
        division=row.get('division'),
        uci_code=row.get('uci_code'),
        ranking_rank=row.get('ranking_rank'),
    )


def _namespace_roster_rider_from_row(row):
    rider_id = row.get('cyclist_id')
    birth_date = row.get('birth_date')

    return SimpleNamespace(
        cyclist_id=row.get('cyclist_id'),
        rider_id=rider_id,
        first_cycling_id=row.get('first_cycling_id'),
        name=_cdb_rider_display_name(row),
        first_name=row.get('first_name'),
        last_name=row.get('last_name'),
        birth_date=birth_date,
        birth_date_display=_format_date_for_display(birth_date),
        region_id=row.get('region_id'),
        nation_name=row.get('nation_name'),
        team_id=row.get('team_id'),
        team_name=row.get('team_name'),
        size_cm=row.get('size_cm'),
        weight_kg=row.get('weight_kg'),
        stat_mo=row.get('stat_mo'),
        stat_mm=row.get('stat_mm'),
        stat_dh=row.get('stat_dh'),
        stat_hil=row.get('stat_hil'),
        stat_tt=row.get('stat_tt'),
        stat_prl=row.get('stat_prl'),
        stat_fla=row.get('stat_fla'),
        stat_spr=row.get('stat_spr'),
        stat_acc=row.get('stat_acc'),
        stat_end=row.get('stat_end'),
        stat_res=row.get('stat_res'),
        stat_rec=row.get('stat_rec'),
        stat_cob=row.get('stat_cob'),
        stat_att=row.get('stat_att'),
    )


def _namespace_roster_stats_from_rider(rider):
    return {
        stat_name: getattr(rider, column_name, None)
        for stat_name, column_name in STAT_COLUMN_MAP.items()
    }


def _format_date_for_display(value):
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    if value:
        raw_value = str(value).strip()
        if re.fullmatch(r'\d{8}', raw_value):
            try:
                return datetime.strptime(raw_value, '%Y%m%d').strftime('%Y-%m-%d')
            except ValueError:
                pass
        return raw_value
    return None


def _enrich_namespace_roster_rider_with_firstcycling(rider):
    """Fill display-only rider attributes from FirstCycling when CDB lacks them."""
    first_cycling_id = _safe_int(getattr(rider, 'first_cycling_id', None))
    if first_cycling_id is None:
        return rider

    firstcycling_rider = FirstCyclingRider.query.filter_by(rider_id=first_cycling_id).first()
    if firstcycling_rider is None:
        return rider

    rider.firstcycling_rider = firstcycling_rider
    rider.name = rider.name or firstcycling_rider.name
    rider.first_name = rider.first_name or firstcycling_rider.first_name
    rider.last_name = rider.last_name or firstcycling_rider.last_name
    rider.nation_name = rider.nation_name or firstcycling_rider.nation_name
    rider.birth_date_display = rider.birth_date_display or _format_date_for_display(firstcycling_rider.birth_date)
    if rider.size_cm is None:
        rider.size_cm = firstcycling_rider.height
    return rider


def _load_namespace_roster_teams(namespace, selected_division='', selected_country='', team_query=''):
    if namespace is None:
        return [], [], [], {}

    params = {}
    where_clauses = []
    if selected_division:
        where_clauses.append('t.division_id = :division_id')
        params['division_id'] = _safe_int(selected_division)
    if selected_country:
        where_clauses.append('t.country_id = :country_id')
        params['country_id'] = _safe_int(selected_country)
    if team_query:
        where_clauses.append(
            """
            (
                t.team_name ILIKE :team_query
                OR t.short_name ILIKE :team_query
                OR t.abbreviation ILIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{team_query}%'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            division_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.division_id::text AS value,
                        COALESCE(division.constant, t.division_id::text) AS label
                    FROM cdb_team t
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    WHERE t.division_id IS NOT NULL
                    ORDER BY COALESCE(division.constant, t.division_id::text) ASC
                    """
                )
            ).mappings().all()
            division_options = [
                {
                    'value': row['value'],
                    'label': row['label'],
                }
                for row in division_rows
            ]
            country_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.country_id::text AS value,
                        COALESCE(country.constant, t.country_id::text) AS label
                    FROM cdb_team t
                    LEFT JOIN cdb_country country ON country.id = t.country_id
                    WHERE t.country_id IS NOT NULL
                    ORDER BY COALESCE(country.constant, t.country_id::text) ASC
                    """
                )
            ).mappings().all()
            country_options = [
                {
                    'value': row['value'],
                    'label': row['label'],
                }
                for row in country_rows
            ]

            team_rows = conn.execute(
                text(
                    f"""
                    SELECT
                        t.id AS team_id,
                        t.short_name,
                        COALESCE(t.team_name, t.short_name) AS team_name,
                        t.abbreviation,
                        t.country_id,
                        t.division_id,
                        COALESCE(country.constant, t.country_id::text) AS nation_name,
                        COALESCE(division.constant, t.division_id::text) AS division,
                        t.abbreviation AS uci_code,
                        NULL AS ranking_rank,
                        COUNT(c.id) AS rider_count
                    FROM cdb_team t
                    LEFT JOIN cdb_cyclist c ON c.team_id = t.id
                    LEFT JOIN cdb_country country ON country.id = t.country_id
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    {where_sql}
                    GROUP BY
                        t.id,
                        t.short_name,
                        t.team_name,
                        t.abbreviation,
                        t.country_id,
                        t.division_id,
                        country.constant,
                        division.constant
                    ORDER BY
                        t.team_name ASC NULLS LAST,
                        t.short_name ASC NULLS LAST,
                        t.id ASC
                    """
                ),
                params,
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB teams from namespace %s', namespace.slug)
        return [], [], [], {}
    finally:
        engine.dispose()

    teams = [_namespace_roster_team_from_row(row) for row in team_rows]
    rider_counts = {
        row['team_id']: int(row.get('rider_count') or 0)
        for row in team_rows
    }
    return teams, division_options, country_options, rider_counts


def _load_namespace_roster_riders(namespace, filters, page=1, per_page=100):
    if namespace is None:
        return [], 0, [], []

    page = max(page, 1)
    per_page = min(max(per_page, 25), 250)
    offset = (page - 1) * per_page

    params = {}
    where_clauses = []
    rider_query = (filters.get('q') or '').strip()
    team_query = (filters.get('team') or '').strip()
    selected_country = (filters.get('country') or '').strip()
    selected_division = (filters.get('division') or '').strip()

    if rider_query:
        where_clauses.append(
            """
            (
                c.first_last_name ILIKE :rider_query
                OR c.first_name ILIKE :rider_query
                OR c.last_name ILIKE :rider_query
                OR CONCAT_WS(' ', c.first_name, c.last_name) ILIKE :rider_query
                OR c.id::text = :rider_query_exact
                OR c.first_cycling_id::text = :rider_query_exact
            )
            """
        )
        params['rider_query'] = f'%{rider_query}%'
        params['rider_query_exact'] = rider_query
    if selected_country:
        where_clauses.append('region.country_id = :country_id')
        params['country_id'] = _safe_int(selected_country)
    if selected_division:
        where_clauses.append('t.division_id = :division_id')
        params['division_id'] = _safe_int(selected_division)
    if team_query:
        where_clauses.append(
            """
            (
                t.team_name ILIKE :team_query
                OR t.short_name ILIKE :team_query
                OR t.abbreviation ILIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{team_query}%'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            country_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        country.id::text AS value,
                        COALESCE(country.constant, country.id::text) AS label
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE country.id IS NOT NULL
                    ORDER BY COALESCE(country.constant, country.id::text) ASC
                    """
                )
            ).mappings().all()
            division_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.division_id::text AS value,
                        COALESCE(division.constant, t.division_id::text) AS label
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    WHERE t.division_id IS NOT NULL
                    ORDER BY COALESCE(division.constant, t.division_id::text) ASC
                    """
                )
            ).mappings().all()
            total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    {where_sql}
                    """
                ),
                params,
            ).scalar() or 0
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        COALESCE(t.team_name, t.short_name) AS team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    {where_sql}
                    ORDER BY
                        c.last_name ASC NULLS LAST,
                        c.first_name ASC NULLS LAST,
                        c.first_last_name ASC NULLS LAST,
                        c.first_cycling_id ASC NULLS LAST,
                        c.id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    **params,
                    'limit': per_page,
                    'offset': offset,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB riders from namespace %s', namespace.slug)
        return [], 0, [], []
    finally:
        engine.dispose()

    country_options = [{'value': row['value'], 'label': row['label']} for row in country_rows]
    division_options = [{'value': row['value'], 'label': row['label']} for row in division_rows]
    return [_namespace_roster_rider_from_row(row) for row in rows], int(total), country_options, division_options


def _load_namespace_roster_team(namespace, team_id):
    if namespace is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        team.id AS team_id,
                        team.short_name,
                        COALESCE(team.team_name, team.short_name) AS team_name,
                        team.abbreviation,
                        team.country_id,
                        team.division_id,
                        COALESCE(country.constant, team.country_id::text) AS nation_name,
                        COALESCE(division.constant, team.division_id::text) AS division,
                        team.abbreviation AS uci_code,
                        NULL AS ranking_rank
                    FROM cdb_team team
                    LEFT JOIN cdb_country country ON country.id = team.country_id
                    LEFT JOIN cdb_division division ON division.id = team.division_id
                    WHERE team.id = :team_id
                    """
                ),
                {'team_id': team_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load CDB team %s from namespace %s', team_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_roster_team_from_row(row) if row else None


def _load_namespace_roster_riders_for_team(namespace, team_id):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        t.team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE c.team_id = :team_id
                    ORDER BY
                        c.last_name ASC NULLS LAST,
                        c.first_name ASC NULLS LAST,
                        c.first_last_name ASC NULLS LAST,
                        c.first_cycling_id ASC NULLS LAST,
                        c.id ASC
                    """
                ),
                {'team_id': team_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB riders for team %s from namespace %s', team_id, namespace.slug)
        return []
    finally:
        engine.dispose()

    return [_namespace_roster_rider_from_row(row) for row in rows]


def _load_namespace_roster_rider(namespace, rider_id):
    if namespace is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        t.team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE c.id = :rider_id
                    """
                ),
                {'rider_id': rider_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load CDB rider %s from namespace %s', rider_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_roster_rider_from_row(row) if row else None


def _stat_change_status_label(row):
    return 'Approved' if row.get('is_approved') else 'Awaiting Approval'


def _stat_change_release_label(row):
    if not row.get('release_id'):
        return 'Not Released'
    return 'Released'


def _race_result_source_info(mapped_result, race_map, stage_map):
    if mapped_result is None:
        return None

    race_id = _safe_int(mapped_result.external_race_id)
    stage_id = _safe_int(mapped_result.external_stage_id)
    race = race_map.get(race_id)
    stage = stage_map.get(stage_id)
    race_name = race.name if race is not None and race.name else f'Race #{race_id or mapped_result.race_result_id}'
    if stage_id is not None:
        stage_label = stage.stage_name or f'Stage #{stage.stage_number or "?"}'
        label = f'{race_name} - {stage_label}'
        url = url_for('web.stats_editor_race_detail', race_id=race_id, stage_id=stage_id) if race_id is not None else None
    else:
        label = race_name
        url = url_for('web.stats_editor_race_detail', race_id=race_id) if race_id is not None else None

    return {
        'type': 'Race Result',
        'label': label,
        'url': url,
    }


def _load_namespace_rider_stat_edits(namespace, cdb_cyclist_id):
    if namespace is None or cdb_cyclist_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        sc.id,
                        sc.race_result_id,
                        sc.stat_list_id,
                        sc.release_id,
                        sc.stat_name,
                        sc.original_value,
                        sc.final_value,
                        sc.delta_value,
                        COALESCE(sc.is_approved, FALSE) AS is_approved,
                        sl.list_name,
                        sl.season_year AS list_season_year,
                        sl.gender AS list_gender,
                        rel.version AS release_version,
                        rel.release_name
                    FROM stat_changes sc
                    LEFT JOIN stat_lists sl ON sl.id = sc.stat_list_id
                    LEFT JOIN releases rel ON rel.id = sc.release_id
                    WHERE sc.cdb_cyclist_id = :cdb_cyclist_id
                    ORDER BY sc.id DESC
                    """
                ),
                {'cdb_cyclist_id': cdb_cyclist_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat edits for cyclist %s from namespace %s', cdb_cyclist_id, namespace.slug)
        return []
    finally:
        engine.dispose()

    race_result_ids = [
        row['race_result_id']
        for row in rows
        if row.get('race_result_id') is not None
    ]
    race_result_map = {}
    race_map = {}
    stage_map = {}
    if race_result_ids:
        race_result_map = {
            row.race_result_id: row
            for row in RaceResultMap.query.filter(RaceResultMap.race_result_id.in_(race_result_ids)).all()
        }
        race_ids = {
            _safe_int(row.external_race_id)
            for row in race_result_map.values()
            if row.provider == 'firstcycling' and _safe_int(row.external_race_id) is not None
        }
        stage_ids = {
            _safe_int(row.external_stage_id)
            for row in race_result_map.values()
            if row.provider == 'firstcycling' and _safe_int(row.external_stage_id) is not None
        }
        if race_ids:
            race_map = {
                int(race.race_id): race
                for race in FirstCyclingRace.query.filter(FirstCyclingRace.race_id.in_(race_ids)).all()
                if race.race_id is not None
            }
        if stage_ids:
            stage_map = {
                int(stage.id): stage
                for stage in FirstCyclingRaceStage.query.filter(FirstCyclingRaceStage.id.in_(stage_ids)).all()
                if stage.id is not None
            }

    edits = []
    for row in rows:
        if row.get('stat_list_id') is not None:
            source = {
                'type': 'Stat List',
                'label': row.get('list_name') or f'List #{row.get("stat_list_id")}',
                'url': url_for('web.stats_editor_list_detail', list_id=row.get('stat_list_id')),
            }
        else:
            source = _race_result_source_info(
                race_result_map.get(row.get('race_result_id')),
                race_map,
                stage_map,
            ) or {
                'type': 'Race Result',
                'label': f'Race Result #{row.get("race_result_id")}',
                'url': None,
            }

        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        edits.append({
            'id': row.get('id'),
            'source': source,
            'stat': stat_code.upper() if stat_code else '-',
            'original_value': row.get('original_value'),
            'final_value': row.get('final_value'),
            'delta_value': row.get('delta_value'),
            'approval_status': _stat_change_status_label(row),
            'release_status': _stat_change_release_label(row),
            'is_approved': bool(row.get('is_approved')),
            'is_released': bool(row.get('release_id')),
        })

    return edits


def _load_dashboard_namespace_stats(namespace):
    stats = {
        'team_count': 0,
        'rider_count': 0,
        'cdb_team_count': 0,
        'cdb_rider_count': 0,
        'total_stat_edits': 0,
        'published_releases': 0,
    }
    if namespace is None:
        return stats

    selected_year, selected_gender = _resolve_namespace_season(namespace)
    if selected_year is not None:
        main_teams = FirstCyclingTeam.query.filter_by(
            team_year=selected_year,
            gender=selected_gender,
        ).all()
        stats['team_count'] = len(main_teams)

        rider_query = FirstCyclingRider.query
        normalized_gender = _normalize_gender_token(selected_gender)
        if normalized_gender:
            gender_tokens = {normalized_gender, selected_gender}
            rider_query = rider_query.filter(or_(
                FirstCyclingRider.gender.is_(None),
                func.lower(FirstCyclingRider.gender).in_({
                    str(token).strip().lower()
                    for token in gender_tokens
                    if str(token).strip()
                }),
            ))
        stats['rider_count'] = rider_query.with_entities(
            func.count(func.distinct(FirstCyclingRider.rider_id))
        ).scalar() or 0

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS cdb_rider_count,
                        COUNT(DISTINCT team_id) FILTER (WHERE team_id IS NOT NULL) AS cdb_team_count,
                        (SELECT COUNT(*) FROM stat_changes) AS total_stat_edits,
                        (SELECT COUNT(*) FROM releases WHERE LOWER(COALESCE(status, '')) = 'published') AS published_releases
                    FROM cdb_cyclist
                    """
                )
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load dashboard namespace stats for namespace %s', namespace.slug)
        return stats
    finally:
        engine.dispose()

    if row:
        stats['cdb_team_count'] = int(row.get('cdb_team_count') or 0)
        stats['cdb_rider_count'] = int(row.get('cdb_rider_count') or 0)
        stats['total_stat_edits'] = int(row.get('total_stat_edits') or 0)
        stats['published_releases'] = int(row.get('published_releases') or 0)

    return stats


def _load_namespace_race_result_deltas(
    namespace,
    race_result_ids,
    cdb_cyclist_ids,
):
    """Load saved stat delta rows keyed by CDB cyclist and stat name for one race scope."""
    race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    cdb_cyclist_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
        if cdb_cyclist_id is not None
    ]
    if namespace is None or not race_result_ids or not cdb_cyclist_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    ORDER BY id ASC
                    """
                ),
                {
                    'race_result_ids': race_result_ids,
                    'cdb_cyclist_ids': cdb_cyclist_ids,
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
        rider_id = row['cdb_cyclist_id']
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

    race_ids = [
        _normalize_external_id(race_id)
        for race_id in first_cycling_race_ids
        if _normalize_external_id(race_id) is not None
    ]
    map_rows = RaceResultMap.query.filter(
        RaceResultMap.provider == 'firstcycling',
        RaceResultMap.external_race_id.in_(race_ids),
    ).all()
    if not map_rows:
        return {}

    race_result_to_race = {
        row.race_result_id: _safe_int(row.external_race_id)
        for row in map_rows
    }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            rows = conn.execute(
                text(
                    """
                    SELECT
                        race_result_id,
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                    GROUP BY race_result_id
                    """
                ),
                {'race_result_ids': list(race_result_to_race.keys())},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load race delta status from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    status_map = {}
    for row in rows:
        race_id = race_result_to_race.get(row['race_result_id'])
        if race_id is None:
            continue
        total_changes = row['total_changes'] or 0
        approved_changes = row['approved_changes'] or 0
        current = status_map.setdefault(race_id, {'total_changes': 0, 'approved_changes': 0})
        current['total_changes'] += int(total_changes)
        current['approved_changes'] += int(approved_changes)

    return status_map


def _load_namespace_stage_delta_status(namespace, first_cycling_race_ids):
    """Load stage-level delta/approval aggregates keyed by FirstCycling stage_id."""
    if namespace is None or not first_cycling_race_ids:
        return {}

    race_ids = [
        _normalize_external_id(race_id)
        for race_id in first_cycling_race_ids
        if _normalize_external_id(race_id) is not None
    ]
    map_rows = RaceResultMap.query.filter(
        RaceResultMap.provider == 'firstcycling',
        RaceResultMap.external_race_id.in_(race_ids),
        RaceResultMap.external_stage_id.isnot(None),
    ).all()
    if not map_rows:
        return {}

    race_result_to_stage = {
        row.race_result_id: _safe_int(row.external_stage_id)
        for row in map_rows
    }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        race_result_id,
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                    GROUP BY race_result_id
                    """
                ),
                {'race_result_ids': list(race_result_to_stage.keys())},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stage delta status from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    status_map = {}
    for row in rows:
        stage_id = race_result_to_stage.get(row['race_result_id'])
        if stage_id is None:
            continue

        total_changes = row['total_changes'] or 0
        approved_changes = row['approved_changes'] or 0
        status_map[int(stage_id)] = {
            'total_changes': int(total_changes),
            'approved_changes': int(approved_changes),
        }

    return status_map


def _load_namespace_scope_delta_status(
    namespace,
    first_cycling_race_id=None,
    first_cycling_stage_id=None,
    stat_list_id=None,
    include_all_race_stages=False,
):
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
            race_result_ids = _load_firstcycling_race_result_map_ids(
                first_cycling_race_id,
                first_cycling_stage_id,
                include_all_race_stages=include_all_race_stages,
            )
            if stat_list_id is None and first_cycling_race_id is not None and not race_result_ids:
                return {
                    'total_changes': 0,
                    'approved_changes': 0,
                    'unapproved_changes': 0,
                }
            race_filter = (
                "race_result_id = ANY(:race_result_ids)"
                if race_result_ids
                else "race_result_id IS NULL"
            )
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE {race_filter}
                      AND stat_list_id IS NOT DISTINCT FROM :list_id
                    """
                ),
                {
                    'race_result_ids': race_result_ids,
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


def _is_draft_release_status(status):
    return (status or 'draft').strip().lower() == 'draft'


def _load_namespace_approved_release_items(
    namespace,
    season_year=None,
    gender=None,
    selected_release_id=None,
    release_status=None,
):
    """Return approved inclusion candidates across race/stage/list scopes."""
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            release_scope_id = _safe_int(selected_release_id) or 0
            draft_release = _is_draft_release_status(release_status)
            release_filter = (
                'COALESCE(release_id, 0) = 0'
                if draft_release
                else 'release_id = :selected_release_id'
            )
            selected_rows_sql = (
                'COUNT(*)'
                if draft_release
                else 'SUM(CASE WHEN release_id = :selected_release_id THEN 1 ELSE 0 END)'
            )
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(race_result_id, 0) AS race_result_id,
                        COALESCE(stat_list_id, 0) AS stat_list_id,
                        {selected_rows_sql} AS selected_rows,
                        COUNT(*) AS total_edits,
                        COUNT(DISTINCT cdb_cyclist_id) AS riders_edited
                    FROM stat_changes
                    WHERE {release_filter}
                      AND (
                            (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                            OR (
                                COALESCE(stat_list_id, 0) > 0
                                AND COALESCE(race_result_id, 0) = 0
                            )
                      )
                    GROUP BY
                        COALESCE(race_result_id, 0),
                        COALESCE(stat_list_id, 0)
                    HAVING BOOL_AND(COALESCE(is_approved, FALSE))
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

    race_result_ids = {
        int(row['race_result_id'])
        for row in rows
        if (row.get('race_result_id') or 0) > 0
    }
    race_result_map = {}
    if race_result_ids:
        race_result_map = {
            row.race_result_id: row
            for row in RaceResultMap.query.filter(RaceResultMap.race_result_id.in_(race_result_ids)).all()
        }
    race_ids = {
        _safe_int(row.external_race_id)
        for row in race_result_map.values()
        if row.provider == 'firstcycling' and _safe_int(row.external_race_id) is not None
    }
    stage_ids = {
        _safe_int(row.external_stage_id)
        for row in race_result_map.values()
        if row.provider == 'firstcycling' and _safe_int(row.external_stage_id) is not None
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
        mapped_result = race_result_map.get(int(row.get('race_result_id') or 0))
        race_id = (_safe_int(mapped_result.external_race_id) if mapped_result is not None else 0) or 0
        stage_id = (_safe_int(mapped_result.external_stage_id) if mapped_result is not None else 0) or 0
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
            detail_url = url_for('web.stats_editor_list_detail', list_id=list_id)
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
            detail_url = url_for('web.stats_editor_race_detail', race_id=race_id, stage_id=stage_id)
        else:
            race_obj = race_map.get(race_id)
            if race_obj is None:
                continue
            scope_type = 'race'
            display_name = race_obj.name or f'Race #{race_id}'
            item_date = race_obj.start_date if race_obj is not None else '-'
            category = race_obj.category if race_obj is not None else '-'
            classification_label = race_obj.classification_label if race_obj is not None else '-'
            detail_url = url_for('web.stats_editor_race_detail', race_id=race_id)

        items.append({
            'item_key': _encode_release_item_key(scope_type, race_id, stage_id, list_id),
            'scope_type': scope_type,
            'type_label': 'List' if scope_type == 'list' else 'Race',
            'is_selected': (int(row.get('selected_rows') or 0) > 0) if selected_release_id is not None else False,
            'date': item_date,
            'name': display_name,
            'detail_url': detail_url,
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


def _load_namespace_stats_release_summary(namespace, release_id=None, release_status=None):
    if namespace is None:
        return {
            'race_results_included': 0,
            'stat_lists_included': 0,
            'riders_with_edits': 0,
            'total_edits': 0,
        }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            release_scope_id = _safe_int(release_id) or 0
            release_filter = (
                'COALESCE(release_id, 0) = 0'
                if _is_draft_release_status(release_status)
                else 'release_id = :release_id'
            )
            qualified_release_filter = (
                'COALESCE(sc.release_id, 0) = 0'
                if _is_draft_release_status(release_status)
                else 'sc.release_id = :release_id'
            )
            row = conn.execute(
                text(
                    f"""
                    WITH approved_scopes AS (
                        SELECT
                            COALESCE(race_result_id, 0) AS race_result_id,
                            COALESCE(stat_list_id, 0) AS stat_list_id
                        FROM stat_changes
                        WHERE {release_filter}
                          AND (
                                (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                                OR (
                                    COALESCE(stat_list_id, 0) > 0
                                    AND COALESCE(race_result_id, 0) = 0
                                )
                          )
                        GROUP BY
                            COALESCE(race_result_id, 0),
                            COALESCE(stat_list_id, 0)
                        HAVING BOOL_AND(COALESCE(is_approved, FALSE))
                    )
                    SELECT
                        COUNT(DISTINCT sc.race_result_id) FILTER (
                            WHERE sc.stat_list_id IS NULL
                              AND sc.race_result_id IS NOT NULL
                        ) AS race_results_included,
                        COUNT(DISTINCT sc.stat_list_id) FILTER (
                            WHERE sc.stat_list_id IS NOT NULL
                        ) AS stat_lists_included,
                        COUNT(DISTINCT sc.cdb_cyclist_id) AS riders_with_edits,
                        COUNT(*) AS total_edits
                    FROM stat_changes sc
                    JOIN approved_scopes approved
                      ON COALESCE(sc.race_result_id, 0) = approved.race_result_id
                     AND COALESCE(sc.stat_list_id, 0) = approved.stat_list_id
                    WHERE {qualified_release_filter}
                    """
                ),
                {'release_id': release_scope_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load stats release summary from namespace %s', namespace.slug)
        return {
            'race_results_included': 0,
            'stat_lists_included': 0,
            'riders_with_edits': 0,
            'total_edits': 0,
        }
    finally:
        engine.dispose()

    return {
        'race_results_included': int((row or {}).get('race_results_included') or 0),
        'stat_lists_included': int((row or {}).get('stat_lists_included') or 0),
        'riders_with_edits': int((row or {}).get('riders_with_edits') or 0),
        'total_edits': int((row or {}).get('total_edits') or 0),
    }


def _sync_release_stats_change_type(namespace, release_id, include_stats):
    if namespace is None or release_id is None:
        return 0

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            if include_stats:
                result = conn.execute(
                    text(
                        """
                        WITH approved_scopes AS (
                            SELECT
                                COALESCE(race_result_id, 0) AS race_result_id,
                                COALESCE(stat_list_id, 0) AS stat_list_id
                            FROM stat_changes
                            WHERE COALESCE(release_id, 0) = 0
                              AND (
                                    (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                                    OR (
                                        COALESCE(stat_list_id, 0) > 0
                                        AND COALESCE(race_result_id, 0) = 0
                                    )
                              )
                            GROUP BY
                                COALESCE(race_result_id, 0),
                                COALESCE(stat_list_id, 0)
                            HAVING BOOL_AND(COALESCE(is_approved, FALSE))
                        )
                        UPDATE stat_changes
                        SET release_id = :release_id
                        WHERE COALESCE(release_id, 0) = 0
                          AND EXISTS (
                                SELECT 1
                                FROM approved_scopes approved
                                WHERE COALESCE(stat_changes.race_result_id, 0) = approved.race_result_id
                                  AND COALESCE(stat_changes.stat_list_id, 0) = approved.stat_list_id
                          )
                        """
                    ),
                    {'release_id': release_id},
                )
            else:
                result = conn.execute(
                    text(
                        """
                        UPDATE stat_changes
                        SET release_id = NULL
                        WHERE release_id = :release_id
                        """
                    ),
                    {'release_id': release_id},
                )
        return int(result.rowcount or 0)
    finally:
        engine.dispose()


def _release_change_types_from_form():
    selected_types = set(request.form.getlist('included_change_types'))
    return {
        'stats_editor': 'stats_editor' in selected_types,
        'palmares': 'palmares' in selected_types,
        'photos': 'photos' in selected_types,
    }


def _release_type_from_change_types(change_types):
    labels = []
    if change_types.get('stats_editor'):
        labels.append('stats')
    if change_types.get('palmares'):
        labels.append('palmares')
    if change_types.get('photos'):
        labels.append('photos')
    return ','.join(labels)


def _namespace_release_from_row(row):
    if row is None:
        return None

    row_dict = dict(row)
    version_number = _release_version_number(row_dict.get('version'))
    if version_number is not None:
        row_dict['version'] = _format_release_version(version_number)
    row_dict['include_stats_editor_changes'] = bool(row_dict.get('include_stats_editor_changes'))
    row_dict['include_palmares_changes'] = bool(row_dict.get('include_palmares_changes'))
    row_dict['include_photo_changes'] = bool(row_dict.get('include_photo_changes'))
    return SimpleNamespace(**row_dict)


def _release_version_number(version):
    version_text = str(version or '').strip().lower()
    if version_text.startswith('v'):
        version_text = version_text[1:]
    return _safe_int(version_text)


def _format_release_version(version_number):
    return f'v{version_number}'


def _namespace_release_select_sql(columns, where_clause=None):
    def column_or_default(column_name, default_sql):
        if column_name in columns:
            return column_name
        return default_sql

    select_columns = [
        'id',
        'version',
        column_or_default('release_name', 'NULL AS release_name'),
        column_or_default('release_type', 'NULL AS release_type'),
        column_or_default('status', "'draft' AS status"),
        column_or_default('release_notes', 'NULL AS release_notes'),
        (
            'COALESCE(include_stats_editor_changes, FALSE) AS include_stats_editor_changes'
            if 'include_stats_editor_changes' in columns
            else 'FALSE AS include_stats_editor_changes'
        ),
        (
            'COALESCE(include_palmares_changes, FALSE) AS include_palmares_changes'
            if 'include_palmares_changes' in columns
            else 'FALSE AS include_palmares_changes'
        ),
        (
            'COALESCE(include_photo_changes, FALSE) AS include_photo_changes'
            if 'include_photo_changes' in columns
            else 'FALSE AS include_photo_changes'
        ),
        column_or_default('created_by_id', 'NULL AS created_by_id'),
        column_or_default('published_by_id', 'NULL AS published_by_id'),
        column_or_default('published_at', 'NULL AS published_at'),
        column_or_default('created_at', 'NULL AS created_at'),
        column_or_default('updated_at', 'NULL AS updated_at'),
    ]
    where_sql = f'\n                    {where_clause}' if where_clause else ''
    order_sql = 'created_at DESC NULLS LAST, id DESC' if 'created_at' in columns else 'id DESC'
    return f"""
                    SELECT
                        {', '.join(select_columns)}
                    FROM releases{where_sql}
                    ORDER BY {order_sql}
                    """


def _load_namespace_release_columns(conn):
    return {
        column_name
        for column_name in conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'releases'
                """
            )
        ).scalars()
    }


def _ensure_namespace_database_connection(conn, namespace):
    current_database = conn.execute(text('SELECT current_database()')).scalar()
    if current_database != namespace.database_name:
        raise RuntimeError(
            f'Connected to {current_database}, expected namespace database {namespace.database_name}'
        )


def _load_namespace_releases(namespace):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)
            if not {'id', 'version'}.issubset(columns):
                logger.warning('Namespace %s releases table is missing id/version columns', namespace.slug)
                return []

            rows = conn.execute(
                text(_namespace_release_select_sql(columns))
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load releases from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()

    return [_namespace_release_from_row(row) for row in rows]


def _load_namespace_release(namespace, release_id):
    if namespace is None or release_id is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)
            if not {'id', 'version'}.issubset(columns):
                logger.warning('Namespace %s releases table is missing id/version columns', namespace.slug)
                return None

            row = conn.execute(
                text(_namespace_release_select_sql(columns, 'WHERE id = :release_id')),
                {'release_id': release_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load release %s from namespace %s', release_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_release_from_row(row)


def _next_release_version(namespace):
    if namespace is None:
        return _format_release_version(1)

    releases = _load_namespace_releases(namespace)
    numeric_versions = []
    for release in releases:
        version = _release_version_number(release.version)
        if version is not None:
            numeric_versions.append(version)

    return _format_release_version((max(numeric_versions) if numeric_versions else 0) + 1)


def _create_namespace_release(namespace, release_data):
    if namespace is None:
        raise ValueError('Namespace is required')

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    release_id = None
    try:
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)

            existing = conn.execute(
                text("SELECT 1 FROM releases WHERE version = :version"),
                {'version': release_data['version']},
            ).first()
            if existing:
                raise IntegrityError('duplicate namespace release version', None, None)

            insert_fields = []
            insert_values = []
            params = {}
            for field_name, field_value in release_data.items():
                if field_name in columns:
                    insert_fields.append(field_name)
                    insert_values.append(f':{field_name}')
                    params[field_name] = field_value

            if 'created_at' in columns:
                insert_fields.append('created_at')
                insert_values.append('NOW()')
            if 'updated_at' in columns:
                insert_fields.append('updated_at')
                insert_values.append('NOW()')

            result = conn.execute(
                text(
                    f"""
                    INSERT INTO releases ({', '.join(insert_fields)})
                    VALUES ({', '.join(insert_values)})
                    RETURNING id
                    """
                ),
                params,
            )
            release_id = result.scalar()
    finally:
        engine.dispose()

    return _load_namespace_release(namespace, release_id)


def _update_namespace_release_fields(namespace, release_id, fields):
    if namespace is None or release_id is None or not fields:
        return

    allowed_fields = {
        'release_name',
        'release_type',
        'status',
        'release_notes',
        'include_stats_editor_changes',
        'include_palmares_changes',
        'include_photo_changes',
        'published_by_id',
        'published_at',
    }
    update_fields = {
        key: value
        for key, value in fields.items()
        if key in allowed_fields
    }
    if not update_fields:
        return

    assignments = [f'{field_name} = :{field_name}' for field_name in update_fields]
    assignments.append('updated_at = NOW()')
    params = dict(update_fields)
    params['release_id'] = release_id

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE releases
                    SET {', '.join(assignments)}
                    WHERE id = :release_id
                    """
                ),
                params,
            )
    finally:
        engine.dispose()


def _save_release_change_type_selection(namespace, release, change_types):
    if release is None:
        return 0

    release_type = _release_type_from_change_types(change_types)
    if not release_type:
        raise ValueError('Select at least one change type for this release.')

    release.include_stats_editor_changes = bool(change_types.get('stats_editor'))
    release.include_palmares_changes = bool(change_types.get('palmares'))
    release.include_photo_changes = bool(change_types.get('photos'))
    release.release_type = release_type

    stats_rows = 0
    if namespace is not None:
        stats_rows = _sync_release_stats_change_type(
            namespace,
            release.id,
            release.include_stats_editor_changes,
        )

    _update_namespace_release_fields(
        namespace,
        release.id,
        {
            'release_type': release.release_type,
            'include_stats_editor_changes': release.include_stats_editor_changes,
            'include_palmares_changes': release.include_palmares_changes,
            'include_photo_changes': release.include_photo_changes,
        },
    )
    return stats_rows


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

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT rv.relative_path
                    FROM release_files rv
                    JOIN releases r ON r.id = rv.release_id
                    WHERE rv.file_type = 'cdb_sqlite'
                      AND r.id <> :release_id
                    ORDER BY r.created_at DESC NULLS LAST, r.id DESC
                    """
                ),
                {'release_id': release.id},
            ).mappings().all()
    finally:
        engine.dispose()

    for row in rows:
        previous_path = _resolve_namespace_sqlite_file_path(namespace, row.get('relative_path'))
        if previous_path:
            return previous_path

    return _resolve_namespace_sqlite_file_path(namespace, namespace.sqlite_file_path)


def _load_namespace_release_files(namespace, release_id):
    """Load generated release file records from the namespace database."""
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
                    FROM release_files
                    WHERE release_id = :release_id
                    ORDER BY created_at ASC NULLS LAST, id ASC
                    """
                ),
                {'release_id': release_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load release file records for namespace %s release %s', namespace.slug, release_id)
        return []
    finally:
        engine.dispose()

    label_map = {
        'cdb_sqlite': 'CDB SQLite',
        'rider_stats_sql': 'DYN Cyclist',
        'release_overview_html': 'Release Overview HTML',
    }

    normalized_rows = []
    for row in rows:
        row_dict = dict(row)
        row_dict['file_type_label'] = label_map.get(row_dict.get('file_type'), row_dict.get('file_type') or '-')
        row_dict['file_name'] = os.path.basename(row_dict.get('relative_path') or '')
        normalized_rows.append(row_dict)

    return normalized_rows


def _replace_namespace_release_files(namespace, release_id, files):
    """Replace generated release file records for one release in the namespace database."""
    if namespace is None or release_id is None:
        return

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM release_files WHERE release_id = :release_id"),
                {'release_id': release_id},
            )

            for file_info in files:
                conn.execute(
                    text(
                        """
                        INSERT INTO release_files (
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
                        cdb_cyclist_id,
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
        cdb_cyclist_id = _safe_int(row.get('cdb_cyclist_id'))
        raw_stat_name = (row.get('stat_name') or '').strip().lower()
        final_value = _safe_int(row.get('final_value'))

        if cdb_cyclist_id is None or not raw_stat_name or final_value is None:
            continue

        stat_code = raw_stat_name[5:] if raw_stat_name.startswith('stat_') else raw_stat_name
        if stat_code not in STAT_DELTA_FIELDS:
            continue

        source_column = stat_column_by_code.get(stat_code)
        if not source_column:
            continue

        updates[(cdb_cyclist_id, source_column)] = final_value

    return {
        'table_name': cyclist_table_name,
        'id_column': cyclist_id_column,
        'updates': updates,
    }


def _mysql_dump_value(value, quote=True):
    if value is None:
        return 'NULL'
    if not quote:
        int_value = _safe_int(value)
        return str(int_value) if int_value is not None else 'NULL'

    text_value = str(value)
    text_value = text_value.replace('\\', '\\\\')
    text_value = text_value.replace("'", "\\'")
    text_value = text_value.replace('\n', '\\n')
    text_value = text_value.replace('\r', '\\r')
    return f"'{text_value}'"


def _load_release_dyn_cyclist_rows(namespace, release_id, updates):
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
                        last_name,
                        first_name,
                        birth_date,
                        stat_fla,
                        stat_mo,
                        stat_mm,
                        stat_dh,
                        stat_cob,
                        stat_tt,
                        stat_prl,
                        stat_spr,
                        stat_acc,
                        stat_end,
                        stat_res,
                        stat_rec,
                        stat_hil,
                        stat_att,
                        first_cycling_id
                    FROM cdb_cyclist
                    ORDER BY id ASC
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    pcm_version = namespace.pcm_version or 'PCM2025'
    cyclist_fields = (get_field_mapping(pcm_version, 'cyclist').get('fields') or {})
    internal_stat_by_cdb_column = {
        cdb_column: internal_name
        for internal_name, cdb_column in cyclist_fields.items()
        if internal_name.startswith('stat_')
    }

    dyn_rows = []
    for row in rows:
        row_dict = dict(row)
        cyclist_id = row_dict.get('id')
        for (updated_cyclist_id, cdb_stat_column), final_value in updates.items():
            if updated_cyclist_id != cyclist_id:
                continue
            internal_stat = internal_stat_by_cdb_column.get(cdb_stat_column)
            if internal_stat:
                row_dict[internal_stat] = final_value
        dyn_rows.append(row_dict)

    return dyn_rows


def _build_dyn_cyclist_mysql_dump(release, dyn_rows):
    release_title = f'{release.release_name or "Release"} {release.version or ""}'.strip()
    lines = [
        '-- MySQL dump 10.13  Distrib 8.0.41, for Win64 (x86_64)',
        '--',
        f'-- Release: {release_title}',
        '-- Host: localhost    Database: officialrelease',
        '-- ------------------------------------------------------',
        '-- Server version\t8.0.41',
        '',
        '/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;',
        '/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;',
        '/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;',
        '/*!50503 SET NAMES utf8 */;',
        '/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;',
        "/*!40103 SET TIME_ZONE='+00:00' */;",
        '/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;',
        '/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;',
        "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;",
        '/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;',
        '',
        '--',
        '-- Table structure for table `dyn_cyclist`',
        '--',
        '',
        'DROP TABLE IF EXISTS `dyn_cyclist`;',
        '/*!40101 SET @saved_cs_client     = @@character_set_client */;',
        '/*!50503 SET character_set_client = utf8mb4 */;',
        'CREATE TABLE `dyn_cyclist` (',
        '  `IDcyclist` text,',
        '  `gene_sz_lastname` text,',
        '  `gene_sz_firstname` text,',
        '  `gene_i_birthdate` text,',
        '  `charac_i_plain` text,',
        '  `charac_i_mountain` text,',
        '  `charac_i_medium_mountain` text,',
        '  `charac_i_downhilling` text,',
        '  `charac_i_cobble` text,',
        '  `charac_i_timetrial` text,',
        '  `charac_i_prologue` text,',
        '  `charac_i_sprint` text,',
        '  `charac_i_acceleration` text,',
        '  `charac_i_endurance` text,',
        '  `charac_i_resistance` text,',
        '  `charac_i_recuperation` text,',
        '  `charac_i_hill` text,',
        '  `charac_i_baroudeur` text,',
        '  `firstCyclingId` int DEFAULT NULL',
        ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;',
        '/*!40101 SET character_set_client = @saved_cs_client */;',
        '',
        '--',
        '-- Dumping data for table `dyn_cyclist`',
        '--',
        '',
        'LOCK TABLES `dyn_cyclist` WRITE;',
        '/*!40000 ALTER TABLE `dyn_cyclist` DISABLE KEYS */;',
    ]

    if dyn_rows:
        value_rows = []
        for row in dyn_rows:
            values = [
                _mysql_dump_value(row.get('id')),
                _mysql_dump_value(row.get('last_name')),
                _mysql_dump_value(row.get('first_name')),
                _mysql_dump_value(row.get('birth_date')),
                _mysql_dump_value(row.get('stat_fla')),
                _mysql_dump_value(row.get('stat_mo')),
                _mysql_dump_value(row.get('stat_mm')),
                _mysql_dump_value(row.get('stat_dh')),
                _mysql_dump_value(row.get('stat_cob')),
                _mysql_dump_value(row.get('stat_tt')),
                _mysql_dump_value(row.get('stat_prl')),
                _mysql_dump_value(row.get('stat_spr')),
                _mysql_dump_value(row.get('stat_acc')),
                _mysql_dump_value(row.get('stat_end')),
                _mysql_dump_value(row.get('stat_res')),
                _mysql_dump_value(row.get('stat_rec')),
                _mysql_dump_value(row.get('stat_hil')),
                _mysql_dump_value(row.get('stat_att')),
                _mysql_dump_value(row.get('first_cycling_id'), quote=False),
            ]
            value_rows.append(f"({','.join(values)})")

        lines.append(f"INSERT INTO `dyn_cyclist` VALUES {','.join(value_rows)};")

    lines.extend([
        '/*!40000 ALTER TABLE `dyn_cyclist` ENABLE KEYS */;',
        'UNLOCK TABLES;',
        '/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;',
        '/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;',
        '/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;',
        '/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;',
        '/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;',
        '/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;',
        '/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;',
        '/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;',
        '',
    ])

    return '\n'.join(lines)


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
    """Create a DYN Cyclist MySQL dump for this release's rider stat updates."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    updates_payload = _load_release_stat_updates(namespace, release.id)
    updates = updates_payload['updates']
    dyn_rows = _load_release_dyn_cyclist_rows(namespace, release.id, updates)
    dump_content = _build_dyn_cyclist_mysql_dump(release, dyn_rows)

    file_info = _create_release_text_artifact(
        namespace,
        f'{artifact_basename}-DUMP_dyn_cyclist.sql',
        dump_content,
        'application/sql',
    )
    file_info['display_name'] = 'DYN Cyclist'
    file_info['applied_changes'] = len(updates)
    return file_info


def _build_release_overview_html_for_release(
    namespace,
    release,
    artifact_basename,
    generated_files,
    release_status=None,
):
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
            release_status=release_status or release.status,
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
    if getattr(release, 'include_stats_editor_changes', False):
        _sync_release_stats_change_type(namespace, release.id, True)

    artifact_basename = _build_release_artifact_basename(namespace, release)

    sqlite_file = _build_review_sqlite_for_release(namespace, release, artifact_basename)
    sqlite_file['file_type'] = 'cdb_sqlite'
    sqlite_file['file_type_label'] = 'CDB SQLite'

    sql_file = _build_release_stat_sql_for_release(namespace, release, artifact_basename)
    sql_file['file_type'] = 'rider_stats_sql'
    sql_file['file_type_label'] = 'DYN Cyclist'

    overview_file = _build_release_overview_html_for_release(
        namespace,
        release,
        artifact_basename,
        [sqlite_file, sql_file],
        release_status='review',
    )
    overview_file['file_type'] = 'release_overview_html'
    overview_file['file_type_label'] = 'Release Overview HTML'

    generated_files = [sqlite_file, sql_file, overview_file]
    _replace_namespace_release_files(namespace, release.id, generated_files)

    return {
        'sqlite_file': sqlite_file,
        'files': generated_files,
        'applied_changes': sqlite_file.get('applied_changes', 0),
    }


def _load_scope_stat_change_conflicts(conn, where_clause, params):
    """Return pending stat changes whose snapshot baseline no longer matches CDB."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT
                stat_changes.id,
                stat_changes.race_result_id,
                stat_changes.stat_list_id,
                stat_changes.cdb_cyclist_id,
                stat_changes.stat_name,
                stat_changes.original_value,
                stat_changes.final_value,
                COALESCE(stat_changes.is_confilict_override, FALSE) AS is_confilict_override,
                c.first_name,
                c.last_name,
                c.first_cycling_id,
                {_snapshot_stat_select_list('c')}
            FROM stat_changes
            JOIN cdb_cyclist c ON c.id = stat_changes.cdb_cyclist_id
            WHERE {where_clause}
              AND stat_changes.is_approved = FALSE
            ORDER BY stat_changes.id ASC
            """
        ),
        params,
    ).mappings().all()

    conflicts = []
    for row in pending_rows:
        final_value = _safe_int(row.get('final_value'))
        original_value = _safe_int(row.get('original_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = STAT_COLUMN_MAP.get(stat_code)
        current_value = _safe_int(row.get(column_name)) if column_name else None

        if original_value is None or final_value is None or current_value is None:
            continue

        if current_value != original_value and current_value != final_value and not row.get('is_confilict_override'):
            rider_name = ' '.join(
                part
                for part in [row.get('first_name'), row.get('last_name')]
                if part
            ).strip() or f"Rider #{row.get('cdb_cyclist_id')}"
            conflicts.append({
                'stat_change_id': row.get('id'),
                'race_result_id': row.get('race_result_id'),
                'stat_list_id': row.get('stat_list_id'),
                'first_cycling_rider_id': row.get('first_cycling_id') or row.get('cdb_cyclist_id'),
                'cdb_cyclist_id': row.get('cdb_cyclist_id'),
                'rider_name': rider_name,
                'stat_name': stat_code.upper(),
                'stat_code': stat_code,
                'snapshot_value': original_value,
                'current_value': current_value,
                'proposed_value': final_value,
            })

    return conflicts


def _load_namespace_scope_conflicts(namespace, first_cycling_race_id=None, stat_list_id=None):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            if stat_list_id is not None:
                return _load_scope_stat_change_conflicts(
                    conn,
                    """
                    stat_changes.race_result_id IS NULL
                    AND stat_changes.stat_list_id = :list_id
                    """,
                    {'list_id': stat_list_id},
                )
            race_result_ids = _load_firstcycling_race_result_map_ids(
                first_cycling_race_id,
                include_all_race_stages=True,
            )
            if not race_result_ids:
                return []
            return _load_scope_stat_change_conflicts(
                conn,
                """
                stat_changes.race_result_id = ANY(:race_result_ids)
                AND stat_changes.stat_list_id IS NULL
                """,
                {'race_result_ids': race_result_ids},
            )
    except Exception:
        logger.exception('Failed to load stat conflicts from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()


def _apply_scope_stat_changes_to_cdb(conn, where_clause, params):
    """Apply pending stat_changes.final_value into cdb_cyclist for one scope."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT id, cdb_cyclist_id, stat_name, final_value
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
        cdb_cyclist_id = _safe_int(row.get('cdb_cyclist_id'))
        final_value = _safe_int(row.get('final_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = STAT_COLUMN_MAP.get(stat_code)

        if cdb_cyclist_id is None or final_value is None or not column_name:
            continue

        conn.execute(
            text(
                f'''
                UPDATE cdb_cyclist
                SET "{column_name}" = :final_value
                WHERE id = :cdb_cyclist_id
                '''
            ),
            {
                'final_value': final_value,
                'cdb_cyclist_id': cdb_cyclist_id,
            },
        )
        applied_count += 1

    return applied_count


def _load_namespace_stat_lists(namespace, season_year=None, gender=None):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        params = {}
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
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


def _load_namespace_list_result_deltas(namespace, stat_list_id, cdb_cyclist_ids):
    """Load saved stat changes for one ad-hoc stat list keyed by CDB cyclist/stat."""
    cdb_cyclist_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
        if cdb_cyclist_id is not None
    ]
    if namespace is None or stat_list_id is None or not cdb_cyclist_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE stat_list_id = :list_id
                      AND race_result_id IS NULL
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {
                    'list_id': stat_list_id,
                    'cdb_cyclist_ids': cdb_cyclist_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load list stat changes from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['cdb_cyclist_id']
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
    final_stage = None
    if race.race_stages:
        final_stage = max(
            race.race_stages,
            key=lambda stage: stage.stage_number if stage.stage_number is not None else -1,
        )
    has_race_scope_result = any(
        result.result_scope == 'race' and result.stage_id is None
        for result in race.race_results
    )
    has_final_stage_overall_result = final_stage is not None and any(
        result.stage_id == final_stage.id and result.overall_position is not None
        for result in race.race_results
    )

    is_stage_race = len(race_stage_ids) > 0
    has_overall_result = has_race_scope_result or (is_stage_race and has_final_stage_overall_result)
    all_stages_saved = is_stage_race and race_stage_ids.issubset(saved_stage_ids)
    has_partial_stage_results = is_stage_race and len(saved_stage_ids.intersection(race_stage_ids)) > 0 and not all_stages_saved
    is_result_saved = all_stages_saved if is_stage_race else has_race_scope_result
    is_future_race = race.start_date is not None and race.start_date > datetime.utcnow().date()
    today = datetime.utcnow().date()
    is_completed = race.end_date is None or race.end_date <= today

    return {
        'is_stage_race': is_stage_race,
        'is_result_saved': is_result_saved,
        'has_overall_result': has_overall_result,
        'all_stages_saved': all_stages_saved,
        'has_partial_stage_results': has_partial_stage_results,
        'is_future_race': is_future_race,
        'is_completed': is_completed,
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
    """Decorator to require namespace management access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('web.login'))
        if not _can_access_namespace_management():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


def user_management_required(f):
    """Decorator to require user management access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('web.login'))
        if not _can_access_user_management():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@web_bp.route('/')
def index():
    """Home page - redirect to dashboard if logged in, otherwise to login"""
    if current_user.is_authenticated:
        return redirect(url_for('web.dashboard'))
    return redirect(url_for('web.login'))


def _issue_user_token(user, purpose, duration, issued_by=None):
    now = datetime.utcnow()
    UserSecurityToken.query.filter_by(
        user_id=user.id,
        purpose=purpose,
        consumed_at=None,
    ).filter(UserSecurityToken.expires_at > now).update({'consumed_at': now})
    raw_token, token = UserSecurityToken.issue(user, purpose, duration, issued_by=issued_by)
    db.session.flush()
    return raw_token, token


def _invite_duration():
    return timedelta(hours=current_app.config['AUTH_INVITE_TOKEN_HOURS'])


def _reset_duration():
    return timedelta(minutes=current_app.config['AUTH_RESET_TOKEN_MINUTES'])


def _discord_oauth_is_configured():
    return bool(current_app.config.get('DISCORD_CLIENT_ID') and current_app.config.get('DISCORD_CLIENT_SECRET'))


def _discord_oauth_redirect_uri():
    return url_for('web.discord_auth_callback', _external=True)


def _start_discord_oauth(purpose, username=None):
    if not _discord_oauth_is_configured():
        flash('Discord account verification is not configured.', 'warning')
        return redirect(url_for('web.login'))

    state = secrets.token_urlsafe(24)
    session['discord_auth_state'] = state
    session['discord_auth_purpose'] = purpose
    if username:
        session['discord_reset_username'] = username

    query = urllib.parse.urlencode({
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'response_type': 'code',
        'redirect_uri': _discord_oauth_redirect_uri(),
        'scope': 'identify',
        'state': state,
        'prompt': 'consent',
    })
    return redirect(f'https://discord.com/oauth2/authorize?{query}')


def _discord_current_user(code):
    token_payload = urllib.parse.urlencode({
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'client_secret': current_app.config['DISCORD_CLIENT_SECRET'],
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': _discord_oauth_redirect_uri(),
    }).encode('utf-8')
    token_request = urllib.request.Request(
        'https://discord.com/api/v10/oauth2/token',
        data=token_payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    with urllib.request.urlopen(token_request, timeout=10) as token_response:
        access_token = json.loads(token_response.read().decode('utf-8'))['access_token']

    user_request = urllib.request.Request(
        'https://discord.com/api/v10/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    with urllib.request.urlopen(user_request, timeout=10) as user_response:
        return json.loads(user_response.read().decode('utf-8'))


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

        if user and user.login_is_locked():
            AuditLog.log_action(
                action=AuditAction.USER_LOGIN_LOCKED,
                user=user,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Invalid username or password', 'error')
            return render_template('login.html', form=form)

        if user and user.check_password(form.password.data):
            if not user.is_active:
                flash('Your account has been deactivated. Contact an administrator.', 'error')
                return redirect(url_for('web.login'))

            login_user(user, remember=form.remember.data)

            # Update last login time
            user.last_login = datetime.utcnow()
            user.clear_login_failures()
            
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
                user.register_login_failure()
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


@web_bp.route('/activate/<token>', methods=['GET', 'POST'])
def activate_user(token):
    security_token = UserSecurityToken.find_valid(token, TOKEN_INVITE)
    if not security_token:
        flash('That invite link is invalid or has expired.', 'error')
        return redirect(url_for('web.login'))

    form = PasswordSetupForm()
    if form.validate_on_submit():
        policy_error = validate_password_policy(form.new_password.data, security_token.user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
        else:
            user = security_token.user
            user.set_password(form.new_password.data)
            user.is_active = True
            user.is_temporary_password = False
            user.clear_login_failures()
            security_token.consume()
            AuditLog.log_action(
                action=AuditAction.USER_ACTIVATED,
                user=user,
                details={'via_invite': True},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Account activated. Log in with your new password.', 'success')
            return redirect(url_for('web.login'))

    return render_template('activate.html', form=form, user=security_token.user)


@web_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password_from_token(token):
    security_token = UserSecurityToken.find_valid(token, TOKEN_PASSWORD_RESET)
    if not security_token:
        flash('That reset link is invalid or has expired.', 'error')
        return redirect(url_for('web.login'))

    form = PasswordSetupForm()
    if form.validate_on_submit():
        policy_error = validate_password_policy(form.new_password.data, security_token.user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
        else:
            user = security_token.user
            user.set_password(form.new_password.data)
            user.is_temporary_password = False
            user.clear_login_failures()
            security_token.consume()
            AuditLog.log_action(
                action=AuditAction.USER_PASSWORD_RESET,
                user=user,
                details={'via_reset_link': True},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Password reset complete. Log in with your new password.', 'success')
            return redirect(url_for('web.login'))

    return render_template('reset_password.html', form=form, user=security_token.user)


@web_bp.route('/forgot-password')
def forgot_password():
    return render_template(
        'forgot_password.html',
        discord_reset_configured=_discord_oauth_is_configured(),
    )


@web_bp.route('/auth/discord/link')
@login_required
def discord_link_start():
    return _start_discord_oauth('link')


@web_bp.route('/auth/discord/reset')
def discord_reset_start():
    username = (request.args.get('username') or '').strip()
    if not username:
        flash('Enter your username to verify with Discord.', 'warning')
        return redirect(url_for('web.forgot_password'))
    return _start_discord_oauth('reset', username=username)


@web_bp.route('/auth/discord/callback')
def discord_auth_callback():
    expected_state = session.pop('discord_auth_state', None)
    purpose = session.pop('discord_auth_purpose', None)
    reset_username = session.pop('discord_reset_username', None)
    if not expected_state or not secrets.compare_digest(expected_state, request.args.get('state') or ''):
        flash('Discord verification expired. Try again.', 'error')
        return redirect(url_for('web.login'))
    if request.args.get('error') or not request.args.get('code'):
        flash('Discord verification was not completed.', 'error')
        return redirect(url_for('web.login'))

    try:
        discord_user = _discord_current_user(request.args['code'])
    except (KeyError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.warning('Discord OAuth verification failed: %s', exc)
        flash('Discord verification failed. Try again.', 'error')
        return redirect(url_for('web.login'))

    discord_user_id = str(discord_user['id'])
    if purpose == 'link':
        if not current_user.is_authenticated:
            flash('Log in before linking Discord.', 'warning')
            return redirect(url_for('web.login'))
        linked_user = User.query.filter_by(discord_user_id=discord_user_id).first()
        if linked_user and linked_user.id != current_user.id:
            flash('That Discord account is already linked.', 'error')
            return redirect(url_for('web.dashboard'))
        current_user.discord_user_id = discord_user_id
        AuditLog.log_action(
            action=AuditAction.USER_DISCORD_LINKED,
            user=current_user,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()
        flash('Discord account linked for password recovery.', 'success')
        return redirect(url_for('web.dashboard'))

    if purpose == 'reset':
        user = User.query.filter_by(username=reset_username, discord_user_id=discord_user_id).first()
        if user:
            raw_token, _ = _issue_user_token(user, TOKEN_PASSWORD_RESET, _reset_duration())
            db.session.commit()
            return redirect(url_for('web.reset_password_from_token', token=raw_token))
        flash('Discord verification did not match a recovery-enabled account.', 'error')
        return redirect(url_for('web.forgot_password'))

    flash('Discord verification expired. Try again.', 'error')
    return redirect(url_for('web.login'))


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


@web_bp.route('/account')
@login_required
def account():
    """Current user account page."""
    user_roles = current_user.roles.all() if hasattr(current_user, 'roles') else []

    return render_template(
        'account.html',
        user=current_user,
        user_roles=user_roles,
    )


@web_bp.route('/wiki')
@web_bp.route('/wiki/<section_slug>')
@web_bp.route('/wiki/<section_slug>/<path:page_path>')
@login_required
def wiki(section_slug=None, page_path=None):
    """User and admin documentation."""
    wiki_sections = load_wiki_sections()
    selected_section = find_wiki_section(section_slug, wiki_sections) if section_slug else None
    if section_slug and selected_section is None:
        abort(404)

    selected_page = find_wiki_page(selected_section, page_path) if page_path else None
    if page_path and selected_page is None:
        abort(404)
    page_content = load_wiki_page_content(selected_page) if selected_page and selected_page.get('file') else None

    return render_template(
        'wiki.html',
        wiki_sections=wiki_sections,
        selected_section=selected_section,
        selected_page=selected_page,
        page_content=page_content,
    )


def _load_dashboard_tasks(namespace):
    """Build permission-specific queues for the selected namespace."""
    tasks = {
        'show_releases': _can_approve_releases() and _can_access_panel('release_manager'),
        'show_reviews': _can_approve_stats() and _can_access_panel('stat_editor'),
        'show_editing': _can_update_stats() and _can_access_panel('stat_editor'),
        'releases': [],
        'reviews': [],
        'editing': {},
    }
    if namespace is None:
        return tasks

    if tasks['show_releases']:
        tasks['releases'] = [
            release for release in _load_namespace_releases(namespace)
            if release.status in ('review', 'pending_approval')
        ]

    if tasks['show_reviews'] or tasks['show_editing']:
        races = FirstCyclingRace.query.order_by(
            FirstCyclingRace.end_date.asc(), FirstCyclingRace.name.asc(),
        ).all()
        statuses = _load_namespace_race_delta_status(namespace, {race.race_id for race in races})
        today = datetime.utcnow().date()
        selected_year, selected_gender = _resolve_namespace_season(namespace)
        for race in races:
            status = statuses.get(race.race_id, {})
            total = status.get('total_changes', 0)
            if tasks['show_reviews'] and total > status.get('approved_changes', 0):
                tasks['reviews'].append({
                    'label': race.name or f'Race #{race.race_id}',
                    'url': url_for('web.stats_editor_race_detail', race_id=race.race_id),
                    'kind': 'Race result',
                })
            if (tasks['show_editing'] and race.end_date and race.end_date < today and not total
                    and race.season_year == selected_year and race.gender == selected_gender):
                classification = race.classification or 'Unclassified'
                tasks['editing'].setdefault(classification, []).append(race)
        tasks['editing'] = dict(sorted(tasks['editing'].items()))

    if tasks['show_reviews']:
        for stat_list in _load_namespace_stat_lists(namespace):
            if stat_list['total_changes'] > stat_list['approved_changes']:
                tasks['reviews'].append({
                    'label': stat_list['list_name'],
                    'url': url_for('web.stats_editor_list_detail', list_id=stat_list['id']),
                    'kind': 'Stat list',
                })
    return tasks


@web_bp.route('/dashboard')
@login_required
def dashboard():
    """User dashboard showing available app panels"""
    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    requested_namespace = (request.args.get('namespace', type=str) or '').strip()
    if requested_namespace:
        requested_namespace_lower = requested_namespace.lower()
        matched_namespace = next(
            (
                namespace
                for namespace in available_namespaces
                if str(namespace.id) == requested_namespace
                or (namespace.slug or '').strip().lower() == requested_namespace_lower
            ),
            None,
        )
        if matched_namespace is not None:
            selected_namespace = matched_namespace
            session['selected_namespace_id'] = matched_namespace.id

    waiting = _load_dashboard_tasks(selected_namespace)

    apps = []
    for panel_name, panel in APP_PANELS.items():
        if not _can_access_panel(panel_name):
            continue

        apps.append({
            'name': panel_name,
            'display_name': panel['display_name'],
            'description': panel['description'],
            'icon': get_app_icon(panel_name),
            'url': _get_panel_url(panel_name),
            'resources': panel['resources'],
        })
    
    return render_template(
        'dashboard.html',
        apps=apps,
        selected_namespace=selected_namespace,
        waiting=waiting,
    )


def get_app_icon(panel_name):
    """Get icon emoji for app panel"""
    icons = {
        'admin': '⚙️',
        'stat_editor': '📊',
        'team_manager': '👥',
        'release_manager': '📦',
    }
    return icons.get(panel_name, '📁')


@web_bp.route('/teams', methods=['GET', 'POST'])
@login_required
def teams():
    """Teams panel using selected namespace CDB roster data."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None
    active_method = _load_team_panel_active_method(selected_namespace)

    team_division_options = []
    team_country_options = []
    teams = []
    rider_counts = {}
    team_filters = _load_teams_state(selected_namespace)
    rider_filters = _load_riders_state(selected_namespace)
    riders = []
    rider_total = 0
    rider_country_options = []
    rider_division_options = []

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        requested_method = (request.form.get('method', type=str) or active_method).strip().lower()

        if action == 'set_method':
            if requested_method in ('teams', 'riders'):
                _save_team_panel_active_method(selected_namespace, requested_method)
        elif requested_method == 'riders':
            active_method = 'riders'
            _save_team_panel_active_method(selected_namespace, active_method)
            if action == 'clear_filters':
                rider_filters = _default_riders_state()
            elif action == 'set_page':
                rider_filters['page'] = max(request.form.get('page', type=int) or 1, 1)
            else:
                rider_filters.update({
                    'q': (request.form.get('q', type=str) or '').strip(),
                    'country': (request.form.get('country', type=str) or '').strip(),
                    'division': (request.form.get('division', type=str) or '').strip(),
                    'team': (request.form.get('team', type=str) or '').strip(),
                    'per_page': min(max(request.form.get('per_page', type=int) or 100, 25), 250),
                    'page': 1,
                })
            _save_riders_state(selected_namespace, rider_filters)
        else:
            active_method = 'teams'
            _save_team_panel_active_method(selected_namespace, active_method)
            if action == 'clear_filters':
                team_filters = _default_teams_state()
            else:
                team_filters = {
                    'division': (request.form.get('division', type=str) or '').strip(),
                    'country': (request.form.get('country', type=str) or '').strip(),
                    'team': (request.form.get('team', type=str) or '').strip(),
                }
            _save_teams_state(selected_namespace, team_filters)
        return redirect(url_for('web.teams'))

    if season_selected and active_method == 'teams':
        teams, team_division_options, team_country_options, rider_counts = _load_namespace_roster_teams(
            selected_namespace,
            team_filters['division'],
            team_filters['country'],
            team_filters['team'],
        )
        division_option_values = {option['value'] for option in team_division_options}
        country_option_values = {option['value'] for option in team_country_options}
        filters_changed = False
        if team_filters['division'] and team_filters['division'] not in division_option_values:
            team_filters['division'] = ''
            filters_changed = True
        if team_filters['country'] and team_filters['country'] not in country_option_values:
            team_filters['country'] = ''
            filters_changed = True
        if filters_changed:
            _save_teams_state(selected_namespace, team_filters)
            teams, team_division_options, team_country_options, rider_counts = _load_namespace_roster_teams(
                selected_namespace,
                team_filters['division'],
                team_filters['country'],
                team_filters['team'],
            )
    elif season_selected and active_method == 'riders':
        filters = {
            'q': rider_filters['q'],
            'country': rider_filters['country'],
            'division': rider_filters['division'],
            'team': rider_filters['team'],
        }
        page = rider_filters['page']
        per_page = rider_filters['per_page']

        riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
            selected_namespace,
            filters,
            page,
            per_page,
        )
        country_option_values = {option['value'] for option in rider_country_options}
        division_option_values = {option['value'] for option in rider_division_options}
        filters_changed = False
        if filters['country'] and filters['country'] not in country_option_values:
            filters['country'] = ''
            filters_changed = True
        if filters['division'] and filters['division'] not in division_option_values:
            filters['division'] = ''
            filters_changed = True
        if filters_changed:
            page = 1
            rider_filters.update(filters)
            rider_filters['page'] = page
            _save_riders_state(selected_namespace, rider_filters)
            riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
                selected_namespace,
                filters,
                page,
                per_page,
            )

        total_pages = max((rider_total + per_page - 1) // per_page, 1)
        if page > total_pages:
            page = total_pages
            rider_filters['page'] = page
            _save_riders_state(selected_namespace, rider_filters)
            riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
                selected_namespace,
                filters,
                page,
                per_page,
            )
    else:
        page = rider_filters['page']
        per_page = rider_filters['per_page']
        total_pages = 1

    if active_method == 'riders':
        rider_pagination = {
            'page': page,
            'per_page': per_page,
            'total': rider_total,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_page': page - 1,
            'next_page': page + 1,
            'start': ((page - 1) * per_page + 1) if rider_total else 0,
            'end': min(page * per_page, rider_total),
        }
    else:
        rider_pagination = {
            'page': rider_filters['page'],
            'per_page': rider_filters['per_page'],
            'total': 0,
            'total_pages': 1,
            'has_prev': False,
            'has_next': False,
            'prev_page': 0,
            'next_page': 2,
            'start': 0,
            'end': 0,
        }

    return render_template(
        'teams.html',
        selected_namespace=selected_namespace,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        teams=teams,
        rider_counts=rider_counts,
        division_options=team_division_options,
        country_options=team_country_options,
        selected_filters=team_filters,
        active_method=active_method,
        riders=riders,
        rider_country_options=rider_country_options,
        rider_division_options=rider_division_options,
        rider_filters=rider_filters,
        rider_pagination=rider_pagination,
        per_page_options=(50, 100, 150, 250),
    )


@web_bp.route('/riders', methods=['GET', 'POST'])
@login_required
def riders():
    """Compatibility route for the Riders tab in the Teams panel."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    state = _load_riders_state(selected_namespace)

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        if action == 'clear_filters':
            state = _default_riders_state()
        elif action == 'set_page':
            state['page'] = max(request.form.get('page', type=int) or 1, 1)
        else:
            state.update({
                'q': (request.form.get('q', type=str) or '').strip(),
                'country': (request.form.get('country', type=str) or '').strip(),
                'division': (request.form.get('division', type=str) or '').strip(),
                'team': (request.form.get('team', type=str) or '').strip(),
                'per_page': min(max(request.form.get('per_page', type=int) or 100, 25), 250),
                'page': 1,
            })
        _save_riders_state(selected_namespace, state)

    _save_team_panel_active_method(selected_namespace, 'riders')
    return redirect(url_for('web.teams'))


@web_bp.route('/teams/<int:team_id>')
@login_required
def team_detail(team_id):
    """Team detail page by CDB team ID."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    team = _load_namespace_roster_team(selected_namespace, team_id)
    if team is None:
        abort(404)

    riders = _load_namespace_roster_riders_for_team(selected_namespace, team_id)
    rider_stats_map = {
        rider.rider_id: _namespace_roster_stats_from_rider(rider)
        for rider in riders
        if rider.rider_id is not None
    }

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
    """Rider detail page by CDB cyclist/FirstCycling rider ID."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider = _load_namespace_roster_rider(selected_namespace, rider_id)
    if rider is None:
        abort(404)
    rider = _enrich_namespace_roster_rider_with_firstcycling(rider)

    team = None
    if rider.team_id is not None:
        team = _load_namespace_roster_team(selected_namespace, rider.team_id)

    rider_stats = _namespace_roster_stats_from_rider(rider)
    rider_measurements = {
        'size_cm': rider.size_cm,
        'weight_kg': rider.weight_kg,
    }
    rider_stat_edits = _load_namespace_rider_stat_edits(selected_namespace, rider.cyclist_id)

    back_team_id = request.args.get('team_id', type=int)
    if back_team_id is None and rider.team_id is not None:
        back_team_id = rider.team_id
    referrer_path = ''
    if request.referrer:
        parsed_referrer = urllib.parse.urlsplit(request.referrer)
        if not parsed_referrer.netloc or parsed_referrer.netloc == request.host:
            referrer_path = parsed_referrer.path or ''
    came_from_riders = referrer_path.endswith('/riders') or (
        referrer_path.endswith('/teams')
        and _load_team_panel_active_method(selected_namespace) == 'riders'
    )

    return render_template(
        'rider_detail.html',
        rider=rider,
        team=team,
        rider_stats=rider_stats,
        rider_measurements=rider_measurements,
        rider_stat_edits=rider_stat_edits,
        selected_namespace=selected_namespace,
        stat_fields=STAT_DELTA_FIELDS,
        back_url=url_for('web.teams'),
        back_label='Back to Riders' if came_from_riders else 'Back to Teams',
        back_params={
            'team_id': back_team_id,
            'division': request.args.get('division', type=str) or '',
        },
    )


STATS_EDITOR_FILTER_FIELDS = (
    'race_name',
    'classification',
    'nation',
    'start_date_from',
    'start_date_to',
    'is_stage_race',
)


def _stats_editor_session_key(namespace):
    return str(namespace.id) if namespace is not None else 'none'


def _default_stats_editor_race_filters():
    return {
        'race_name': '',
        'classification': '',
        'nation': '',
        'start_date_from': '',
        'start_date_to': '',
        'is_stage_race': '',
        'edit_statuses': [],
    }


def _load_stats_editor_state(namespace):
    states = session.get('stats_editor_state_by_namespace') or {}
    state = states.get(_stats_editor_session_key(namespace)) or {}
    active_method = (state.get('active_method') or 'by_race').strip().lower()
    if active_method not in ('by_race', 'by_list'):
        active_method = 'by_race'

    filters = _default_stats_editor_race_filters()
    filters.update({
        key: (state.get('race_filters') or {}).get(key, filters[key])
        for key in STATS_EDITOR_FILTER_FIELDS
    })
    filters['edit_statuses'] = [
        'waiting_edits' if value in ('no_edits', 'pending_edits') else value
        for value in (state.get('race_filters') or {}).get('edit_statuses', filters['edit_statuses'])
        if value in ('waiting_edits', 'no_edits', 'pending_edits', 'pending_approval', 'approved')
    ]
    return {
        'active_method': active_method,
        'race_filters': filters,
    }


def _save_stats_editor_state(namespace, state):
    states = dict(session.get('stats_editor_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['stats_editor_state_by_namespace'] = states


def _set_stats_editor_active_method(namespace, active_method):
    if active_method not in ('by_race', 'by_list'):
        return
    state = _load_stats_editor_state(namespace)
    state['active_method'] = active_method
    _save_stats_editor_state(namespace, state)


def _default_teams_state():
    return {
        'division': '',
        'country': '',
        'team': '',
    }


def _load_teams_state(namespace):
    states = session.get('teams_state_by_namespace') or {}
    saved_state = states.get(_stats_editor_session_key(namespace)) or {}
    state = _default_teams_state()
    state.update({
        key: (saved_state.get(key, state[key]) or '').strip()
        for key in state
    })
    return state


def _save_teams_state(namespace, state):
    states = dict(session.get('teams_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['teams_state_by_namespace'] = states


def _default_riders_state():
    return {
        'q': '',
        'country': '',
        'division': '',
        'team': '',
        'page': 1,
        'per_page': 100,
    }


def _load_riders_state(namespace):
    states = session.get('riders_state_by_namespace') or {}
    saved_state = states.get(_stats_editor_session_key(namespace)) or {}
    state = _default_riders_state()
    for key in ('q', 'country', 'division', 'team'):
        state[key] = (saved_state.get(key, state[key]) or '').strip()
    state['page'] = max(_safe_int(saved_state.get('page')) or state['page'], 1)
    state['per_page'] = min(max(_safe_int(saved_state.get('per_page')) or state['per_page'], 25), 250)
    return state


def _save_riders_state(namespace, state):
    states = dict(session.get('riders_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['riders_state_by_namespace'] = states


def _load_team_panel_active_method(namespace):
    states = session.get('team_panel_state_by_namespace') or {}
    state = states.get(_stats_editor_session_key(namespace)) or {}
    active_method = (state.get('active_method') or 'teams').strip().lower()
    if active_method not in ('teams', 'riders'):
        active_method = 'teams'
    return active_method


def _save_team_panel_active_method(namespace, active_method):
    if active_method not in ('teams', 'riders'):
        return
    states = dict(session.get('team_panel_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = {'active_method': active_method}
    session['team_panel_state_by_namespace'] = states


@web_bp.route('/stats-editor', methods=['GET', 'POST'])
@login_required
def stats_editor():
    """Stats editor panel with two methods: by race result and by ad-hoc list."""
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None
    editor_state = _load_stats_editor_state(selected_namespace)

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        next_state = {
            'active_method': editor_state['active_method'],
            'race_filters': dict(editor_state['race_filters']),
        }

        if action == 'dashboard_classification':
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = _default_stats_editor_race_filters()
            next_state['race_filters'].update({
                'classification': (request.form.get('classification', type=str) or '').strip(),
                'start_date_to': datetime.utcnow().date().isoformat(),
                'edit_statuses': ['waiting_edits'],
            })
        elif action == 'set_method':
            requested_method = (request.form.get('method', type=str) or 'by_race').strip().lower()
            if requested_method in ('by_race', 'by_list'):
                next_state['active_method'] = requested_method
        elif action == 'clear_filters':
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = _default_stats_editor_race_filters()
        else:
            valid_edit_statuses = ('waiting_edits', 'no_edits', 'pending_edits', 'pending_approval', 'approved')
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = {
                key: (request.form.get(key, type=str) or '').strip()
                for key in STATS_EDITOR_FILTER_FIELDS
            }
            next_state['race_filters']['edit_statuses'] = [
                'waiting_edits' if value in ('no_edits', 'pending_edits') else value
                for value in request.form.getlist('edit_status')
                if value in valid_edit_statuses
            ]

        _save_stats_editor_state(selected_namespace, next_state)
        return redirect(url_for('web.stats_editor'))

    active_method = editor_state['active_method']

    races = []
    race_status_map = {}
    stage_status_map = {}
    list_entries = []
    selected_list_id = None
    selected_list = None
    list_items = []
    list_riders = []
    list_search_rider = ''
    list_search_team = ''
    list_search_results = []
    list_item_rider_ids = set()
    list_scope_status = {
        'total_changes': 0,
        'approved_changes': 0,
        'unapproved_changes': 0,
    }
    filter_choices = {
        'classifications': [],
        'has_unclassified': False,
        'nations': [],
    }

    selected_filters = dict(editor_state['race_filters'])
    selected_edit_statuses = selected_filters['edit_statuses']

    if season_selected and active_method == 'by_race':
        if not selected_filters['start_date_from'] and not selected_filters['start_date_to']:
            selected_filters['start_date_from'] = f'{selected_year}-01-01'
        if not selected_filters['start_date_to']:
            selected_filters['start_date_to'] = f'{selected_year}-12-31'

        base_query = FirstCyclingRace.query.filter_by(
            season_year=selected_year,
            gender=selected_gender,
        )

        filter_choices['classifications'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.classification).distinct().order_by(FirstCyclingRace.classification).all()
            if value
        ]
        filter_choices['nations'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.nation_name).distinct().order_by(FirstCyclingRace.nation_name).all()
            if value
        ]

        if selected_filters['classification'] and selected_filters['classification'] != '__unclassified__':
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
            query = query.filter(
                func.coalesce(FirstCyclingRace.end_date, FirstCyclingRace.start_date)
                < parsed_start_date_to
            )

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
            is_pending_approval = has_changes and not all_approved
            is_approved = all_approved

            if selected_edit_statuses:
                matches_status_filter = (
                    ('waiting_edits' in selected_edit_statuses and not has_changes)
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

        # Offer classifications with matches under every other active filter.
        filter_choices['classifications'] = sorted({
            race.classification for race in filtered_races if race.classification
        })
        filter_choices['has_unclassified'] = any(
            not race.classification for race in filtered_races
        )
        classification = selected_filters['classification']
        if (
            classification == '__unclassified__' and not filter_choices['has_unclassified']
        ) or (
            classification and classification != '__unclassified__'
            and classification not in filter_choices['classifications']
        ):
            classification = selected_filters['classification'] = ''
        if classification == '__unclassified__':
            races = [race for race in filtered_races if not race.classification]
        elif classification:
            races = [race for race in filtered_races if race.classification == classification]
        else:
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
        can_create_stats=_can_create_stats(),
    )


@web_bp.route('/stats-editor/lists/create', methods=['POST'])
@login_required
def stats_editor_create_list():
    if not _can_create_stats():
        abort(403)

    list_name = (request.form.get('list_name', type=str) or '').strip()
    if not list_name:
        flash('List Name is required.', 'warning')
        return redirect(url_for('web.stats_editor'))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))

    season_year, gender = _resolve_namespace_season(selected_namespace)
    if season_year is None or not gender:
        flash('Selected namespace must have Database Year and Gender before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))

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
        return redirect(url_for('web.stats_editor'))
    finally:
        engine.dispose()

    flash(f"Created list '{list_name}'.", 'success')
    return redirect(url_for('web.stats_editor_list_detail', list_id=new_list_id))


@web_bp.route('/stats-editor/lists/<int:list_id>/add-rider', methods=['POST'])
@login_required
def stats_editor_add_list_rider(list_id):
    if not _can_access_panel('stat_editor'):
        abort(403)

    cdb_cyclist_id = request.form.get('cdb_cyclist_id', type=int)
    rider_id = request.form.get('rider_id', type=int)
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if cdb_cyclist_id is None and rider_id is None:
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
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM cdb_cyclist
                    WHERE (:cdb_cyclist_id IS NOT NULL AND id = :cdb_cyclist_id)
                       OR (:cdb_cyclist_id IS NULL AND first_cycling_id = :rider_id)
                    LIMIT 1
                    """
                ),
                {
                    'cdb_cyclist_id': cdb_cyclist_id,
                    'rider_id': rider_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider was not found in this namespace CDB.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

            conn.execute(
                text(
                    """
                    INSERT INTO stat_list_items (stat_list_id, cdb_cyclist_id)
                    VALUES (:list_id, :cdb_cyclist_id)
                    ON CONFLICT (stat_list_id, cdb_cyclist_id)
                    DO NOTHING
                    """
                ),
                {
                    'list_id': list_id,
                    'cdb_cyclist_id': rider_row['id'],
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
    if not _can_access_panel('stat_editor'):
        abort(403)

    cdb_cyclist_id = request.form.get('cdb_cyclist_id', type=int)
    rider_id = request.form.get('rider_id', type=int)
    is_checked = request.form.get('is_checked', type=str) == '1'
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if cdb_cyclist_id is None and rider_id is None:
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
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM cdb_cyclist
                    WHERE (:cdb_cyclist_id IS NOT NULL AND id = :cdb_cyclist_id)
                       OR (:cdb_cyclist_id IS NULL AND first_cycling_id = :rider_id)
                    LIMIT 1
                    """
                ),
                {
                    'cdb_cyclist_id': cdb_cyclist_id,
                    'rider_id': rider_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider is not linked in this namespace CDB yet.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))
            cdb_cyclist_id = rider_row['id']

            if is_checked:
                conn.execute(
                    text(
                        """
                        INSERT INTO stat_list_items (stat_list_id, cdb_cyclist_id)
                        VALUES (:list_id, :cdb_cyclist_id)
                        ON CONFLICT (stat_list_id, cdb_cyclist_id)
                        DO NOTHING
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE stat_list_id = :list_id
                          AND race_result_id IS NULL
                          AND cdb_cyclist_id = :cdb_cyclist_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                    },
                )
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_list_items
                        WHERE stat_list_id = :list_id
                          AND cdb_cyclist_id = :cdb_cyclist_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
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
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        abort(404)
    _set_stats_editor_active_method(selected_namespace, 'by_list')

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
                    SELECT i.cdb_cyclist_id, c.first_cycling_id AS first_cycling_rider_id
                    FROM stat_list_items i
                    JOIN cdb_cyclist c ON c.id = i.cdb_cyclist_id
                    WHERE i.stat_list_id = :list_id
                    ORDER BY i.id ASC
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

    cdb_cyclist_ids = [
        item['cdb_cyclist_id']
        for item in list_items
        if item.get('cdb_cyclist_id') is not None
    ]
    list_item_rider_ids = set(cdb_cyclist_ids)
    rider_rows = []
    if cdb_cyclist_ids:
        try:
            with engine.connect() as conn:
                rider_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            c.id AS cdb_cyclist_id,
                            c.first_cycling_id,
                            c.first_name,
                            c.last_name,
                            c.first_last_name,
                            c.team_id,
                            COALESCE(t.team_name, t.short_name) AS team_name,
                            {_snapshot_stat_select_list('c')}
                        FROM cdb_cyclist c
                        LEFT JOIN cdb_team t ON t.id = c.team_id
                        WHERE c.id = ANY(:cdb_cyclist_ids)
                        ORDER BY c.id ASC
                        """
                    ),
                    {'cdb_cyclist_ids': cdb_cyclist_ids},
                ).mappings().all()
        except Exception:
            logger.exception('Failed to load CDB cyclists for stat list %s in namespace %s', list_id, selected_namespace.slug)
            rider_rows = []
    rider_map = {
        row['cdb_cyclist_id']: row
        for row in rider_rows
    }

    if list_search_rider or list_search_team:
        search_where = []
        search_params = {
            'selected_cdb_ids': list(cdb_cyclist_ids) or [-1],
        }
        if list_search_rider:
            search_where.append(
                """
                (
                    c.first_name ILIKE :rider_term
                    OR c.last_name ILIKE :rider_term
                    OR c.first_last_name ILIKE :rider_term
                    OR CONCAT_WS(' ', c.first_name, c.last_name) ILIKE :rider_term
                )
                """
            )
            search_params['rider_term'] = f'%{list_search_rider}%'
        if list_search_team:
            search_where.append(
                """
                (
                    t.team_name ILIKE :team_term
                    OR t.short_name ILIKE :team_term
                    OR t.abbreviation ILIKE :team_term
                )
                """
            )
            search_params['team_term'] = f'%{list_search_team}%'

        search_filter = ' AND '.join(search_where) if search_where else 'TRUE'
        try:
            with engine.connect() as conn:
                search_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            c.id AS cdb_cyclist_id,
                            c.first_cycling_id,
                            c.first_name,
                            c.last_name,
                            c.first_last_name,
                            c.team_id,
                            COALESCE(t.team_name, t.short_name) AS team_name,
                            {_snapshot_stat_select_list('c')}
                        FROM cdb_cyclist c
                        LEFT JOIN cdb_team t ON t.id = c.team_id
                        WHERE {search_filter}
                          AND NOT (c.id = ANY(:selected_cdb_ids))
                        ORDER BY
                            c.last_name ASC NULLS LAST,
                            c.first_name ASC NULLS LAST,
                            c.first_last_name ASC NULLS LAST,
                            c.id ASC
                        LIMIT 50
                        """
                    ),
                    search_params,
                ).mappings().all()
        except Exception:
            logger.exception('Failed to search CDB cyclists for stat list in namespace %s', selected_namespace.slug)
            search_rows = []

        list_search_results = [
            SimpleNamespace(
                cdb_cyclist_id=row.get('cdb_cyclist_id'),
                rider_id=row.get('cdb_cyclist_id'),
                first_cycling_id=row.get('first_cycling_id'),
                name=_cdb_rider_display_name(row),
                first_name=row.get('first_name'),
                last_name=row.get('last_name'),
                team_id=row.get('team_id'),
                team_name=row.get('team_name'),
                stats=_snapshot_stats_from_row(row),
            )
            for row in search_rows
        ]

    rider_snapshot_map = _load_namespace_scope_snapshots(selected_namespace, stat_list_id=list_id, cdb_cyclist_ids=cdb_cyclist_ids)
    rider_delta_map = _load_namespace_list_result_deltas(selected_namespace, list_id, cdb_cyclist_ids)
    scope_delta_status = _load_namespace_scope_delta_status(
        selected_namespace,
        first_cycling_race_id=None,
        first_cycling_stage_id=None,
        stat_list_id=list_id,
    )
    stat_conflicts = _load_namespace_scope_conflicts(selected_namespace, stat_list_id=list_id)

    rows = []
    for cdb_cyclist_id in cdb_cyclist_ids:
        rider = rider_map.get(cdb_cyclist_id)
        rider_name = _cdb_rider_display_name(rider) if rider is not None else ''
        rider_name = rider_name or f'Rider #{cdb_cyclist_id}'

        rider_in_namespace = rider is not None
        if rider_in_namespace:
            base_stats = _snapshot_stats_from_row(rider)
            snapshot_stats = rider_snapshot_map.get(cdb_cyclist_id) or {}
            original_stats = dict(snapshot_stats or base_stats)
            final_stats = dict(original_stats)
            for stat_name, delta_info in (rider_delta_map.get(cdb_cyclist_id) or {}).items():
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
            'cdb_cyclist_id': cdb_cyclist_id,
            'rider_id': cdb_cyclist_id,
            'rider_link_id': cdb_cyclist_id,
            'rider_name': rider_name,
            'team_id': rider.get('team_id') if rider is not None else None,
            'team_name': (rider.get('team_name') if rider is not None and rider.get('team_name') else '-'),
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
        stat_conflicts=stat_conflicts,
        stat_fields=STAT_DELTA_FIELDS,
        can_approve_stats=_can_approve_stats(),
        can_update_stats=_can_update_stats(),
    )


@web_bp.route('/stats-editor/lists/<int:list_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_list_stat_deltas(list_id):
    if not _can_update_stats():
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

    cdb_cyclist_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    cdb_cyclist_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        saved_count = 0
        skipped_count = 0
        with engine.begin() as conn:
            snapshot_rider_rows = conn.execute(
                text(
                    """
                    SELECT cdb_cyclist_id
                    FROM stat_list_items
                    WHERE stat_list_id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().all()
            snapshot_cdb_ids = {
                _safe_int(row.get('cdb_cyclist_id'))
                for row in snapshot_rider_rows
            }
            snapshot_cdb_ids.discard(None)
            _ensure_scope_stat_snapshots(conn, stat_list_id=list_id, cdb_cyclist_ids=snapshot_cdb_ids)
            snapshot_rows = conn.execute(
                text(
                    f"""
                    SELECT cdb_cyclist_id, {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE race_result_id IS NULL
                      AND stat_list_id = :list_id
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {'list_id': list_id, 'cdb_cyclist_ids': list(cdb_cyclist_ids)},
            ).mappings().all()
            snapshot_map = {
                row['cdb_cyclist_id']: _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                cdb_cyclist_id = _safe_int(item.get('first_cycling_rider_id'))
                stat_name = (item.get('stat_name') or '').strip().lower()
                final_value = _safe_int(item.get('final_value'))
                original_value = _safe_int((snapshot_map.get(cdb_cyclist_id) or {}).get(stat_name))
                is_confilict_override = bool(item.get('is_confilict_override'))

                if (
                    cdb_cyclist_id is None
                    or stat_name not in STAT_DELTA_FIELDS
                    or original_value is None
                    or final_value is None
                    or final_value < STAT_MIN_VALUE
                    or final_value > STAT_MAX_VALUE
                ):
                    skipped_count += 1
                    continue

                delta_value = final_value - original_value
                if delta_value == 0 and not is_confilict_override:
                    conn.execute(
                        text(
                            """
                            DELETE FROM stat_changes
                            WHERE race_result_id IS NULL
                              AND stat_list_id = :list_id
                              AND cdb_cyclist_id = :cdb_cyclist_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'list_id': list_id,
                            'cdb_cyclist_id': cdb_cyclist_id,
                            'stat_name': stat_name,
                        },
                    )
                    continue

                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE race_result_id IS NULL
                          AND stat_list_id = :list_id
                          AND cdb_cyclist_id = :cdb_cyclist_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            race_result_id,
                            stat_list_id,
                            cdb_cyclist_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value,
                            is_confilict_override,
                            is_approved
                        )
                        VALUES (
                            NULL,
                            :list_id,
                            :cdb_cyclist_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            :is_confilict_override,
                            FALSE
                        )
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                        'is_confilict_override': is_confilict_override,
                    },
                )
                saved_count += 1

        if saved_count > 0:
            flash(f'Saved {saved_count} stat change(s).', 'success')
            try:
                list_name = load_namespace_stat_list_name(selected_namespace, list_id)
                review_url = url_for('web.stats_editor_list_detail', list_id=list_id, _external=True)
                if notify_discord_stat_list_saved(
                    selected_namespace,
                    list_id,
                    list_name,
                    review_url,
                    saved_count,
                    saved_by=current_user.username,
                ):
                    flash('Discord approval thread notified.', 'success')
            except Exception:
                logger.exception('Failed to send Discord notification for stat list %s', list_id)
                flash('Saved stat changes, but Discord notification failed.', 'warning')
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
    if not _can_approve_stats():
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
            conflicts = _load_scope_stat_change_conflicts(
                conn,
                """
                stat_changes.race_result_id IS NULL
                AND stat_changes.stat_list_id = :list_id
                """,
                {'list_id': list_id},
            )
            if conflicts:
                flash(f'Resolve {len(conflicts)} stat conflict(s) before approving this list.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

            applied_count = _apply_scope_stat_changes_to_cdb(
                conn,
                """
                race_result_id IS NULL
                AND stat_list_id = :list_id
                """,
                {'list_id': list_id},
            )
            result = conn.execute(
                text(
                    """
                    UPDATE stat_changes
                    SET is_approved = TRUE
                    WHERE race_result_id IS NULL
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
        try:
            if notify_discord_stat_list_approved(
                selected_namespace,
                list_id,
                approved_by=current_user.username,
            ):
                flash('Discord approval thread updated.', 'success')
        except Exception:
            logger.exception('Failed to send Discord approval message for stat list %s', list_id)
            flash('Approved stat changes, but Discord approval message failed.', 'warning')
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
        if not current_user.has_permission('releases', 'create'):
            abort(403)

        if selected_namespace is None:
            flash('Select a namespace before creating a release.', 'warning')
            return redirect(url_for('web.releases', tab='create'))

        version = _next_release_version(selected_namespace)
        release_name = (request.form.get('release_name', type=str) or '').strip() or None
        release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
        change_types = _release_change_types_from_form()
        release_type = _release_type_from_change_types(change_types)
        if not release_type:
            flash('Select at least one change type for this release.', 'error')
            return redirect(url_for('web.releases', tab='create'))

        try:
            release = _create_namespace_release(
                selected_namespace,
                {
                    'version': version,
                    'release_name': release_name,
                    'release_type': release_type,
                    'status': 'draft',
                    'release_notes': release_notes,
                    'include_stats_editor_changes': bool(change_types.get('stats_editor')),
                    'include_palmares_changes': bool(change_types.get('palmares')),
                    'include_photo_changes': bool(change_types.get('photos')),
                    'created_by_id': current_user.id,
                },
            )
            selected_release_id = release.id
            AuditLog.log_action(
                AuditAction.RELEASE_CREATED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:release',
                resource_id=release.id, resource_name=release_name or version,
                details={'version': version, 'status': 'draft'},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash(f'Release {version} created as draft.', 'success')
        except IntegrityError:
            flash('Release version already exists for this namespace.', 'error')
            return redirect(url_for('web.releases', tab='create'))
        except Exception:
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
        releases = _load_namespace_releases(selected_namespace)

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
        if request.form.get('save_change_types'):
            if not _can_update_releases():
                abort(403)

            if selected_namespace is None:
                flash('Select a namespace before editing release change types.', 'warning')
                return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

            change_types = _release_change_types_from_form()
            try:
                stats_rows = _save_release_change_type_selection(selected_namespace, selected_release, change_types)
                flash(
                    f'Updated release change types. Synced {stats_rows} stat change row(s).',
                    'success',
                )
            except Exception:
                logger.exception('Failed to update release change types for release %s', selected_release.id)
                flash('Failed to update release change types.', 'error')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        if request.form.get('save_release_notes'):
            if not _can_update_releases():
                abort(403)

            selected_release.release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
            try:
                _update_namespace_release_fields(
                    selected_namespace,
                    selected_release.id,
                    {'release_notes': selected_release.release_notes},
                )
                flash('Release notes updated.', 'success')
            except Exception:
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

        required_transition_actions = {
            ('draft', 'review'): 'update',
            ('review', 'draft'): 'update',
            ('review', 'approved'): 'approve',
            ('approved', 'published'): 'publish',
        }
        required_action = required_transition_actions.get((current_status, next_status))
        if required_action and not current_user.has_permission('releases', required_action):
            abort(403)

        review_build_result = None
        try:
            if current_status == 'draft' and next_status == 'review':
                review_build_result = _build_review_artifacts_for_release(selected_namespace, selected_release)

            selected_release.status = next_status
            if next_status == 'published':
                selected_release.published_by_id = current_user.id
                selected_release.published_at = datetime.utcnow()

            release_updates = {
                'status': selected_release.status,
                'published_by_id': selected_release.published_by_id,
                'published_at': selected_release.published_at,
            }
            _update_namespace_release_fields(selected_namespace, selected_release.id, release_updates)
            AuditLog.log_action(
                AuditAction.RELEASE_STATUS_CHANGED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:release',
                resource_id=selected_release.id,
                resource_name=selected_release.release_name or selected_release.version,
                details={'from_status': current_status, 'to_status': next_status},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
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
                try:
                    overview_file = next(
                        (
                            file_info
                            for file_info in review_build_result.get('files', [])
                            if file_info.get('file_type') == 'release_overview_html'
                        ),
                        None,
                    )
                    overview_path = _resolve_namespace_release_file_path(
                        selected_namespace,
                        overview_file.get('relative_path') if overview_file else None,
                    )
                    release_review_url = url_for(
                        'web.releases',
                        tab='overview',
                        release_id=selected_release.id,
                        _external=True,
                    )
                    if overview_path and notify_discord_release_review_requested(
                        selected_namespace,
                        selected_release,
                        release_review_url,
                        overview_path,
                    ):
                        flash('Discord release review request sent.', 'success')
                except Exception:
                    logger.exception('Failed to send Discord release review request for release %s', selected_release.id)
                    flash('Release moved to review, but Discord release notification failed.', 'warning')
            flash(f'Release moved to {next_status.title()}.', 'success')
        except ValueError as err:
            flash(str(err), 'error')
        except Exception:
            logger.exception('Failed to update release lifecycle status for release %s', selected_release.id)
            flash('Failed to update release status.', 'error')

        return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

    stats_release_summary = {
        'race_results_included': 0,
        'stat_lists_included': 0,
        'riders_with_edits': 0,
        'total_edits': 0,
    }
    stats_included_items = []
    release_files = []
    next_release_version = (
        _next_release_version(selected_namespace)
        if selected_namespace is not None
        else _format_release_version(1)
    )

    if selected_namespace is not None and active_tab == 'stats':
        stats_release_summary = _load_namespace_stats_release_summary(
            selected_namespace,
            selected_release.id,
            selected_release.status,
        )
        year, gender = _resolve_namespace_season(selected_namespace)
        stats_included_items = [
            item
            for item in _load_namespace_approved_release_items(
                selected_namespace,
                season_year=year,
                gender=gender,
                selected_release_id=selected_release.id,
                release_status=selected_release.status,
            )
            if item.get('is_selected')
        ]

    if selected_namespace is not None and selected_release is not None and active_tab == 'overview':
        release_files = _load_namespace_release_files(selected_namespace, selected_release.id)
        stats_release_summary = _load_namespace_stats_release_summary(
            selected_namespace,
            selected_release.id,
            selected_release.status,
        )

    return render_template(
        'releases.html',
        active_tab=active_tab,
        selected_namespace=selected_namespace,
        releases=releases,
        selected_release=selected_release,
        selected_release_id=selected_release_id,
        summary=summary,
        next_release_version=next_release_version,
        stats_release_summary=stats_release_summary,
        stats_included_items=stats_included_items,
        release_files=release_files,
        can_create_releases=current_user.has_permission('releases', 'create'),
        can_update_releases=_can_update_releases(),
        can_approve_releases=_can_approve_releases(),
        can_publish_releases=_can_publish_releases(),
    )


@web_bp.route('/releases/<int:release_id>/files/<int:file_id>/download')
@login_required
def release_file_download(release_id, file_id):
    if not _can_access_panel('release_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        abort(404)

    release = _load_namespace_release(selected_namespace, release_id)
    if release is None:
        abort(404)

    release_files = _load_namespace_release_files(selected_namespace, release.id)
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
        download_name=os.path.basename(absolute_path),
        mimetype=release_file.get('content_type') or None,
    )


@web_bp.route('/stats-editor/races/<int:race_id>')
@web_bp.route('/stats-editor/races/<int:race_id>/<int:stage_id>')
@login_required
def stats_editor_race_detail(race_id, stage_id=None):
    """Race detail page with optional stage-specific view."""
    if not _can_access_panel('stat_editor'):
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
    _set_stats_editor_active_method(selected_namespace, 'by_race')
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
    rider_link_map = _load_namespace_cyclist_link_map(selected_namespace, rider_ids)
    fc_to_cdb_id = {
        rider_id: (rider_link_map.get(rider_id) or {}).get('cyclist_id')
        for rider_id in rider_ids
    }
    cdb_cyclist_ids = [cdb_id for cdb_id in fc_to_cdb_id.values() if cdb_id is not None]
    stage_scope_id = selected_stage.id if selected_stage is not None else None
    include_all_race_stages = selected_stage is None and result_state['is_stage_race']
    race_result_map_ids = _load_firstcycling_race_result_map_ids(
        race.race_id,
        stage_scope_id,
        include_all_race_stages=include_all_race_stages,
    )
    scope_delta_status = _load_namespace_scope_delta_status(
        selected_namespace,
        race.race_id,
        stage_scope_id,
        include_all_race_stages=include_all_race_stages,
    )
    overall_delta_status = scope_delta_status if selected_stage is None else (
        _load_namespace_scope_delta_status(
            selected_namespace,
            race.race_id,
            include_all_race_stages=result_state['is_stage_race'],
        )
    )
    rider_snapshot_map = _load_namespace_scope_snapshots(
        selected_namespace,
        race_result_ids=race_result_map_ids,
        cdb_cyclist_ids=cdb_cyclist_ids,
    )
    rider_delta_map = _load_namespace_race_result_deltas(
        selected_namespace,
        race_result_map_ids,
        cdb_cyclist_ids,
    )
    stat_conflicts = _load_namespace_scope_conflicts(selected_namespace, first_cycling_race_id=race.race_id) if selected_stage is None else []
    delta_change_count = sum(len(changes) for changes in rider_delta_map.values())

    display_rows = []
    for result in result_rows:
        rider = rider_map.get(result.rider_id)
        rider_link = rider_link_map.get(result.rider_id) or {}
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
            cdb_cyclist_id = fc_to_cdb_id.get(result.rider_id)
            base_stats = rider_stats_map.get(result.rider_id) or {}
            snapshot_stats = rider_snapshot_map.get(cdb_cyclist_id) or {}
            original_stats = dict(snapshot_stats or base_stats)
            final_stats = dict(original_stats)
            for stat_name, delta_info in (rider_delta_map.get(cdb_cyclist_id) or {}).items():
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
            'rider_link_id': rider_link.get('cyclist_id'),
            'rider_name': rider_name,
            'rider_in_namespace': rider_in_namespace,
            'team_id': rider_link.get('team_id'),
            'team_name': rider_link.get('team_name') or (team.team_name if team is not None else (rider.team_name if rider is not None and rider.team_name else '-')),
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
        stat_conflicts=stat_conflicts,
        stage_status_map=stage_status_map,
        selected_namespace=selected_namespace,
        overall_delta_status=overall_delta_status,
        is_submitted_for_approval=_race_is_submitted_for_approval(selected_namespace, race.race_id),
        stat_fields=STAT_DELTA_FIELDS,
        can_approve_stats=_can_approve_stats(),
        can_update_stats=_can_update_stats(),
        can_download_stats=_can_download_stats(),
    )


@web_bp.route('/namespaces')
@login_required
def namespaces():
    """List the namespaces the current user can select."""
    return render_template('namespaces.html')


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
    g.selected_namespace = selected_namespace
    return redirect(_with_namespace_slug_in_url(next_url, selected_namespace))


@web_bp.route('/stats-editor/races/<int:race_id>/get-result', methods=['POST'])
@login_required
def stats_editor_get_result(race_id):
    """Fetch and persist race results for one race from FirstCycling."""
    if not _can_download_stats():
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

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
        ))

    return redirect(url_for(
        'web.stats_editor_race_detail',
        race_id=race.race_id,
    ))


@web_bp.route('/stats-editor/races/<int:race_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_stat_deltas(race_id):
    """Persist edited stat deltas for this race into the selected namespace database."""
    if not _can_update_stats():
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

    stage_id = request.form.get('stage_id', type=int)
    stage_scope_id = stage_id

    redirect_target = {
        'race_id': race.race_id,
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
        race_result_map = _get_firstcycling_race_result_map(race.race_id, stage_scope_id)
        db.session.commit()
        if race_result_map is None:
            flash('Could not create race result map for this race.', 'error')
            return redirect(url_for('web.stats_editor_race_detail', **redirect_target))
        race_result_map_id = race_result_map.race_result_id

        rider_to_cdb_id = {}
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
            rider_to_cdb_id = {
                row['first_cycling_id']: row['id']
                for row in rider_rows
            }

        saved_count = 0
        skipped_count = 0
        saved_changes = []

        with engine.begin() as conn:
            snapshot_rider_ids = {
                rider_to_cdb_id.get(_safe_int(result.rider_id))
                for result in race.race_results
                if result.rider_id is not None
            }
            snapshot_rider_ids.discard(None)
            if not snapshot_rider_ids:
                snapshot_rider_ids = set(rider_to_cdb_id.values())
            _ensure_scope_stat_snapshots(conn, race_result_id=race_result_map_id, cdb_cyclist_ids=snapshot_rider_ids)
            snapshot_rows = conn.execute(
                text(
                    f"""
                    SELECT cdb_cyclist_id, {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE race_result_id = :race_result_id
                      AND stat_list_id IS NULL
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {'race_result_id': race_result_map_id, 'cdb_cyclist_ids': list(rider_to_cdb_id.values())},
            ).mappings().all()
            snapshot_map = {
                row['cdb_cyclist_id']: _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                rider_id = _safe_int(item.get('first_cycling_rider_id'))
                cdb_cyclist_id = rider_to_cdb_id.get(rider_id)
                stat_name = (item.get('stat_name') or '').strip().lower()
                final_value = _safe_int(item.get('final_value'))
                original_value = _safe_int((snapshot_map.get(cdb_cyclist_id) or {}).get(stat_name))
                is_confilict_override = bool(item.get('is_confilict_override'))

                if (
                    rider_id is None
                    or cdb_cyclist_id is None
                    or stat_name not in STAT_DELTA_FIELDS
                    or original_value is None
                    or final_value is None
                    or final_value < STAT_MIN_VALUE
                    or final_value > STAT_MAX_VALUE
                ):
                    skipped_count += 1
                    continue

                delta_value = final_value - original_value
                saved_changes.append({
                    'cdb_cyclist_id': cdb_cyclist_id, 'stat_name': stat_name,
                    'original_value': original_value, 'final_value': final_value,
                    'is_confilict_override': is_confilict_override,
                })
                if delta_value == 0 and not is_confilict_override:
                    conn.execute(
                        text(
                            """
                            DELETE FROM stat_changes
                            WHERE race_result_id = :race_result_id
                              AND stat_list_id IS NULL
                              AND cdb_cyclist_id = :cdb_cyclist_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'race_result_id': race_result_map_id,
                            'cdb_cyclist_id': cdb_cyclist_id,
                            'stat_name': stat_name,
                        },
                    )
                    continue

                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE race_result_id = :race_result_id
                          AND stat_list_id IS NULL
                          AND cdb_cyclist_id = :cdb_cyclist_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'race_result_id': race_result_map_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            race_result_id,
                            stat_list_id,
                            cdb_cyclist_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value,
                            is_confilict_override,
                            is_approved
                        )
                        VALUES (
                            :race_result_id,
                            NULL,
                            :cdb_cyclist_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            :is_confilict_override,
                            FALSE
                        )
                        """
                    ),
                    {
                        'race_result_id': race_result_map_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                        'is_confilict_override': is_confilict_override,
                    },
                )
                saved_count += 1

        if saved_changes:
            AuditLog.log_action(
                AuditAction.RACE_STATS_SAVED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:race',
                resource_id=race.race_id, resource_name=race.name,
                details={'stage_id': stage_scope_id, 'changes': saved_changes},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()

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


def _race_pending_submission_fingerprint(conn, race_result_ids):
    """Identify the exact saved, unapproved edits being submitted for review."""
    rows = conn.execute(text("""
        SELECT row_to_json(changes)::text
        FROM stat_changes AS changes
        WHERE race_result_id = ANY(:race_result_ids)
          AND stat_list_id IS NULL AND is_approved = FALSE
        ORDER BY id
    """), {'race_result_ids': race_result_ids}).scalars().all()
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest() if rows else None


def _race_submission_matches(namespace, race_id, fingerprint):
    if not fingerprint or namespace is None:
        return False
    submission = AuditLog.query.filter_by(
        action=AuditAction.RACE_STATS_SUBMITTED,
        resource_type=f'namespace:{namespace.id}:race',
        resource_id=race_id,
    ).order_by(AuditLog.id.desc()).first()
    return submission is not None and (submission.details or {}).get('fingerprint') == fingerprint


def _race_is_submitted_for_approval(namespace, race_id):
    if namespace is None:
        return False
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            fingerprint = _race_pending_submission_fingerprint(
                conn, _load_firstcycling_race_result_map_ids(race_id, include_all_race_stages=True),
            )
        return _race_submission_matches(namespace, race_id, fingerprint)
    except Exception:
        logger.exception('Failed to load submission state for race %s', race_id)
        return False
    finally:
        engine.dispose()


@web_bp.route('/stats-editor/races/<int:race_id>/submit-for-approval', methods=['POST'])
@login_required
def stats_editor_submit_for_approval(race_id):
    """Request review of saved overall and stage edits in the selected namespace."""
    if not _can_update_stats():
        abort(403)
    if 'stage_id' in request.form:
        abort(400)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()
    target = url_for('web.stats_editor_race_detail', race_id=race_id)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before submitting stat changes.', 'warning')
        return redirect(target)

    race_result_ids = _load_firstcycling_race_result_map_ids(
        race_id, include_all_race_stages=True,
    )
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            pending_count = conn.execute(text("""
                SELECT COUNT(*) FROM stat_changes
                WHERE race_result_id = ANY(:race_result_ids)
                  AND stat_list_id IS NULL AND is_approved = FALSE
            """), {'race_result_ids': race_result_ids}).scalar_one()
            fingerprint = _race_pending_submission_fingerprint(conn, race_result_ids)
        if not pending_count:
            flash('No pending stat edits to submit.', 'info')
            return redirect(target)
        AuditLog.log_action(
            AuditAction.RACE_STATS_SUBMITTED, current_user,
            resource_type=f'namespace:{namespace.id}:race', resource_id=race_id,
            details={'fingerprint': fingerprint},
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()
        if notify_discord_race_stat_saved(
            namespace, race_id, race.name, race.start_date, None,
            url_for('web.stats_editor_race_detail', race_id=race_id, _external=True),
            pending_count, saved_by=current_user.username,
        ):
            flash('Submitted for approval. Discord approval thread notified.', 'success')
        else:
            flash('Submitted for approval, but the Discord notification was not sent. Check the Discord integration settings.', 'warning')
    except Exception:
        logger.exception('Failed to submit stat edits for race %s', race_id)
        flash('Failed to submit stat edits for approval. Please try again.', 'error')
    finally:
        engine.dispose()
    return redirect(target)


@web_bp.route('/stats-editor/races/<int:race_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_stat_deltas(race_id):
    """Mark saved stat deltas as approved for this race or selected stage scope."""
    if not _can_approve_stats():
        abort(403)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()

    stage_id = request.form.get('stage_id', type=int)
    stage_scope_id = stage_id

    redirect_target = {
        'race_id': race.race_id,
    }
    if stage_id is not None:
        redirect_target['stage_id'] = stage_id

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    result_state = _build_race_result_state(race)
    if result_state['is_stage_race'] and stage_scope_id is not None:
        flash('Stage races can only be approved from the overall race result.', 'warning')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    if result_state['is_stage_race'] and (not result_state['is_completed'] or not result_state['has_overall_result']):
        flash('Stage races can only be approved once the race is completed and the overall result is saved.', 'warning')
        return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    applied_count = 0
    approved_count = 0
    try:
        with engine.begin() as conn:
            if result_state['is_stage_race']:
                race_result_ids = _load_firstcycling_race_result_map_ids(
                    race.race_id,
                    include_all_race_stages=True,
                )
                approval_where_clause = """
                stat_changes.race_result_id = ANY(:race_result_ids)
                AND stat_changes.stat_list_id IS NULL
                """
                approval_params = {'race_result_ids': race_result_ids}
            else:
                race_result_ids = _load_firstcycling_race_result_map_ids(
                    race.race_id,
                    stage_scope_id,
                )
                approval_where_clause = """
                stat_changes.race_result_id = ANY(:race_result_ids)
                AND stat_changes.stat_list_id IS NULL
                """
                approval_params = {'race_result_ids': race_result_ids}

            if not race_result_ids:
                flash('No saved stat changes were found for this race.', 'info')
                return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

            if not _race_submission_matches(
                selected_namespace, race.race_id,
                _race_pending_submission_fingerprint(conn, race_result_ids),
            ):
                flash('Submit the current saved changes for approval before approving this race.', 'warning')
                return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

            conflicts = _load_scope_stat_change_conflicts(conn, approval_where_clause, approval_params)
            if conflicts:
                flash(f'Resolve {len(conflicts)} stat conflict(s) before approving this race.', 'warning')
                return redirect(url_for('web.stats_editor_race_detail', **redirect_target))

            applied_count = _apply_scope_stat_changes_to_cdb(
                conn,
                approval_where_clause.replace('stat_changes.', ''),
                approval_params,
            )
            result = conn.execute(
                text(
                    f"""
                    UPDATE stat_changes
                    SET is_approved = TRUE
                    WHERE {approval_where_clause.replace('stat_changes.', '')}
                      AND is_approved = FALSE
                    """
                ),
                approval_params,
            )
        approved_count = result.rowcount or 0
        if approved_count > 0:
            AuditLog.log_action(
                AuditAction.RACE_STATS_APPROVED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:race',
                resource_id=race.race_id, resource_name=race.name,
                details={
                    'stage_id': stage_scope_id, 'race_result_ids': race_result_ids,
                    'approved_count': approved_count, 'applied_count': applied_count,
                },
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
    except Exception:
        logger.exception('Failed to approve stat deltas for race %s (stage=%s)', race.race_id, stage_scope_id)
        flash('Failed to approve stat changes.', 'error')
    finally:
        engine.dispose()

    if approved_count > 0:
        flash(f'Approved {approved_count} stat edit(s) and applied {applied_count} update(s) to CDB rider stats.', 'success')
        try:
            if notify_discord_race_stat_approved(
                selected_namespace,
                race.race_id,
                stage_scope_id,
                approved_by=current_user.username,
            ):
                flash('Discord approval thread updated.', 'success')
        except Exception:
            logger.exception('Failed to send Discord approval message for race %s', race.race_id)
            flash('Approved stat changes, but Discord approval message failed.', 'warning')
    else:
        flash('No pending stat edit to approve.', 'info')

    return redirect(url_for('web.stats_editor_race_detail', **redirect_target))


# ==================== Admin Panel Routes ====================

@web_bp.route('/settings')
@login_required
def settings():
    """Central directory of administration and configuration options."""
    return render_template(
        'settings.html',
        can_manage_users=_can_access_user_management(),
        can_manage_namespaces=_can_access_namespace_management(),
        provider_access=_integration_provider_access_map(),
    )


@web_bp.route('/admin')
@login_required
@admin_required
def admin():
    """Keep the legacy admin entry point linked to Settings."""
    return redirect(url_for('web.settings'))


@web_bp.route('/admin/users')
@login_required
@user_management_required
def admin_users():
    """Admin panel - Users"""
    users = User.query.order_by(User.created_at.desc()).all()
    roles = Role.query.order_by(Role.display_name.asc()).all()
    return render_template(
        'admin/users.html',
        users=users,
        roles=roles,
        can_create_users=_can_create_users(),
        can_update_users=_can_update_users(),
        active_section='users',
    )


@web_bp.route('/admin/roles/<int:role_id>')
@login_required
def admin_role_detail(role_id):
    """Admin panel - View role permissions and assigned users."""
    if not _can_read_roles():
        abort(403)

    role = Role.query.get_or_404(role_id)
    assigned_users = role.users.order_by(User.username.asc()).all() if hasattr(role, 'users') else []

    permissions_by_resource = {}
    for permission in role.permissions:
        resource = permission.resource
        action = permission.action
        if resource is None or action is None:
            continue

        resource_entry = permissions_by_resource.setdefault(resource.name, {
            'resource': resource,
            'actions': [],
        })
        resource_entry['actions'].append(action)

    permission_groups = sorted(
        permissions_by_resource.values(),
        key=lambda item: item['resource'].name or '',
    )
    for group in permission_groups:
        group['actions'] = sorted(group['actions'], key=lambda action: action.name or '')

    return render_template(
        'admin/role_detail.html',
        role=role,
        assigned_users=assigned_users,
        permission_groups=permission_groups,
        active_section='users',
    )


@web_bp.route('/admin/namespaces')
@login_required
@admin_required
def admin_namespaces():
    """Admin panel - Namespaces"""
    namespaces = Namespace.query.order_by(Namespace.created_at.desc()).all()
    return render_template('admin/namespaces.html', namespaces=namespaces, active_section='namespaces')


@web_bp.route('/admin/integrations')
@login_required
def admin_integrations_index():
    """Redirect integrations index to the default provider view."""
    query_params = request.args.to_dict()
    provider_access = _integration_provider_access_map()
    if provider_access.get('firstcycling'):
        return redirect(url_for('web.admin_integrations', provider='firstcycling', **query_params))
    if provider_access.get('discord'):
        return redirect(url_for('web.admin_integrations', provider='discord', **query_params))
    abort(403)


@web_bp.route('/admin/integrations/<provider>', methods=['GET', 'POST'])
@login_required
def admin_integrations(provider='firstcycling'):
    """Admin panel - integration controls and history."""
    requested_provider = (request.args.get('provider') or '').strip().lower()
    if requested_provider:
        provider = requested_provider

    provider = (provider or 'firstcycling').strip().lower()
    if provider not in INTEGRATION_PROVIDER_LABELS:
        abort(404)
    if not _can_access_integrations_provider(provider):
        abort(403)

    sync_view = (request.args.get('sync_view') or 'all').strip()

    if sync_view not in EXTERNAL_SYNC_TYPE_LABELS:
        abort(404)

    # Ensure this request reads current values written by background workers.
    db.session.expire_all()

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    discord_settings = (
        DiscordSettings.query.filter_by(namespace_id=selected_namespace.id).order_by(DiscordSettings.id.asc()).all()
        if provider == 'discord' and selected_namespace is not None
        else []
    )

    if request.method == 'POST' and provider == 'discord':
        if not _can_update_discord_integrations():
            abort(403)

        if selected_namespace is None:
            flash('Select a namespace before saving Discord settings.', 'warning')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        action = (request.form.get('action', type=str) or 'save').strip().lower()
        settings_id = request.form.get('settings_id', type=int)
        name = (request.form.get('name', type=str) or '').strip() or None
        workflow = (request.form.get('workflow', type=str) or DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS).strip()
        valid_workflows = {value for value, _label in DISCORD_WORKFLOW_CHOICES}
        if workflow not in valid_workflows:
            workflow = DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS
        channel_id = (request.form.get('channel_id', type=str) or '').strip() or None
        approver_role_id = (request.form.get('approver_role_id', type=str) or '').strip() or None

        discord_settings_row = None
        if settings_id is not None:
            discord_settings_row = DiscordSettings.query.filter_by(
                id=settings_id,
                namespace_id=selected_namespace.id,
            ).first()

        if action == 'delete':
            if discord_settings_row is None:
                flash('Discord workflow target could not be found.', 'warning')
            else:
                db.session.execute(
                    text("DELETE FROM discord_workflow_threads WHERE discord_settings_id = :settings_id"),
                    {'settings_id': discord_settings_row.id},
                )
                db.session.delete(discord_settings_row)
                db.session.commit()
                flash('Discord workflow target deleted.', 'success')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        if action == 'test':
            try:
                thread_id = test_discord_channel_permissions(channel_id)
                flash(f'Discord test succeeded. Created test thread {thread_id}.', 'success')
            except Exception as err:
                logger.exception('Discord settings test failed for namespace %s', selected_namespace.slug)
                flash(f'Discord test failed: {err}', 'error')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        if discord_settings_row is None:
            discord_settings_row = DiscordSettings(
                namespace_id=selected_namespace.id,
                created_by_id=current_user.id,
            )
            db.session.add(discord_settings_row)

        discord_settings_row.name = name
        discord_settings_row.workflow = workflow
        discord_settings_row.is_enabled = bool(request.form.get('is_enabled'))
        discord_settings_row.channel_id = channel_id
        discord_settings_row.approver_role_id = approver_role_id
        discord_settings_row.updated_by_id = current_user.id
        db.session.commit()
        flash('Discord workflow target saved.', 'success')
        return redirect(url_for('web.admin_integrations', provider='discord'))

    configured_seasons = _load_config_seasons()
    selected_season = _resolve_external_sync_season(
        configured_seasons,
        request.args.get('year'),
        request.args.get('gender'),
    )

    sync_history = []
    has_running_sync = False
    if provider == 'firstcycling':
        history_query = ExternalSyncLog.query.filter_by(provider=provider)
        if selected_season is not None:
            history_query = history_query.filter_by(
                season_year=selected_season['year'],
                gender=selected_season['gender'],
            )
        if sync_view != 'all':
            history_query = history_query.filter_by(sync_type=sync_view)

        sync_history = history_query.order_by(ExternalSyncLog.timestamp.desc()).limit(200).all()
        for log in sync_history:
            details = None
            if log.details_json:
                try:
                    details = json.loads(log.details_json)
                except (TypeError, ValueError):
                    details = None
            log.details = details or {}
        has_running_sync = any(log.status in ('queued', 'running') for log in sync_history)

    selected_start_date = (request.args.get('start_date') or '').strip()
    selected_end_date = (request.args.get('end_date') or '').strip()

    response = make_response(render_template(
        'admin/integrations.html',
        active_section='integrations',
        active_provider=provider,
        integration_providers=INTEGRATION_PROVIDER_LABELS,
        integration_provider_access=_integration_provider_access_map(),
        active_sync_view=sync_view,
        sync_views=EXTERNAL_SYNC_TYPE_LABELS,
        run_sync_types=EXTERNAL_SYNC_RUN_TYPES,
        can_run_firstcycling_integrations=_can_run_firstcycling_integrations(),
        can_update_discord_integrations=_can_update_discord_integrations(),
        sync_history=sync_history,
        configured_seasons=configured_seasons,
        selected_season=selected_season,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
        sync_type_display=_external_sync_type_display,
        has_running_sync=has_running_sync,
        selected_namespace=selected_namespace,
        discord_settings=discord_settings,
        discord_workflow_choices=DISCORD_WORKFLOW_CHOICES,
        discord_bot_configured=bool(current_app.config.get('DISCORD_BOT_TOKEN')),
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@web_bp.route('/admin/external-sync')
@web_bp.route('/admin/external-sync/<provider>')
@login_required
def admin_external_sync_redirect(provider='firstcycling'):
    """Redirect legacy external sync URLs to integrations URLs."""
    query_params = request.args.to_dict()
    return redirect(url_for('web.admin_integrations', provider=provider, **query_params), code=302)


@web_bp.route('/admin/integrations/run', methods=['POST'])
@web_bp.route('/admin/external-sync/run', methods=['POST'])
@login_required
def admin_integrations_run():
    """Queue one external sync type job and write execution history."""
    provider = (request.form.get('provider') or 'firstcycling').strip().lower()
    if provider != 'firstcycling':
        flash('This integration provider is scaffolded but not runnable yet.', 'info')
        return redirect(url_for('web.admin_integrations', provider=provider))
    if not _can_run_firstcycling_integrations():
        abort(403)

    sync_type = (request.form.get('sync_type') or '').strip()
    if sync_type not in EXTERNAL_SYNC_RUN_TYPES:
        flash('Invalid sync type selected.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view='all'))
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
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

    selected_season = _resolve_external_sync_season(
        seasons,
        request.form.get('year'),
        request.form.get('gender'),
    )
    if selected_season is None:
        flash('No season selected for sync execution.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

    selected_year = selected_season['year']
    selected_gender = selected_season['gender']
    selected_start_date = (request.form.get('start_date') or '').strip()
    selected_end_date = (request.form.get('end_date') or '').strip()

    if sync_type == 'fc_race_results':
        if not selected_start_date or not selected_end_date:
            flash('Start date and end date are required for race results sync.', 'error')
            return redirect(
                url_for(
                    'web.admin_integrations',
                    provider=provider,
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
                    'web.admin_integrations',
                    provider=provider,
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
                    'web.admin_integrations',
                    provider=provider,
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
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

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
                'web.admin_integrations',
                provider=provider,
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
            'web.admin_integrations',
            provider=provider,
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
    
    return render_template('admin/namespace_detail.html',
                         namespace=namespace,
                         active_section='namespaces')


@web_bp.route('/admin/namespaces/create', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_namespace_create():
    """Admin panel - Create new namespace"""
    if not _can_create_namespaces():
        abort(403)

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
    if not _can_update_namespaces():
        abort(403)

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
@user_management_required
def admin_user_create():
    """Admin panel - Create new user"""
    if not _can_create_users():
        abort(403)

    form = UserCreateForm()
    
    if form.validate_on_submit():
        # Check if username already exists
        existing = User.query.filter_by(username=form.username.data).first()
        if existing:
            flash('A user with this username already exists', 'error')
            return render_template('admin/user_form.html', form=form, active_section='users')
        
        # Pending users set their own password from a one-time invite link.
        user = User(
            username=form.username.data,
            is_active=False,
            is_temporary_password=False,
        )
        db.session.add(user)
        raw_token, _ = _issue_user_token(user, TOKEN_INVITE, _invite_duration(), issued_by=current_user)
        AuditLog.log_action(
            action=AuditAction.USER_INVITE_CREATED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()

        invite_url = url_for('web.activate_user', token=raw_token, _external=True)
        return render_template(
            'admin/user_invite_created.html',
            user=user,
            invite_url=invite_url,
            active_section='users',
        )
    
    return render_template('admin/user_form.html', form=form, active_section='users')


@web_bp.route('/admin/user/<int:user_id>')
@login_required
@user_management_required
def admin_user_detail(user_id):
    """Admin panel - View and manage user details"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    
    # Get all roles for assignment
    all_roles = Role.query.order_by(Role.display_name).all()

    # Get user's current roles
    user_roles = user.roles.all() if hasattr(user, 'roles') else []
    
    return render_template('admin/user_detail.html', 
                         user=user, 
                         all_roles=all_roles,
                         user_roles=user_roles,
                         active_section='users')


@web_bp.route('/admin/user/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@user_management_required
def admin_user_toggle_status(user_id):
    """Admin panel - Toggle user active status"""
    if not _can_update_users():
        abort(403)

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
@user_management_required
def admin_user_reset_password(user_id):
    """Admin panel - Issue a one-time user password reset link."""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    if user.is_superadmin:
        abort(403)

    form = AdminPasswordResetForm()
    reset_url = None

    if form.validate_on_submit():
        raw_token, _ = _issue_user_token(user, TOKEN_PASSWORD_RESET, _reset_duration(), issued_by=current_user)
        AuditLog.log_action(
            action=AuditAction.USER_PASSWORD_RESET,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'reset_by_admin': True, 'issued_reset_link': True},
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        db.session.commit()
        reset_url = url_for('web.reset_password_from_token', token=raw_token, _external=True)

    return render_template(
        'admin/user_reset_password.html',
        form=form,
        user=user,
        reset_url=reset_url,
        active_section='users',
    )


@web_bp.route('/admin/user/<int:user_id>/discord-id', methods=['POST'])
@login_required
@user_management_required
def admin_user_update_discord_id(user_id):
    """Admin panel - update a user's Discord identity binding."""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    discord_user_id = (request.form.get('discord_user_id') or '').strip() or None

    if discord_user_id and not re.fullmatch(r'\d{17,32}', discord_user_id):
        flash('Discord user ID must be a numeric Discord snowflake.', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))

    previous_discord_user_id = user.discord_user_id
    user.discord_user_id = discord_user_id

    AuditLog.log_action(
        action=AuditAction.USER_DISCORD_LINKED if discord_user_id else AuditAction.USER_DISCORD_UNLINKED,
        user=current_user,
        resource_type='user',
        resource_id=user.id,
        resource_name=user.username,
        details={
            'previous_discord_user_id': previous_discord_user_id,
            'discord_user_id': discord_user_id,
            'updated_by_admin': True,
        },
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent'),
    )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('That Discord user ID is already linked to another user.', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))

    flash('Discord user ID updated.', 'success')
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/add-role', methods=['POST'])
@login_required
@user_management_required
def admin_user_add_role(user_id):
    """Admin panel - Add role to user"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    if user.is_superadmin:
        abort(403)

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
@user_management_required
def admin_user_remove_role(user_id, role_id):
    """Admin panel - Remove role from user"""
    if not _can_update_users():
        abort(403)

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

        policy_error = validate_password_policy(form.new_password.data, current_user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
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


def _register_namespace_scoped_routes():
    """Register /n/<slug>/ aliases for routes whose content depends on selected namespace."""
    namespace_routes = [
        ('/dashboard', 'dashboard', dashboard, ['GET']),
        ('/teams', 'teams', teams, ['GET', 'POST']),
        ('/teams/<int:team_id>', 'team_detail', team_detail, ['GET']),
        ('/riders', 'riders', riders, ['GET', 'POST']),
        ('/riders/<int:rider_id>', 'rider_detail', rider_detail, ['GET']),
        ('/stats-editor', 'stats_editor', stats_editor, ['GET', 'POST']),
        ('/stats-editor/lists/create', 'stats_editor_create_list', stats_editor_create_list, ['POST']),
        ('/stats-editor/lists/<int:list_id>/add-rider', 'stats_editor_add_list_rider', stats_editor_add_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>/toggle-rider', 'stats_editor_toggle_list_rider', stats_editor_toggle_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>', 'stats_editor_list_detail', stats_editor_list_detail, ['GET']),
        ('/stats-editor/lists/<int:list_id>/save-stat-deltas', 'stats_editor_save_list_stat_deltas', stats_editor_save_list_stat_deltas, ['POST']),
        ('/stats-editor/lists/<int:list_id>/approve-stat-deltas', 'stats_editor_approve_list_stat_deltas', stats_editor_approve_list_stat_deltas, ['POST']),
        ('/stats-editor/races/<int:race_id>', 'stats_editor_race_detail', stats_editor_race_detail, ['GET']),
        ('/stats-editor/races/<int:race_id>/<int:stage_id>', 'stats_editor_race_detail', stats_editor_race_detail, ['GET']),
        ('/stats-editor/races/<int:race_id>/get-result', 'stats_editor_get_result', stats_editor_get_result, ['POST']),
        ('/stats-editor/races/<int:race_id>/save-stat-deltas', 'stats_editor_save_stat_deltas', stats_editor_save_stat_deltas, ['POST']),
        ('/stats-editor/races/<int:race_id>/approve-stat-deltas', 'stats_editor_approve_stat_deltas', stats_editor_approve_stat_deltas, ['POST']),
        ('/releases', 'releases', releases, ['GET', 'POST']),
        ('/releases/<int:release_id>/files/<int:file_id>/download', 'release_file_download', release_file_download, ['GET']),
        ('/namespace/select', 'select_namespace', select_namespace, ['POST']),
        ('/admin/integrations', 'admin_integrations_index', admin_integrations_index, ['GET']),
        ('/admin/integrations/<provider>', 'admin_integrations', admin_integrations, ['GET', 'POST']),
        ('/admin/external-sync', 'admin_external_sync_redirect', admin_external_sync_redirect, ['GET']),
        ('/admin/external-sync/<provider>', 'admin_external_sync_redirect', admin_external_sync_redirect, ['GET']),
        ('/admin/integrations/run', 'admin_integrations_run', admin_integrations_run, ['POST']),
        ('/admin/external-sync/run', 'admin_integrations_run', admin_integrations_run, ['POST']),
    ]

    for rule, endpoint, view_func, methods in namespace_routes:
        web_bp.add_url_rule(
            f'/n/<namespace_slug>{rule}',
            endpoint=endpoint,
            view_func=view_func,
            methods=methods,
        )


_register_namespace_scoped_routes()
