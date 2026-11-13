"""Web routes for UI"""
import hashlib
import html
import logging
import json
import os
import re
import csv
import io
import shutil
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import render_template, redirect, url_for, flash, request, abort, current_app, session, make_response, send_file, g, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine, text, Integer, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload
from werkzeug.exceptions import HTTPException, NotFound

from webapp.web import web_bp
from webapp.web.race_catalog import unified_races, filter_races
from webapp.web.rider_views import ACTIVITY_FILTERS, ACTIVITY_CONDITIONS, RIDER_VIEWS, IDENTITY_FILTERS, IDENTITY_REVIEW_FILTERS, DEFAULT_IDENTITY_REVIEW_FLAGS, activity_sql, identity_flag_condition, filter_snapshot
from webapp.web.forms import (
    AdminPasswordResetForm,
    LoginForm,
    NamespaceForm,
    NamespaceMetadataForm,
    PasswordChangeForm,
    PasswordSetupForm,
    UserCreateForm,
)
from webapp.models import User, UserSecurityToken, Resource, Namespace, Role, DiscordSettings, DISCORD_WORKFLOW_CHOICES, AuditLog, AuditAction, FirstCyclingRace, RaceResultMap
from webapp.models import Action, ExternalCallTask, ExternalCallTaskGroup, RolePermission, ScheduledJob
from webapp.integrations.identity import MODELS as IDENTITY_MODELS, save_identity, publish_namespace_pairs
from webapp.integrations.cdb import apply_updates_to_sqlite as _apply_release_updates_to_sqlite
from webapp.integrations.cdb.converter import convert_sqlite_to_cdb as _convert_release_sqlite_to_cdb
from webapp.integrations.discord import (
    notify_discord_race_stat_approved,
    notify_discord_race_stat_saved,
    notify_discord_release_review_requested,
    notify_discord_stat_list_approved,
    notify_discord_stat_list_saved,
    test_discord_channel_permissions,
)
from webapp.integrations.firstcycling.api import FirstCyclingClient, FirstCyclingAPIError
from webapp.models.main.firstcycling import FirstCyclingRider, FirstCyclingTeam
from webapp.models.main.firstcycling import FirstCyclingRaceResult, FirstCyclingRaceStage
from webapp.models.main.pcs import PcsRider
from webapp.integrations.cdb import get_field_mapping
from webapp.integrations.cdb.editability import get_editable_fields
from webapp.integrations.cdb import attribute_changes, field_configuration
from webapp.models.ns.field_configuration import FieldConfigurationAudit
from webapp.integrations.cdb.rider_state import pcs_dob_identity_status, refresh_pending_flags
from webapp.integrations.rider_checks import (
    ACTIVITY_CHECK, IDENTITY_CHECK, FC_RIDER_SYNC, PCS_RIDER_SYNC, PROFILE_ENDPOINT,
    RIDER_CHECK_JOBS, rider_database, scheduled_rider, scheduled_rider_choices,
)
from webapp.integrations.pcs.slug import request_rider_slug, valid_rider_slug
from webapp.extensions import db
from webapp.models.main.user import TOKEN_INVITE, TOKEN_PASSWORD_RESET
from webapp.security import validate_password_policy
from webapp.worker import create_manual_external_group, run_external_sync_task, run_rider_check_now
from webapp.worker import run_activity_monitor_run_task
from webapp.worker import enqueue_group, replace_draft_requests, submit_external_group, cancel_external_group
from webapp.integrations.activity_monitor_queue import (
    enqueue_activity_monitor_run,
    request_activity_monitor_run_stop,
    resume_activity_monitor_run,
)
from webapp.integrations.activity_monitor_planning import (
    count_due_activity_riders,
    due_activity_riders,
)
from webapp.tools.activity_monitor_schedule import next_planned_round, planned_rounds
from webapp.web.wiki_content import find_wiki_page, find_wiki_section, load_wiki_page_content, load_wiki_sections
from functools import wraps


def _activity_check_status_label(value):
    key = str(value or '').strip().casefold().replace(' ', '_')
    labels = {
        'success': 'Checked',
        'results_parsed': 'Checked',
        'results_table_empty': 'Checked',
        'missing_link': 'No profile link saved',
        'profile_not_found': 'Profile page not found',
        'http_error': 'Site could not be reached',
        'network_error': 'Site could not be reached',
        'invalid_response': 'Page could not be read',
        'identity_not_verified': 'Rider could not be confirmed',
        'identity_mismatch': 'Rider details differ',
        'mismatch': 'Rider details differ',
        'ambiguous': 'Could not confirm rider',
        'not_checked': 'Not checked',
        'unknown': 'Not available',
    }
    return labels.get(key, 'Needs review')


def _activity_check_history_label(value):
    key = str(value or '').strip().casefold().replace(' ', '_')
    labels = {
        'candidate': 'Saved, not checked yet',
        'cleared': 'No longer selected',
        'manual_review': 'Needs a closer look',
        'needs_review': 'Needs a closer look',
        'partial': 'The site may not show every race',
        'complete': 'The site showed its available race list',
        'unverified': 'Could not confirm',
        'verified': 'Rider matched',
        'verified_from_imported_results': 'Matched to saved results',
        'mismatch': 'Rider details differ',
        'ambiguous': 'Could not confirm rider',
        'not_checked': 'Not confirmed',
        'unknown': 'Not available',
    }
    return labels.get(key, 'Not available')


def _activity_pcs_slug_color(profile, latest_check, selected_path, worlddb_birth_date):
    """Choose the PCS slug color from the current link and its latest PCS check."""
    def normalized_path(value):
        raw = str(value or '').strip()
        parsed = urllib.parse.urlsplit(raw if '://' in raw else '')
        path = parsed.path if parsed.scheme else raw
        path = '/' + path.lstrip('/') if path else ''
        path = path.rstrip('/').casefold()
        if path.startswith('/rider/') and not path.endswith('/road'):
            path = f'{path}/road'
        return path

    selected = normalized_path(selected_path)
    if not selected:
        return 'default'

    latest_path = normalized_path((latest_check or {}).get('request_path'))
    latest_matches = bool(latest_path and latest_path == selected)
    latest_result = (latest_check or {}).get('identity_result')
    latest_status = (latest_check or {}).get('source_status')
    verified_statuses = {'success', 'results_parsed', 'results_table_empty'}
    unconfirmed_results = {
        'mismatch', 'ambiguous', 'unverified', 'unknown', 'unavailable',
        'manual_review', 'needs_review',
    }

    approved_path = normalized_path(profile.get('approved_profile_path'))
    approved_pair_matches = bool(
        approved_path == selected
        and profile.get('approved_at')
        and profile.get('approved_pcs_birth_date')
        and profile.get('approved_worlddb_birth_date') == worlddb_birth_date
        and profile.get('profile_birth_date') == profile.get('approved_pcs_birth_date')
    )
    approved_birthdate_exception = bool(
        approved_pair_matches
        and profile.get('approved_pcs_birth_date') != worlddb_birth_date
    )

    if latest_matches and latest_result == 'verified' and latest_status in verified_statuses:
        return 'green'
    if (approved_birthdate_exception and latest_matches and latest_result == 'mismatch'
            and 'birthdate mismatch' not in str((latest_check or {}).get('message') or '').casefold()):
        return 'red'
    if approved_birthdate_exception:
        return 'orange'
    if latest_matches and (
        latest_result in unconfirmed_results
        or latest_status in {
            'identity_not_verified', 'identity_mismatch', 'profile_not_found',
            'missing_link', 'http_error', 'network_error', 'invalid_response',
        }
    ):
        return 'red'

    profile_checked_path = normalized_path(profile.get('checked_url'))
    if profile_checked_path == selected:
        if profile.get('check_result') == 'verified':
            return 'green'
        if profile.get('check_result') in {
            'identity_mismatch', 'mismatch', 'rider_details_dont_match',
            'ambiguous', 'unavailable', 'identity_not_verified',
        }:
            return 'red'

    if profile.get('link_source') == 'manual_override':
        return 'orange'
    return 'default'


def _activity_check_explanation(status, history=None, identity=None):
    status_key = str(status or '').strip().casefold().replace(' ', '_')
    history_key = str(history or '').strip().casefold().replace(' ', '_')
    identity_key = str(identity or '').strip().casefold().replace(' ', '_')
    if status_key == 'success' and history_key == 'partial':
        return 'The check worked, but this site may not show every race. Missing results do not prove the rider was inactive.'
    if status_key == 'success' and identity_key not in {'verified', 'verified_from_imported_results'}:
        return 'The site replied, but we could not confirm that this is the correct rider.'
    return {
        'success': 'The check worked and the race information available on the site was saved.',
        'missing_link': 'No profile link is saved for this rider.',
        'profile_not_found': 'The saved profile page was not found. Check that the link is correct.',
        'http_error': 'The site could not be reached. This may be temporary.',
        'network_error': 'The site could not be reached. This may be temporary.',
        'invalid_response': 'The site replied, but its page could not be read.',
        'identity_not_verified': 'The site replied, but the rider could not be confirmed.',
        'identity_mismatch': 'The details on the profile do not match this rider.',
        'mismatch': 'The details on the profile do not match this rider.',
        'ambiguous': 'We could not tell whether this profile belongs to the rider.',
    }.get(status_key, 'No further details are available.')


def _activity_datetime_label(value):
    if not value:
        return '—'
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    else:
        raw = str(value).strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return raw
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return re.sub(r'(\d{2}:\d{2}:\d{2})\.\d+', r'\1', raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('UTC'))
    return parsed.astimezone(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')

@web_bp.app_template_filter('activity_check_status')
def activity_check_status_filter(value):
    return _activity_check_status_label(value)


@web_bp.app_template_filter('activity_check_history')
def activity_check_history_filter(value):
    return _activity_check_history_label(value)


@web_bp.app_template_filter('activity_check_explanation')
def activity_check_explanation_filter(value, history=None, identity=None):
    return _activity_check_explanation(value, history, identity)


@web_bp.app_template_filter('activity_datetime')
def activity_datetime_filter(value):
    return _activity_datetime_label(value)


_NAME_SEARCH_TRANSLATE_FROM = 'àáâãäåāăąǎçćĉċčďđèéêëēĕėęěĝğġģìíîïĩīĭįıĵķĺļľłñńņňòóôõöōŏőøŕŗřśŝşšţťŧùúûüũūŭůűųŵýÿŷźżžð'
_NAME_SEARCH_TRANSLATE_TO = ''.join(
    'a' if ch in 'àáâãäåāăąǎ' else
    'c' if ch in 'çćĉċč' else
    'd' if ch in 'ďđ' else
    'e' if ch in 'èéêëēĕėęě' else
    'g' if ch in 'ĝğġģ' else
    'i' if ch in 'ìíîïĩīĭįı' else
    'j' if ch == 'ĵ' else
    'k' if ch == 'ķ' else
    'l' if ch in 'ĺļľł' else
    'n' if ch in 'ñńņň' else
    'o' if ch in 'òóôõöōŏőø' else
    'r' if ch in 'ŕŗř' else
    's' if ch in 'śŝşš' else
    't' if ch in 'ţťŧ' else
    'u' if ch in 'ùúûüũūŭůűų' else
    'w' if ch == 'ŵ' else
    'y' if ch in 'ýÿŷ' else
    'z' if ch in 'źżž' else
    'd'
    for ch in _NAME_SEARCH_TRANSLATE_FROM
)


def _normalize_name_search_term(value):
    normalized = unicodedata.normalize('NFKD', (value or '').casefold())
    normalized = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    return (normalized.replace('æ', 'ae').replace('œ', 'oe')
            .replace('ß', 'ss').replace('þ', 'th').replace('ð', 'd')
            .replace('ø', 'o').replace('ł', 'l').replace('đ', 'd')
            .replace('ı', 'i'))


def _activity_monitor_country_sql():
    """Display the namespace CDB's country name, including historical snapshots."""
    return """COALESCE(
        (SELECT COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''))
         FROM rider cdb_rider
         JOIN region ON region.id = cdb_rider.region_id
         JOIN country ON country.id = region.country_id
         WHERE cdb_rider.id = r.source_worlddb_rider_id AND r.present_in_cdb),
        (SELECT COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''))
         FROM country
         WHERE UPPER(country.constant) = UPPER(COALESCE(NULLIF(r.nationality_code, ''), r.nationality))
            OR LOWER(country.flag_name) = LOWER(r.nationality)
         ORDER BY country.id LIMIT 1),
        NULLIF(r.nationality, ''), NULLIF(r.nationality_code, ''), '—'
    )"""


def _activity_monitor_attention_sql(rider_alias='r'):
    """Shared review rule for the monitor overview and rider roster."""
    def source_family(check_alias):
        normalized_source = f"lower(replace(COALESCE({check_alias}.source, ''), '_', ' '))"
        return f"""CASE
            WHEN {normalized_source} IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api') THEN 'firstcycling'
            WHEN {normalized_source} IN ('pcs', 'pcs html', 'procyclingstats') THEN 'pcs'
            ELSE {normalized_source}
        END"""

    pcs_success_for_fc_issue = """EXISTS (
        SELECT 1 FROM activity_monitor_source_checks pcs_success
        WHERE pcs_success.rider_id = c.rider_id
          AND pcs_success.identity_generation = c.identity_generation
          AND lower(replace(COALESCE(pcs_success.source, ''), '_', ' '))
              IN ('pcs', 'pcs html', 'procyclingstats')
          AND lower(replace(COALESCE(pcs_success.source_status, ''), '_', ' '))
              IN ('success', 'results parsed', 'results table empty')
          AND lower(replace(COALESCE(pcs_success.identity_result, ''), '_', ' ')) = 'verified'
          AND (
              pcs_success.run_id = c.run_id
              OR pcs_success.checked_at > c.checked_at
          )
    )"""

    return f"""({rider_alias}.identity_hold OR EXISTS (
        SELECT 1 FROM activity_monitor_identity_alerts a
        WHERE a.rider_id = {rider_alias}.id AND a.status = 'open'
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_profile_links p
        WHERE p.rider_id = {rider_alias}.id
          AND p.identity_generation = {rider_alias}.identity_generation
          AND p.is_current = TRUE
          AND (
              lower(replace(COALESCE(p.check_result, ''), '_', ' '))
                  IN ('mismatch', 'identity mismatch', 'rider details don t match', 'profile not found')
              OR p.link_status IN ('mismatch', 'unavailable', 'needs_review', 'manual_review', 'conflict')
          )
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_source_checks c
        WHERE c.rider_id = {rider_alias}.id
          AND c.identity_generation = {rider_alias}.identity_generation
          AND lower(replace(COALESCE(c.identity_result, ''), '_', ' '))
              IN ('mismatch', 'identity mismatch', 'needs review', 'manual review', 'conflict',
                  'ambiguous', 'unavailable', 'unknown', 'not available', 'not verified', 'identity not verified')
          AND NOT ({pcs_success_for_fc_issue} AND {source_family('c')} = 'firstcycling')
          AND NOT EXISTS (
              SELECT 1 FROM activity_monitor_source_checks newer
              WHERE newer.rider_id = c.rider_id
                AND newer.identity_generation = c.identity_generation
                AND {source_family('newer')} = {source_family('c')}
                AND (newer.checked_at, newer.id) > (c.checked_at, c.id)
          )
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_source_checks c
        WHERE c.rider_id = {rider_alias}.id
          AND c.identity_generation = {rider_alias}.identity_generation
          AND lower(replace(COALESCE(c.source_status, ''), '_', ' '))
              IN ('profile not found', 'missing link', 'http error', 'network error', 'invalid response',
                  'identity not verified', 'ambiguous')
          AND NOT ({pcs_success_for_fc_issue} AND {source_family('c')} = 'firstcycling')
          AND NOT EXISTS (
              SELECT 1 FROM activity_monitor_source_checks newer
              WHERE newer.rider_id = c.rider_id
                AND newer.identity_generation = c.identity_generation
                AND {source_family('newer')} = {source_family('c')}
                AND (newer.checked_at, newer.id) > (c.checked_at, c.id)
          )
    ))"""


def _assumed_pcs_profile(first_name, last_name):
    """Build an Arie-tool-style PCS candidate without treating it as verified."""
    raw = f"{first_name or ''} {last_name or ''}"
    raw = ''.join('-' if char in "'’‘`ʼʻ＇´" else char
                  for char in raw if unicodedata.category(char) != 'Cf')
    value = unicodedata.normalize('NFKD', raw)
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().translate(str.maketrans({
        'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'æ': 'ae',
        'œ': 'oe', 'ħ': 'h', 'ı': 'i', 'ŋ': 'ng', 'ŧ': 't', 'ƶ': 'z',
        'ƒ': 'f', 'ĸ': 'k',
    }))
    slug = re.sub(r'[^a-z0-9-]+', '-', value)
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        return '', None
    return slug, f'https://www.procyclingstats.com/rider/{slug}/road'


def _normalized_name_sql(expression):
    normalized = f"lower(COALESCE({expression}, ''))"
    for source, target in (('æ', 'ae'), ('œ', 'oe'), ('ß', 'ss'), ('þ', 'th')):
        normalized = f"replace({normalized}, '{source}', '{target}')"
    return f"translate({normalized}, '{_NAME_SEARCH_TRANSLATE_FROM}', '{_NAME_SEARCH_TRANSLATE_TO}')"


def _normalized_name_expr(expression):
    """SQLAlchemy equivalent of _normalized_name_sql for ORM query expressions."""
    normalized = func.lower(func.coalesce(expression, ''))
    for source, target in (('æ', 'ae'), ('œ', 'oe'), ('ß', 'ss'), ('þ', 'th')):
        normalized = func.replace(normalized, source, target)
    return func.translate(normalized, _NAME_SEARCH_TRANSLATE_FROM, _NAME_SEARCH_TRANSLATE_TO)

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


def _stat_field_settings(conn, *, lock=False):
    return field_configuration.stored_fields(conn, 'rider', lock=lock)


def _editable_stat_fields(settings):
    editable = get_editable_fields('rider')
    return {column for column in STAT_COLUMN_MAP.values()
            if settings.get(('rider', column), {}).get('is_editable', column in editable)}


def _require_editable_stat_changes(settings, changes):
    editable = _editable_stat_fields(settings)
    for item in changes:
        if not isinstance(item, dict):
            continue
        stat = item.get('stat_name')
        if isinstance(stat, str) and stat.strip().lower() in STAT_COLUMN_MAP:
            if STAT_COLUMN_MAP[stat.strip().lower()] not in editable:
                abort(403)


def _stat_applies_immediately(settings, stat):
    return not settings.get(('rider', STAT_COLUMN_MAP[stat]), {}).get('is_require_approval', True)


def _require_unapplied_stat(conn, scope, params, rider_id, stat):
    # Immediately applied edits remain available to releases.
    conn.execute(text('SELECT id FROM rider WHERE id = :id FOR UPDATE'), {'id': rider_id}).all()
    if conn.execute(text(f'''
        SELECT 1 FROM stat_changes WHERE {scope} AND rider_id = :field_rider_id
        AND stat_name = :field_stat AND is_approved = TRUE AND release_id IS NULL
        AND (to_jsonb(stat_changes)->>'is_applied_immediately') = 'true' LIMIT 1
    '''), {**params, 'field_rider_id': rider_id, 'field_stat': stat}).first():
        abort(409, description='Applied stat edits are read-only in this scope.')


def _apply_immediate_stats(conn, change_ids):
    if not change_ids:
        return
    params = {'ids': change_ids}
    conn.execute(text('''
        SELECT id FROM rider WHERE id IN (SELECT rider_id FROM stat_changes WHERE id = ANY(:ids))
        ORDER BY id FOR UPDATE
    '''), params).all()
    if _load_scope_stat_change_conflicts(conn, 'stat_changes.id = ANY(:ids)', params):
        abort(409, description='Resolve stat conflicts before saving these changes.')
    _apply_scope_stat_changes_to_cdb(conn, 'id = ANY(:ids)', params)
    conn.execute(text('''
        UPDATE stat_changes SET is_approved = TRUE, is_applied_immediately = TRUE WHERE id = ANY(:ids)
    '''), params)


@web_bp.app_context_processor
def inject_cdb_editability():
    return {'editable_rider_fields': get_editable_fields('rider')}


EXTERNAL_SYNC_TYPE_LABELS = {
    'all': 'All Syncs',
    'fc_race_calendar': 'Race Calendar',
    'fc_teams': 'Teams',
    'fc_riders': 'Riders',
    'fc_race_results': 'Race Results',
    PCS_RIDER_SYNC: 'PCS Rider',
    FC_RIDER_SYNC: 'FC Rider',
    **RIDER_CHECK_JOBS,
}

EXTERNAL_SYNC_RUN_TYPES = ('fc_race_calendar', 'fc_teams', 'fc_riders', 'fc_race_results')
RIDER_BULK_TASK_PROVIDERS = {
    FC_RIDER_SYNC: ('firstcycling',),
    PCS_RIDER_SYNC: ('pcs',),
    IDENTITY_CHECK: ('pcs',),
    ACTIVITY_CHECK: ('pcs', 'firstcycling'),
}
MAX_BULK_TASK_RIDERS = 250
SCHEDULED_JOB_TYPES = {'race_results': 'Race Results', **RIDER_CHECK_JOBS}


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
    scheduled_job_id=None,
):
    details_json = None
    if details is not None:
        details_json = json.dumps(details)

    row = ExternalCallTaskGroup(
        provider=provider,
        workflow_type=sync_type,
        season_year=season_year,
        gender=gender,
        status=status,
        requested_count=requested_count,
        synced_count=synced_count,
        failed_count=failed_count,
        executed_by_id=executed_by_id,
        scheduled_job_id=scheduled_job_id,
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


def _build_namespace_slug(name, gender):
    """Build a stable identity independent of the current season/game."""
    return '-'.join(filter(None, (
        _slugify_namespace_part(name), _slugify_namespace_part(gender),
    )))

    
def _resolve_external_sync_season(seasons, year_value, gender_value):
    """Resolve selected season from params, falling back to configured default."""
    return _find_external_sync_season(seasons, year_value, gender_value) or _resolve_default_season(seasons)


def _find_external_sync_season(seasons, year_value, gender_value):
    selected_year = _safe_int(year_value)
    selected_gender = (gender_value or '').strip()
    return next((season for season in seasons
                 if season['year'] == selected_year and season['gender'] == selected_gender), None)


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
    'activity_monitor': {
        'display_name': 'Activity Monitor',
        'description': 'Review rider activity in the selected CDB',
        'endpoint': 'web.activity_monitor',
        'resources': ['activity-monitor'],
        'required_permissions': [('activity-monitor', 'read')],
    },
    'stat_editor': {
        'display_name': 'Races',
        'description': 'Maintain rider stats from race results',
        'endpoint': 'web.races',
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
    'rider_manager': {
        'display_name': 'Riders',
        'description': 'Browse riders and maintain rider stats',
        'endpoint': 'web.riders',
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
    return current_user.has_permissions(('stats', 'approve'), ('rider:stat', 'approve'))


def _can_create_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'create')


def _can_update_stats():
    return current_user.has_permissions(('stats', 'update'), ('rider:stat', 'update'))


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


def _can_read_external_call_groups():
    return current_user.has_permission('integrations-external-calls', 'read')


def _can_check_rider_identity():
    return (_can_access_panel('team_manager')
            and current_user.has_permission('integrations-external-calls', 'download'))


def _valid_pcs_rider_slug(slug):
    return valid_rider_slug(slug)


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


def _can_transfer_activity_monitor_history():
    """Require namespace write authority for moving cross-database history."""
    return current_user.is_superadmin or any(
        current_user.has_permission('namespaces', action)
        for action in ('create', 'update', 'delete')
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
    panel_order = {'team_manager': 0, 'rider_manager': 1, 'stat_editor': 2, 'release_manager': 3}
    for panel_name in sorted(panel_order, key=panel_order.get):
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
                    FROM rider
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


def _load_namespace_scope_snapshots(namespace, race_result_ids=None, stat_list_id=None, rider_ids=None):
    """Load rider stat snapshots keyed by CDB cyclist id for one approval scope."""
    if namespace is None or not rider_ids:
        return {}

    normalized_race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    normalized_cdb_ids = [
        rider_id
        for rider_id in {_safe_int(rider_id) for rider_id in rider_ids}
        if rider_id is not None
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
                        rider_id,
                        {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE {race_filter}
                      AND stat_list_id IS NOT DISTINCT FROM :list_id
                      AND rider_id = ANY(:rider_ids)
                    """
                ),
                {
                    'race_result_ids': normalized_race_result_ids,
                    'list_id': stat_list_id,
                    'rider_ids': normalized_cdb_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat snapshots from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    return {
        row['rider_id']: _snapshot_stats_from_row(row)
        for row in rows
        if row.get('rider_id') is not None
    }


def _ensure_scope_stat_snapshots(conn, race_result_id=None, stat_list_id=None, rider_ids=None):
    """Capture current CDB stats for riders in a scope that has no snapshot yet."""
    snapshot_entire_roster = rider_ids is None
    normalized_cdb_ids = []
    if not snapshot_entire_roster:
        normalized_cdb_ids = [
            rider_id
            for rider_id in {_safe_int(rider_id) for rider_id in (rider_ids or [])}
            if rider_id is not None
        ]

    if not snapshot_entire_roster and not normalized_cdb_ids:
        return 0

    result = conn.execute(
        text(
            f"""
            INSERT INTO stat_change_snapshot (
                race_result_id,
                stat_list_id,
                rider_id,
                {', '.join(STAT_COLUMN_MAP.values())},
                created_at
            )
            SELECT
                :race_result_id,
                :list_id,
                c.id,
                {_snapshot_stat_select_list('c')},
                NOW()
            FROM rider c
            WHERE (:snapshot_entire_roster OR c.id = ANY(:rider_ids))
              AND NOT EXISTS (
                  SELECT 1
                  FROM stat_change_snapshot s
                  WHERE s.race_result_id IS NOT DISTINCT FROM :race_result_id
                    AND s.stat_list_id IS NOT DISTINCT FROM :list_id
                    AND s.rider_id = c.id
              )
            """
        ),
        {
            'race_result_id': race_result_id,
            'list_id': stat_list_id,
            'snapshot_entire_roster': snapshot_entire_roster,
            'rider_ids': normalized_cdb_ids,
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
                    FROM rider
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
                    FROM rider c
                    LEFT JOIN team t ON t.id = c.team_id
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
        country_id=row.get('country_id'),
        division_id=row.get('division_id'),
        short_name=row.get('short_name'),
        abbreviation=row.get('abbreviation') or row.get('uci_code'),
        nation_name=row.get('nation_name'),
        division=row.get('division'),
        uci_code=row.get('uci_code'),
        ranking_rank=row.get('ranking_rank'),
    )


def _namespace_roster_rider_from_row(row):
    rider_id = row.get('cyclist_id')
    birth_date = row.get('birth_date')
    pcs_birth_date = row.get('pcs_birth_date')
    pcs_status = row.get('pcs_identity_status') or 'unknown'
    if pcs_status in ('verified', 'review'):
        pcs_status = pcs_dob_identity_status(birth_date, pcs_birth_date)

    return SimpleNamespace(
        cyclist_id=row.get('cyclist_id'),
        rider_id=rider_id,
        first_cycling_id=row.get('first_cycling_id'),
        pcs_identity_status=pcs_status,
        pcs_birth_date=pcs_birth_date,
        pcs_identity_verified_override=row.get('pcs_identity_verified_override', False),
        pcs_slug=row.get('pcs_slug'),
        is_deleted=row.get('is_deleted', False),
        has_pending_attribute_changes=row.get('has_pending_attribute_changes', False),
        name=_cdb_rider_display_name(row),
        first_name=row.get('first_name'),
        last_name=row.get('last_name'),
        birth_date=birth_date,
        birth_date_display=_format_date_for_display(birth_date),
        country_id=row.get('country_id'),
        division_id=row.get('division_id'),
        search_name=row.get('search_name'),
        search_team=row.get('search_team'),
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
        monitor_id=row.get('monitor_id'),
        activity_available=row.get('activity_available'),
        latest_check_at=row.get('latest_check_at'),
        latest_check_status=row.get('latest_check_status'),
        latest_result_date=row.get('latest_result_date'),
        latest_race_name=row.get('latest_race_name'),
        days_since_last_race=(date.today() - row['latest_result_date']).days if row.get('latest_result_date') else None,
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


def _load_firstcycling_rider_results(first_cycling_id):
    if not first_cycling_id:
        return []
    result_date = func.coalesce(
        FirstCyclingRaceStage.stage_date, FirstCyclingRace.end_date, FirstCyclingRace.start_date
    )
    rows = (
        FirstCyclingRaceResult.query
        .join(FirstCyclingRaceResult.race)
        .outerjoin(FirstCyclingRaceResult.stage)
        .add_columns(FirstCyclingRace, FirstCyclingRaceStage, result_date)
        .filter(FirstCyclingRaceResult.rider_id == first_cycling_id)
        .order_by(FirstCyclingRace.season_year.desc(), result_date.desc().nulls_last(),
                  FirstCyclingRaceStage.stage_number.desc().nulls_last(), FirstCyclingRaceResult.id.desc())
        .all()
    )
    races = {}
    for result, race, stage, result_date in rows:
        entry = races.setdefault(race.id, {
            'race': race,
            'result': result,
            'date': race.end_date or race.start_date or result_date,
            'stages': [],
        })
        if stage is None:
            entry['result'] = result
        else:
            entry['stages'].append({'stage': stage, 'result': result, 'date': result_date})
    for entry in races.values():
        entry['stages'].sort(key=lambda row: (
            row['stage'].stage_number is None, row['stage'].stage_number or 0,
            row['stage'].stage_date or date.min, row['stage'].id,
        ))
    return list(races.values())


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
                """ + _normalized_name_sql('t.team_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{_normalize_name_search_term(team_query)}%'

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
                    FROM team t
                    LEFT JOIN division division ON division.id = t.division_id
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
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), t.country_id::text) AS label
                    FROM team t
                    LEFT JOIN country country ON country.id = t.country_id
                    WHERE t.country_id IS NOT NULL
                    ORDER BY COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), t.country_id::text) ASC
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
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), t.country_id::text) AS nation_name,
                        COALESCE(division.constant, t.division_id::text) AS division,
                        t.abbreviation AS uci_code,
                        NULL AS ranking_rank,
                        COUNT(c.id) AS rider_count
                    FROM team t
                    LEFT JOIN rider c ON c.team_id = t.id
                    LEFT JOIN country country ON country.id = t.country_id
                    LEFT JOIN division division ON division.id = t.division_id
                    {where_sql}
                    GROUP BY
                        t.id,
                        t.short_name,
                        t.team_name,
                        t.abbreviation,
                        t.country_id,
                        t.division_id,
                        country.constant,
                        country.flag_name,
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


def _load_namespace_roster_riders(namespace, filters, page=1, per_page=100, *, snapshot=False, include_activity=False):
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
    view = filters.get('view', 'stats')
    activity_view = include_activity if snapshot else view == 'activity'
    if filters.get('identity_review'):
        conditions = []
        for index, flag in enumerate(filters.get('identity_flags', DEFAULT_IDENTITY_REVIEW_FLAGS)):
            if flag not in IDENTITY_REVIEW_FILTERS:
                raise ValueError('Invalid identity review flag')
            key = f'identity_flag_{index}'
            conditions.append(identity_flag_condition(flag).replace(':identity_flag', ':' + key))
            params[key] = flag
        where_clauses.append('(' + ' OR '.join(conditions) + ')' if conditions else 'FALSE')
        where_clauses.append("NOT COALESCE((to_jsonb(c)->>'is_deleted')::boolean, FALSE)")
    if view == 'identity' and filters.get('identity_flag') in IDENTITY_FILTERS and filters['identity_flag']:
        where_clauses.append(identity_flag_condition(filters['identity_flag']))
        params['identity_flag'] = filters['identity_flag']
    if view == 'stats' and filters.get('stat_key') in STAT_COLUMN_MAP:
        column = STAT_COLUMN_MAP[filters['stat_key']]
        for bound, operator in (('stat_min', '>='), ('stat_max', '<=')):
            if filters.get(bound) not in (None, ''):
                where_clauses.append(f'c.{column} {operator} :{bound}')
                params[bound] = int(filters[bound])
    if activity_view and filters.get('activity_status') in ACTIVITY_CONDITIONS:
        where_clauses.append(ACTIVITY_CONDITIONS[filters['activity_status']])

    if rider_query:
        where_clauses.append(
            "(" + _normalized_name_sql('c.first_last_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql('c.first_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql('c.last_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql("CONCAT_WS(' ', c.first_name, c.last_name)") + " LIKE :rider_query"
            + " OR c.id::text = :rider_query_exact"
            + " OR c.first_cycling_id::text = :rider_query_exact)"
        )
        params['rider_query'] = f'%{_normalize_name_search_term(rider_query)}%'
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
                """ + _normalized_name_sql('t.team_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{_normalize_name_search_term(team_query)}%'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            activity_join = activity_sql(conn) if activity_view else ''
            activity_columns = 'activity.*,' if activity_view else ''
            activity_order = 'activity.latest_result_date DESC NULLS LAST,' if activity_view and not snapshot else ''
            country_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        country.id::text AS value,
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) AS label
                    FROM rider c
                    LEFT JOIN region region ON region.id = c.region_id
                    LEFT JOIN country country ON country.id = region.country_id
                    WHERE country.id IS NOT NULL
                    ORDER BY COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) ASC
                    """
                )
            ).mappings().all()
            division_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.division_id::text AS value,
                        COALESCE(division.constant, t.division_id::text) AS label
                    FROM rider c
                    LEFT JOIN team t ON t.id = c.team_id
                    LEFT JOIN division division ON division.id = t.division_id
                    WHERE t.division_id IS NOT NULL
                    ORDER BY COALESCE(division.constant, t.division_id::text) ASC
                    """
                )
            ).mappings().all()
            total = None if snapshot else conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM rider c
                    {activity_join}
                    LEFT JOIN team t ON t.id = c.team_id
                    LEFT JOIN region region ON region.id = c.region_id
                    {where_sql}
                    """
                ),
                params,
            ).scalar() or 0
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        {activity_columns}
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        to_jsonb(c)->>'pcs_slug' AS pcs_slug,
                        to_jsonb(c)->>'pcs_identity_status' AS pcs_identity_status,
                        (to_jsonb(c)->>'pcs_birth_date')::date AS pcs_birth_date,
                        COALESCE((to_jsonb(c)->>'pcs_identity_verified_override')::boolean, FALSE) AS pcs_identity_verified_override,
                        COALESCE((to_jsonb(c)->>'pcs_slug_is_assumed')::boolean, FALSE) AS pcs_slug_is_assumed,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        region.country_id,
                        t.division_id,
                        CONCAT_WS(' ', c.first_last_name, c.first_name, c.last_name,
                                  CONCAT_WS(' ', c.first_name, c.last_name)) AS search_name,
                        CONCAT_WS(' ', t.team_name, t.short_name, t.abbreviation) AS search_team,
                        c.size_cm,
                        c.weight_kg,
                        COALESCE((to_jsonb(c)->>'is_deleted')::boolean, FALSE) AS is_deleted,
                        COALESCE((to_jsonb(c)->>'has_pending_attribute_changes')::boolean, FALSE) AS has_pending_attribute_changes,
                        COALESCE(t.team_name, t.short_name) AS team_name,
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM rider c
                    {activity_join}
                    LEFT JOIN team t ON t.id = c.team_id
                    LEFT JOIN region region ON region.id = c.region_id
                    LEFT JOIN country country ON country.id = region.country_id
                    {where_sql}
                    ORDER BY
                        {activity_order}
                        c.last_name ASC NULLS LAST,
                        c.first_name ASC NULLS LAST,
                        c.first_last_name ASC NULLS LAST,
                        c.first_cycling_id ASC NULLS LAST,
                        c.id ASC
                    {'' if snapshot else 'LIMIT :limit OFFSET :offset'}
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
        if snapshot:
            abort(503, description='Could not load riders. Please try again.')
        return [], 0, [], []
    finally:
        engine.dispose()

    country_options = [{'value': row['value'], 'label': row['label']} for row in country_rows]
    division_options = [{'value': row['value'], 'label': row['label']} for row in division_rows]
    riders = [_namespace_roster_rider_from_row(row) for row in rows]
    if (snapshot or view == 'identity') and riders:
        model = IDENTITY_MODELS['rider']
        fc_ids = {rider.first_cycling_id for rider in riders if rider.first_cycling_id}
        slugs = {row['pcs_slug'] for row in rows if row['pcs_slug'] and not row['pcs_slug_is_assumed']}
        identities = model.query.filter(or_(model.firstcycling_id.in_(fc_ids), model.pcs_slug.in_(slugs))).all()
        by_fc = {identity.firstcycling_id: identity for identity in identities if identity.firstcycling_id}
        by_pcs = {identity.pcs_slug: identity for identity in identities if identity.pcs_slug}
        for rider, row in zip(riders, rows):
            fc_identity = by_fc.get(rider.first_cycling_id)
            pcs_identity = by_pcs.get(rider.pcs_slug) if not row['pcs_slug_is_assumed'] else None
            identity = fc_identity or pcs_identity
            rider.mapping_conflict = bool(identity and (
                (fc_identity and pcs_identity and fc_identity.id != pcs_identity.id)
                or (rider.first_cycling_id and identity.firstcycling_id and rider.first_cycling_id != identity.firstcycling_id)
                or (rider.pcs_slug and not row['pcs_slug_is_assumed'] and identity.pcs_slug and rider.pcs_slug != identity.pcs_slug)
            ))
            rider.identity_id = identity.id if identity and not rider.mapping_conflict else None
    return riders, len(riders) if snapshot else int(total), country_options, division_options


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
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), team.country_id::text) AS nation_name,
                        COALESCE(division.constant, team.division_id::text) AS division,
                        team.abbreviation AS uci_code,
                        NULL AS ranking_rank
                    FROM team team
                    LEFT JOIN country country ON country.id = team.country_id
                    LEFT JOIN division division ON division.id = team.division_id
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
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM rider c
                    LEFT JOIN team t ON t.id = c.team_id
                    LEFT JOIN region region ON region.id = c.region_id
                    LEFT JOIN country country ON country.id = region.country_id
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
            _ensure_namespace_database_connection(conn, namespace)
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        to_jsonb(c) AS debug_columns,
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        to_jsonb(c)->>'pcs_identity_status' AS pcs_identity_status,
                        (to_jsonb(c)->>'pcs_birth_date')::date AS pcs_birth_date,
                        c.pcs_identity_verified_override,
                        c.pcs_slug,
                        c.is_deleted,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        t.team_name,
                        COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM rider c
                    LEFT JOIN team t ON t.id = c.team_id
                    LEFT JOIN region region ON region.id = c.region_id
                    LEFT JOIN country country ON country.id = region.country_id
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

    if row is None:
        return None
    rider = _namespace_roster_rider_from_row(row)
    rider.debug_columns = row['debug_columns']
    return rider


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
        stage_label = (
            stage.stage_name or f'Stage #{stage.stage_number or stage_id}'
            if stage is not None else f'Stage #{stage_id}'
        )
        label = f'{race_name} - {stage_label}'
        url = url_for('web.race_detail', race_id=race_id, stage_id=stage_id) if race_id is not None else None
    else:
        label = race_name
        url = url_for('web.race_detail', race_id=race_id) if race_id is not None else None

    return {
        'type': 'Race Result',
        'label': label,
        'url': url,
    }


def _load_namespace_rider_stat_edits(namespace, rider_id=None, limit=None, offset=0, approved=None):
    """Load newest edits for a rider, or a page of the namespace-wide stat log."""
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            rows = conn.execute(
                text(
                    """
                    SELECT
                        sc.id,
                        sc.rider_id,
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
                        COALESCE(NULLIF(c.first_last_name, ''), CONCAT_WS(' ', c.first_name, c.last_name)) AS rider_name,
                        rel.version AS release_version,
                        rel.release_name
                    FROM stat_changes sc
                    LEFT JOIN rider_list sl ON sl.id = sc.stat_list_id
                    LEFT JOIN rider c ON c.id = sc.rider_id
                    LEFT JOIN releases rel ON rel.id = sc.release_id
                    WHERE (CAST(:rider_id AS INTEGER) IS NULL OR sc.rider_id = :rider_id)
                      AND (CAST(:approved AS BOOLEAN) IS NULL OR COALESCE(sc.is_approved, FALSE) = :approved)
                    ORDER BY sc.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {'rider_id': rider_id, 'limit': limit, 'offset': offset, 'approved': approved},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat edits for cyclist %s from namespace %s', rider_id, namespace.slug)
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
        row_rider_id = row['rider_id']
        if row.get('stat_list_id') is None and row.get('race_result_id') is None:
            source = {
                'type': 'Individual',
                'label': row.get('rider_name') or f'Rider #{row_rider_id}',
                'url': url_for('web.rider_detail', rider_id=row_rider_id),
            }
        elif row.get('stat_list_id') is not None:
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
            'rider_name': row.get('rider_name') or f'Rider #{row_rider_id}',
            'rider_url': url_for('web.rider_detail', rider_id=row_rider_id),
            'approve_url': url_for('web.rider_stats_action', rider_id=row_rider_id, action='approve')
            if row.get('race_result_id') is None and row.get('stat_list_id') is None else None,
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
                    FROM rider
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
    rider_ids,
):
    """Load saved stat delta rows keyed by CDB cyclist and stat name for one race scope."""
    race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    rider_ids = [
        rider_id
        for rider_id in {_safe_int(rider_id) for rider_id in (rider_ids or [])}
        if rider_id is not None
    ]
    if namespace is None or not race_result_ids or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        rider_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value,
                        COALESCE((to_jsonb(stat_changes)->>'is_applied_immediately')::boolean, FALSE)
                            AS is_applied_immediately
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                      AND rider_id = ANY(:rider_ids)
                    ORDER BY id ASC
                    """
                ),
                {
                    'race_result_ids': race_result_ids,
                    'rider_ids': rider_ids,
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
        rider_id = row['rider_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
            'is_applied_immediately': row['is_applied_immediately'],
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
    log_mode=False,
    limit=None,
    offset=0,
):
    """Return release candidates, or paginated edit groups of every status for the log."""
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            if log_mode:
                _ensure_namespace_database_connection(conn, namespace)
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
            if log_mode:
                release_filter = 'TRUE'
            having_sql = '' if log_mode else 'HAVING BOOL_AND(COALESCE(is_approved, FALSE))'
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(race_result_id, 0) AS race_result_id,
                        COALESCE(stat_list_id, 0) AS stat_list_id,
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END AS individual_rider_id,
                        {selected_rows_sql} AS selected_rows,
                        COUNT(*) AS total_edits,
                        COUNT(DISTINCT rider_id) AS riders_edited,
                        BOOL_AND(COALESCE(is_approved, FALSE)) AS is_approved,
                        BOOL_AND(release_id IS NOT NULL) AS is_released,
                        ARRAY_AGG(DISTINCT release_id) FILTER (WHERE release_id IS NOT NULL) AS release_ids,
                        MAX(id) AS latest_edit_id
                    FROM stat_changes
                    WHERE {release_filter}
                      AND (race_result_id IS NULL OR stat_list_id IS NULL)
                    GROUP BY
                        COALESCE(race_result_id, 0),
                        COALESCE(stat_list_id, 0),
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END
                    {having_sql}
                    ORDER BY MAX(id) DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {'selected_release_id': release_scope_id,
                 'limit': None if log_mode else limit, 'offset': 0 if log_mode else offset},
            ).mappings().all()

            release_names = {}
            if log_mode:
                release_ids = {release_id for row in rows for release_id in (row.get('release_ids') or [])}
                if release_ids:
                    release_names = {
                        release['id']: ' '.join(str(value) for value in (release['release_name'], release['version']) if value)
                        for release in conn.execute(text('SELECT id, release_name, version FROM releases WHERE id = ANY(:ids)'),
                                                    {'ids': list(release_ids)}).mappings()
                    }
            list_ids = {
                int(row['stat_list_id'])
                for row in rows
                if (row.get('stat_list_id') or 0) > 0
            }
            list_name_map = {}
            if list_ids:
                list_query = """
                    SELECT id, list_name, season_year, gender, created_at
                    FROM rider_list
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

    approval_events = AuditLog.query.filter(
        AuditLog.resource_type.in_([
            f'namespace:{namespace.id}:race',
            f'namespace:{namespace.id}:stat_list',
            f'namespace:{namespace.id}:individual',
        ]),
        AuditLog.action.in_([
            AuditAction.RACE_STATS_SUBMITTED, AuditAction.RACE_STATS_APPROVED,
            AuditAction.STAT_LIST_SUBMITTED, 'individual_stats_submitted', 'individual_stats_approved',
        ]),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).all()

    items = []
    for row in rows:
        mapped_result = race_result_map.get(int(row.get('race_result_id') or 0))
        race_id = (_safe_int(mapped_result.external_race_id) if mapped_result is not None else 0) or 0
        stage_id = (_safe_int(mapped_result.external_stage_id) if mapped_result is not None else 0) or 0
        list_id = int(row.get('stat_list_id') or 0)

        individual_rider_id = int(row.get('individual_rider_id') or 0)
        race_obj = None
        if individual_rider_id:
            rider = _load_namespace_roster_rider(namespace, individual_rider_id)
            if rider is None and not log_mode:
                continue
            scope_type = 'individual'
            display_name = rider.name if rider is not None else f'Rider #{individual_rider_id}'
            item_date = '-'
            category = '-'
            classification_label = '-'
            detail_url = url_for('web.rider_detail', rider_id=individual_rider_id)
        elif list_id > 0:
            list_meta = list_name_map.get(list_id)
            if not list_meta and log_mode:
                list_meta = {'list_name': f'List #{list_id}'}
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
            if (race_obj is None or stage_obj is None) and not log_mode:
                continue
            scope_type = 'stage'
            source = _race_result_source_info(mapped_result, race_map, stage_map)
            display_name = source['label']
            item_date = stage_obj.stage_date if stage_obj is not None else (race_obj.start_date if race_obj is not None else '-')
            category = race_obj.category if race_obj is not None else '-'
            classification_label = race_obj.classification_label if race_obj is not None else '-'
            detail_url = url_for('web.race_detail', race_id=race_id, stage_id=stage_id)
        else:
            race_obj = race_map.get(race_id)
            if race_obj is None and not log_mode:
                continue
            scope_type = 'race'
            display_name = race_obj.name if race_obj is not None and race_obj.name else f'Race #{race_id}'
            item_date = race_obj.start_date if race_obj is not None else '-'
            category = race_obj.category if race_obj is not None else '-'
            classification_label = race_obj.classification_label if race_obj is not None else '-'
            detail_url = url_for('web.race_detail', race_id=race_id)

        if not list_id and not individual_rider_id and mapped_result is None and log_mode:
            display_name = f"Race Result #{row['race_result_id']}"
            detail_url = None

        resource_kind = 'individual' if individual_rider_id else ('stat_list' if list_id else 'race')
        resource_id = individual_rider_id or list_id or race_id
        approval_dates = {}
        for event in approval_events:
            if event.resource_type != f'namespace:{namespace.id}:{resource_kind}' or event.resource_id != resource_id:
                continue
            if event.action == AuditAction.RACE_STATS_APPROVED:
                event_stage_id = (event.details or {}).get('stage_id')
                if event_stage_id and int(event_stage_id) != stage_id:
                    continue
            date_key = 'approval_requested_on' if event.action.endswith('_submitted') else 'approved_on'
            approval_dates.setdefault(date_key, event.created_at)

        items.append({
            'item_key': _encode_release_item_key(scope_type, race_id, stage_id, individual_rider_id or list_id),
            'scope_type': scope_type,
            'type_label': {'list': 'List', 'individual': 'Individual'}.get(scope_type, 'Race Result'),
            'is_selected': (int(row.get('selected_rows') or 0) > 0) if selected_release_id is not None else False,
            'date': item_date,
            'approved_on': approval_dates.get('approved_on'),
            'approval_requested_on': approval_dates.get('approval_requested_on'),
            'name': display_name,
            'detail_url': detail_url,
            'category': category,
            'classification': race_obj.classification if race_obj is not None else '-',
            'classification_label': classification_label,
            'riders_edited': int(row.get('riders_edited') or 0),
            'total_edits': int(row.get('total_edits') or 0),
            'is_approved': bool(row.get('is_approved')),
            'is_released': bool(row.get('is_released')),
            'latest_edit_id': row.get('latest_edit_id') or 0,
            'releases': [{'name': release_names.get(release_id) or f'Release #{release_id}',
                          'url': url_for('web.releases', release_id=release_id, tab='stats')}
                         for release_id in sorted(row.get('release_ids') or [])] if log_mode else [],
        })

    if log_mode:
        items.sort(key=lambda item: (item['approved_on'] or datetime.min, item['latest_edit_id']), reverse=True)
        return items[offset:offset + limit if limit is not None else None]
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
            'individuals_included': 0,
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
                            COALESCE(stat_list_id, 0) AS stat_list_id,
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END AS individual_rider_id
                        FROM stat_changes
                        WHERE {release_filter}
                          AND (race_result_id IS NULL OR stat_list_id IS NULL)
                        GROUP BY
                            COALESCE(race_result_id, 0),
                            COALESCE(stat_list_id, 0),
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END
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
                        COUNT(DISTINCT sc.rider_id) FILTER (
                            WHERE sc.stat_list_id IS NULL AND sc.race_result_id IS NULL
                        ) AS individuals_included,
                        COUNT(DISTINCT sc.rider_id) AS riders_with_edits,
                        COUNT(*) AS total_edits
                    FROM stat_changes sc
                    JOIN approved_scopes approved
                      ON COALESCE(sc.race_result_id, 0) = approved.race_result_id
                     AND COALESCE(sc.stat_list_id, 0) = approved.stat_list_id
                     AND (approved.individual_rider_id = 0 OR sc.rider_id = approved.individual_rider_id)
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
            'individuals_included': 0,
            'riders_with_edits': 0,
            'total_edits': 0,
        }
    finally:
        engine.dispose()

    return {
        'race_results_included': int((row or {}).get('race_results_included') or 0),
        'stat_lists_included': int((row or {}).get('stat_lists_included') or 0),
        'individuals_included': int((row or {}).get('individuals_included') or 0),
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
                                COALESCE(stat_list_id, 0) AS stat_list_id,
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END AS individual_rider_id
                            FROM stat_changes
                            WHERE COALESCE(release_id, 0) = 0
                              AND (race_result_id IS NULL OR stat_list_id IS NULL)
                            GROUP BY
                                COALESCE(race_result_id, 0),
                                COALESCE(stat_list_id, 0),
                        CASE WHEN race_result_id IS NULL AND stat_list_id IS NULL THEN rider_id ELSE 0 END
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
                                  AND (approved.individual_rider_id = 0 OR stat_changes.rider_id = approved.individual_rider_id)
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
        'cdb': 'CDB',
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
    field_mapping = get_field_mapping(pcm_version, 'rider')
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
                        rider_id,
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
        rider_id = _safe_int(row.get('rider_id'))
        raw_stat_name = (row.get('stat_name') or '').strip().lower()
        final_value = _safe_int(row.get('final_value'))

        if rider_id is None or not raw_stat_name or final_value is None:
            continue

        stat_code = raw_stat_name[5:] if raw_stat_name.startswith('stat_') else raw_stat_name
        if stat_code not in STAT_DELTA_FIELDS:
            continue

        source_column = stat_column_by_code.get(stat_code)
        if not source_column:
            continue

        updates[(rider_id, source_column)] = final_value

    return {
        'table_name': cyclist_table_name,
        'id_column': cyclist_id_column,
        'updates': updates,
    }


def _load_release_pcs_slug_updates(namespace, sqlite_path):
    """Return exportable PCS slugs keyed by unique FirstCycling-to-CDB IDs."""
    if namespace is None or not sqlite_path:
        return {}

    pcm_version = namespace.pcm_version or 'PCM2025'
    cyclist_mapping = get_field_mapping(pcm_version, 'rider')
    cyclist_fields = cyclist_mapping.get('fields') or {}
    export_fields = cyclist_mapping.get('export_fields') or {}
    cdb_table = cyclist_mapping.get('cdb_table_name')
    cdb_id_column = cyclist_fields.get('id')
    first_cycling_column = cyclist_fields.get('first_cycling_id')
    constant_column = export_fields.get('pcs_profile_slug')
    if not all((cdb_table, cdb_id_column, first_cycling_column, constant_column)):
        raise ValueError(f'Missing PCS slug export mapping for PCM version {pcm_version}')

    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            tables_ready = conn.execute(text("""
                SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                   AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                   AND to_regclass('activity_monitor_source_checks') IS NOT NULL
            """)).scalar_one()
            if not tables_ready:
                return {}

            rider_fc_counts = {
                _safe_int(row['first_cycling_id']): int(row['rider_count'])
                for row in conn.execute(text("""
                    SELECT first_cycling_id, COUNT(*) AS rider_count
                    FROM activity_monitor_riders
                    WHERE first_cycling_id IS NOT NULL AND present_in_cdb
                    GROUP BY first_cycling_id
                """)).mappings().all()
            }
            rows = conn.execute(text("""
                SELECT r.first_cycling_id, r.birth_date,
                       p.profile_path, p.link_source, p.link_status,
                       p.check_result, p.checked_url, p.approved_profile_path,
                       p.approved_pcs_birth_date, p.approved_worlddb_birth_date,
                       p.approved_at, p.profile_birth_date,
                       latest.request_path, latest.source_status,
                       latest.identity_result, latest.message
                FROM activity_monitor_riders r
                JOIN activity_monitor_profile_links p
                  ON p.rider_id = r.id
                 AND p.identity_generation = r.identity_generation
                 AND p.is_current = TRUE
                LEFT JOIN LATERAL (
                    SELECT c.request_path, c.source_status, c.identity_result, c.message
                    FROM activity_monitor_source_checks c
                    WHERE c.rider_id = r.id
                      AND c.identity_generation = r.identity_generation
                      AND LOWER(TRIM(COALESCE(c.source, ''))) IN
                          ('pcs', 'pcs_html', 'procyclingstats')
                    ORDER BY c.checked_at DESC, c.id DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE r.present_in_cdb AND r.first_cycling_id IS NOT NULL
            """)).mappings().all()
    finally:
        engine.dispose()

    exportable_by_fc = {}
    for row in rows:
        first_cycling_id = _safe_int(row.get('first_cycling_id'))
        if first_cycling_id is None or rider_fc_counts.get(first_cycling_id) != 1:
            continue
        profile = dict(row)
        selected_path = (
            row.get('approved_profile_path')
            or row.get('profile_path')
        )
        latest_check = {
            'request_path': row.get('request_path'),
            'source_status': row.get('source_status'),
            'identity_result': row.get('identity_result'),
            'message': row.get('message'),
        }
        slug_color = _activity_pcs_slug_color(
            profile, latest_check, selected_path, row.get('birth_date')
        )
        if slug_color not in {'green', 'orange'}:
            continue

        parsed = urllib.parse.urlsplit(
            selected_path if '://' in str(selected_path or '')
            else f'https://www.procyclingstats.com/{str(selected_path or "").lstrip("/")}'
        )
        if parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            continue
        slug_match = re.fullmatch(
            r'/rider/([A-Za-z0-9_-]+)(?:/road)?/?', parsed.path, re.IGNORECASE
        )
        if slug_match:
            exportable_by_fc[first_cycling_id] = slug_match.group(1)

    if not exportable_by_fc:
        return {}

    def quote_identifier(value):
        return '"' + str(value).replace('"', '""') + '"'

    sqlite_uri = f'{Path(sqlite_path).resolve().as_uri()}?mode=ro'
    connection = sqlite3.connect(sqlite_uri, uri=True)
    try:
        cdb_rows = connection.execute(
            f'SELECT {quote_identifier(cdb_id_column)}, '
            f'CAST({quote_identifier(first_cycling_column)} AS INTEGER) '
            f'FROM {quote_identifier(cdb_table)} '
            f'WHERE {quote_identifier(first_cycling_column)} IS NOT NULL'
        ).fetchall()
    finally:
        connection.close()

    cdb_ids_by_fc = {}
    for cdb_id, first_cycling_id in cdb_rows:
        normalized_fc_id = _safe_int(first_cycling_id)
        if normalized_fc_id is not None:
            cdb_ids_by_fc.setdefault(normalized_fc_id, []).append(_safe_int(cdb_id))

    updates = {}
    for first_cycling_id, slug in exportable_by_fc.items():
        matching_cdb_ids = cdb_ids_by_fc.get(first_cycling_id, [])
        if len(matching_cdb_ids) == 1 and matching_cdb_ids[0] is not None:
            updates[(matching_cdb_ids[0], constant_column)] = slug
    return updates


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
                    FROM rider
                    ORDER BY id ASC
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    pcm_version = namespace.pcm_version or 'PCM2025'
    cyclist_fields = (get_field_mapping(pcm_version, 'rider').get('fields') or {})
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
    """Create a packed PCM CDB from the previous baseline and assigned changes."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    baseline_path = _resolve_release_base_sqlite_path(namespace, release)
    if not baseline_path:
        raise ValueError(
            'No baseline SQLite database found. Upload a namespace CDB or create an earlier release with a SQLite file first.'
        )

    from webapp.config import ensure_namespace_directories, get_cdb_releases_path

    ensure_namespace_directories(namespace.slug)

    output_filename = f'{artifact_basename}.cdb'
    working_filename = f'{artifact_basename}.sqlite'
    output_dir = get_cdb_releases_path(namespace.slug)
    output_abs_path = os.path.join(output_dir, output_filename)
    working_abs_path = os.path.join(output_dir, working_filename)

    shutil.copy2(baseline_path, working_abs_path)

    try:
        updates_payload = _load_release_stat_updates(namespace, release.id)
        pcs_slug_updates = _load_release_pcs_slug_updates(namespace, baseline_path)
        all_updates = dict(updates_payload['updates'])
        all_updates.update(pcs_slug_updates)
        applied_changes = _apply_release_updates_to_sqlite(
            working_abs_path,
            updates_payload['table_name'],
            updates_payload['id_column'],
            all_updates,
        )
        _convert_release_sqlite_to_cdb(working_abs_path, output_abs_path)
    except Exception:
        if os.path.exists(output_abs_path):
            os.remove(output_abs_path)
        raise
    finally:
        if os.path.exists(working_abs_path):
            os.remove(working_abs_path)

    md5_checksum, sha256_checksum = _compute_file_hashes(output_abs_path)
    relative_path = f'releases/{output_filename}'

    return {
        'display_name': output_filename,
        'relative_path': relative_path,
        'content_type': 'application/octet-stream',
        'file_size_bytes': os.path.getsize(output_abs_path),
        'md5_checksum': md5_checksum,
        'sha256_checksum': sha256_checksum,
        'applied_changes': applied_changes,
        'pcs_slug_changes': len(pcs_slug_updates),
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
        approved_on = item.get('approved_on')
        approval_requested_on = item.get('approval_requested_on')
        item_rows.append(
            '<tr>'
            f'<td>{html.escape(item.get("type_label") or "-")}</td>'
            f'<td>{approved_on.strftime("%Y-%m-%d %H:%M:%S") if approved_on else "-"}</td>'
            f'<td>{approval_requested_on.strftime("%Y-%m-%d %H:%M:%S") if approval_requested_on else "-"}</td>'
            f'<td>{html.escape(item.get("name") or "-")}</td>'
            f'<td>{int(item.get("riders_edited") or 0)}</td>'
            f'<td>{int(item.get("total_edits") or 0)}</td>'
            f'<td>{html.escape(item.get("classification") or "-")}</td>'
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
      <tr><th>Type</th><th>Approved On</th><th>Approval Requested On</th><th>Name</th><th>Rider Edits</th><th>Total Edits</th><th>Classification</th></tr>
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
    sqlite_file['file_type'] = 'cdb'
    sqlite_file['file_type_label'] = 'CDB'

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
                stat_changes.rider_id,
                stat_changes.stat_name,
                stat_changes.original_value,
                stat_changes.final_value,
                COALESCE(stat_changes.is_confilict_override, FALSE) AS is_confilict_override,
                c.first_name,
                c.last_name,
                c.first_cycling_id,
                {_snapshot_stat_select_list('c')}
            FROM stat_changes
            JOIN rider c ON c.id = stat_changes.rider_id
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
            ).strip() or f"Rider #{row.get('rider_id')}"
            conflicts.append({
                'stat_change_id': row.get('id'),
                'race_result_id': row.get('race_result_id'),
                'stat_list_id': row.get('stat_list_id'),
                'first_cycling_rider_id': row.get('first_cycling_id') or row.get('rider_id'),
                'rider_id': row.get('rider_id'),
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
    """Apply pending stat_changes.final_value into rider for one scope."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT id, rider_id, stat_name, final_value
            FROM stat_changes
            WHERE {where_clause}
              AND is_approved = FALSE
            ORDER BY id ASC
            """
        ),
        params,
    ).mappings().all()

    applied_count = 0
    editable = _editable_stat_fields(_stat_field_settings(conn, lock=True))
    for row in pending_rows:
        rider_id = _safe_int(row.get('rider_id'))
        final_value = _safe_int(row.get('final_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = STAT_COLUMN_MAP.get(stat_code)

        if rider_id is None or final_value is None or not column_name:
            continue

        if column_name not in editable:
            abort(403)

        conn.execute(
            text(
                f'''
                UPDATE rider
                SET "{column_name}" = :final_value
                WHERE id = :rider_id
                '''
            ),
            {
                'final_value': final_value,
                'rider_id': rider_id,
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
                          AND table_name = 'rider_list'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                return []

            selected_columns = ['id', 'list_name']
            if 'list_type' in stat_list_columns:
                selected_columns.append('list_type')
            if 'season_year' in stat_list_columns:
                selected_columns.append('season_year')
            if 'gender' in stat_list_columns:
                selected_columns.append('gender')
            if 'created_at' in stat_list_columns:
                selected_columns.append('created_at')
            if 'created_by_id' in stat_list_columns:
                selected_columns.append('created_by_id')

            query = """
                SELECT {selected_columns}
                FROM rider_list
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
                        SELECT rider_list_id, COUNT(*) AS riders_count
                        FROM rider_list_item
                        WHERE rider_list_id = ANY(:list_ids)
                        GROUP BY rider_list_id
                        """
                    ),
                    {'list_ids': list_ids},
                ).mappings().all()

                rider_count_map = {int(r['rider_list_id']): int(r['riders_count'] or 0) for r in rider_counts}

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


def _load_namespace_list_result_deltas(namespace, stat_list_id, rider_ids):
    """Load saved stat changes for one ad-hoc stat list keyed by CDB cyclist/stat."""
    rider_ids = [
        rider_id
        for rider_id in {_safe_int(rider_id) for rider_id in (rider_ids or [])}
        if rider_id is not None
    ]
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
                        rider_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value,
                        COALESCE((to_jsonb(stat_changes)->>'is_applied_immediately')::boolean, FALSE)
                            AS is_applied_immediately
                    FROM stat_changes
                    WHERE stat_list_id = :list_id
                      AND race_result_id IS NULL
                      AND rider_id = ANY(:rider_ids)
                    """
                ),
                {
                    'list_id': stat_list_id,
                    'rider_ids': rider_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load list stat changes from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['rider_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
            'is_applied_immediately': row['is_applied_immediately'],
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
    try:
        selected_namespace = _get_selected_namespace(available_namespaces)
    except NotFound:
        # Error templates also run context processors. Preserve the original
        # 404 without selecting another namespace or raising during rendering.
        selected_namespace = None
        panel_menu_items = []
    else:
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
                    'url': url_for('web.race_detail', race_id=race.race_id),
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
        'activity_monitor': '🕒',
        'stat_editor': '📊',
        'team_manager': '👥',
        'release_manager': '📦',
    }
    return icons.get(panel_name, '📁')


@web_bp.route('/activity-monitor')
@login_required
def activity_monitor():
    """Read-only Activity Monitor overview for the selected CDB namespace."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider_count = None
    monitor_summary = None
    load_error = False
    history_unavailable = False
    source_history_unavailable = False

    if selected_namespace is not None:
        engine = None
        try:
            overview_today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                rider_count = conn.execute(
                    text('SELECT COUNT(*) FROM rider')
                ).scalar_one()
                monitor_tables_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_riders') IS NOT NULL")
                ).scalar_one()
                if monitor_tables_ready:
                    upcoming_cutoff = (overview_today + timedelta(days=2)).isoformat()

                    def load_upcoming_checks():
                        with engine.connect() as planning_conn:
                            planning_conn.execute(text('SET LOCAL jit = off'))
                            return count_due_activity_riders(
                                planning_conn,
                                overview_today,
                                upcoming_cutoff,
                                include_upcoming=True,
                                include_checked_today=True,
                            )

                    # The correlated Needs Review expression is small enough to run
                    # directly; PostgreSQL JIT compilation costs more than this query.
                    conn.execute(text('SET LOCAL jit = off'))
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        upcoming_future = executor.submit(load_upcoming_checks)
                        monitor_summary = dict(conn.execute(text("""
                        SELECT
                            (SELECT COUNT(*) FROM activity_monitor_riders
                             WHERE eligible AND present_in_cdb) AS monitored,
                            (SELECT COUNT(*) FROM activity_monitor_riders r
                             WHERE r.eligible AND r.present_in_cdb
                               AND NOT EXISTS (
                                   SELECT 1 FROM activity_monitor_source_checks c
                                   WHERE c.rider_id = r.id
                               )) AS awaiting_first_check,
                            (SELECT COUNT(DISTINCT rider_id)
                             FROM activity_monitor_source_checks
                             WHERE rider_id IS NOT NULL) AS riders_checked,
                            (SELECT COUNT(DISTINCT rider_id)
                             FROM activity_monitor_results
                             WHERE rider_id IS NOT NULL
                               AND identity_verified
                               AND season = :current_year
                               AND (
                                   participation_status IN ('confirmed_participation', 'confirmed')
                                   OR UPPER(TRIM(COALESCE(result_position, ''))) = 'DSQ'
                               )) AS riders_active_this_year,
                            (SELECT COUNT(DISTINCT r.id)
                             FROM activity_monitor_riders r
                             WHERE r.eligible AND r.present_in_cdb
                               AND """ + _activity_monitor_attention_sql('r') + """) AS needs_attention,
                            (SELECT MAX(checked_at) FROM activity_monitor_source_checks) AS latest_check_at,
                            (SELECT MAX(COALESCE(finished_at, started_at, created_at))
                             FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%') AS latest_run_at,
                            (SELECT status FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_status,
                            (SELECT requested_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_requested,
                            (SELECT attempted_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_attempted,
                            (SELECT successful_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_successful
                    """), {
                        'current_year': overview_today.year,
                    }).mappings().one())
                        monitor_summary['upcoming_checks'] = upcoming_future.result()
                else:
                    history_unavailable = True
        except Exception:
            logger.exception(
                'Failed to load Activity Monitor CDB summary for namespace %s',
                selected_namespace.slug,
            )
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    return render_template(
        'activity_monitor.html',
        selected_namespace=selected_namespace,
        rider_count=rider_count,
        monitor_summary=monitor_summary,
        history_unavailable=history_unavailable,
        load_error=load_error,
        can_transfer_history=_can_transfer_activity_monitor_history(),
    )


@web_bp.route('/activity-monitor/history-transfer', methods=['GET', 'POST'])
@login_required
def activity_monitor_history_transfer():
    """Export local monitor history or preview/apply a one-time history bundle."""
    if not _can_access_panel('activity_monitor') or not _can_transfer_activity_monitor_history():
        abort(403)
    namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(namespaces)
    if selected_namespace is None:
        abort(404)
    main_engine = db.engines.get('main') or db.engine
    namespace_engine = None
    preview = None
    error = None
    completed = False
    archive_ready = False
    namespace_history_ready = False

    from webapp.tools.activity_monitor_global_history import (
        ARCHIVE_TABLE,
        TRANSFER_MARKER_TYPE,
        archive_namespace_history,
        build_history_bundle,
        import_and_restore_history_bundle,
        preview_history_bundle,
        validate_history_bundle,
    )
    from sqlalchemy import inspect as sqlalchemy_inspect

    try:
        namespace_engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        archive_ready = sqlalchemy_inspect(main_engine).has_table(ARCHIVE_TABLE)
        namespace_history_ready = sqlalchemy_inspect(namespace_engine).has_table('activity_monitor_riders')
        if archive_ready:
            with main_engine.connect() as connection:
                completed = connection.execute(text(f"""
                    SELECT EXISTS (SELECT 1 FROM {ARCHIVE_TABLE}
                     WHERE namespace_slug = '__pcm_manager_transfer__'
                       AND record_type = :marker_type
                       AND record_data->>'status' = 'complete')
                """), {'marker_type': TRANSFER_MARKER_TYPE}).scalar_one()
        if request.method != 'GET':
            action = request.form.get('action')
            uploaded = request.files.get('history_bundle')
            if action == 'prepare':
                from webapp.models.ns.activity_monitor import ActivityMonitorBase
                from webapp.tools.activity_monitor_global_history import install_global_history_schema

                install_global_history_schema(main_engine)
                ActivityMonitorBase.metadata.create_all(namespace_engine, checkfirst=True)
                flash('History storage is ready. No history was imported or deleted.', 'success')
                return redirect(url_for('web.activity_monitor_history_transfer'))
            if action == 'export':
                total_to_sync = 0
                for namespace in namespaces:
                    source_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
                    try:
                        namespace_summary = archive_namespace_history(
                            source_engine, main_engine, namespace.slug, apply=True,
                        )
                        total_to_sync += namespace_summary.get('records_to_sync', 0)
                    finally:
                        source_engine.dispose()
                bundle_bytes = build_history_bundle(main_engine)
                filename = f"activity-monitor-history-{selected_namespace.slug}.json"
                response = send_file(
                    io.BytesIO(bundle_bytes), mimetype='application/json',
                    as_attachment=True, download_name=filename,
                )
                response.headers['X-Activity-Monitor-Export-Records'] = str(total_to_sync)
                return response
            if uploaded is None or not uploaded.filename:
                error = 'Choose the Activity Monitor history export file first.'
            else:
                raw_bytes = uploaded.stream.read(100 * 1024 * 1024 + 1)
                if len(raw_bytes) > 100 * 1024 * 1024:
                    error = 'The export file is larger than the 100 MB limit.'
                else:
                    try:
                        validated = validate_history_bundle(raw_bytes)
                        preview = preview_history_bundle(main_engine, namespace_engine, validated)
                        if action == 'import':
                            if (request.form.get('confirm_import') != 'yes'
                                    or request.form.get('preview_sha256') != preview['bundle_sha256']):
                                error = 'The selected file differs from the preview. Review the new preview before importing.'
                            elif completed:
                                error = 'A history transfer has already been completed in this installation.'
                            else:
                                outcome = import_and_restore_history_bundle(
                                    main_engine, namespace_engine, validated,
                                    actor_user_id=current_user.id,
                                )
                                flash(
                                    f"History transfer completed: {outcome['records_imported']:,} archive records "
                                    f"processed and {outcome['riders_matched']:,} riders matched.",
                                    'success',
                                )
                                return redirect(url_for('web.activity_monitor_history_transfer'))
                    except (ValueError, RuntimeError) as exc:
                        error = str(exc)
    except Exception:
        logger.exception('Activity Monitor history transfer failed for namespace %s', selected_namespace.slug)
        error = ('The transfer stopped before completion. The archive may already be saved; '
                 'you can safely retry it, and no existing records were deleted.')
    finally:
        if namespace_engine is not None:
            namespace_engine.dispose()

    return render_template(
        'activity_monitor_history_transfer.html', selected_namespace=selected_namespace,
        preview=preview, error=error, completed=completed,
        archive_ready=archive_ready, namespace_history_ready=namespace_history_ready,
    )


@web_bp.route('/activity-monitor/riders')
@login_required
def activity_monitor_riders():
    """Read-only rider roster with imported Activity Monitor history."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    filters = {
        'q': (request.args.get('q', type=str) or '').strip(),
        'team': (request.args.get('team', type=str) or '').strip(),
        'country': (request.args.get('country', type=str) or '').strip(),
        'division': (request.args.get('division', type=str) or '').strip(),
        'status': (request.args.get('status', type=str) or 'monitored').strip().lower(),
        'sort': (request.args.get('sort', type=str) or 'name').strip().lower(),
        'direction': (request.args.get('direction', type=str) or 'asc').strip().lower(),
    }
    export_csv = request.args.get('export', type=str) == 'csv'
    if filters['status'] not in {'monitored', 'awaiting', 'checked', 'attention', 'no_evidence', 'other', 'all'}:
        filters['status'] = 'monitored'
    if filters['direction'] not in {'asc', 'desc'}:
        filters['direction'] = 'asc'
    page = max(request.args.get('page', type=int) or 1, 1)
    per_page_raw = (request.args.get('per_page', type=str) or '50').strip().lower()
    show_all = per_page_raw == 'all'
    per_page = int(per_page_raw) if per_page_raw.isdigit() else 50
    if not show_all and per_page not in {25, 50, 100, 1000}:
        per_page = 50
    per_page_arg = 'all' if show_all else per_page

    riders = []
    total = 0
    team_options = []
    country_options = []
    division_options = []
    load_error = False
    history_unavailable = False
    if selected_namespace is not None:
        engine = None
        try:
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                history_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_riders') IS NOT NULL")
                ).scalar_one()
                if not history_ready:
                    history_unavailable = True
                else:
                    team_label_sql = """CASE
                        WHEN r.birth_date IS NOT NULL
                         AND EXTRACT(YEAR FROM CURRENT_DATE)::integer - EXTRACT(YEAR FROM r.birth_date)::integer BETWEEN 0 AND 18
                        THEN 'Juniors'
                        ELSE NULLIF(BTRIM(r.team_name), '')
                    END"""
                    division_label_sql = """CASE
                        WHEN upper(BTRIM(COALESCE(d.constant, ''))) = 'FA'
                            THEN 'Free'
                        WHEN upper(BTRIM(COALESCE(d.constant, ''))) IN ('U23', 'WT', 'PT', 'CT')
                            THEN upper(BTRIM(d.constant))
                        WHEN lower(COALESCE(d.constant, '')) ~ 'free[[:space:]_-]*(agent|agents)?'
                            THEN 'Free'
                        WHEN lower(COALESCE(d.constant, '')) ~ '(u[[:space:]_-]*23|under[[:space:]_-]*23)'
                            THEN 'U23'
                        WHEN lower(COALESCE(d.constant, '')) ~ 'world[[:space:]_-]*tour'
                            THEN 'WT'
                        WHEN lower(BTRIM(COALESCE(d.constant, ''))) ~ '.*pro[[:space:]_-]*(team|tour).*'
                            THEN 'PT'
                        WHEN lower(COALESCE(d.constant, '')) ~ 'continental'
                            THEN 'CT'
                        ELSE COALESCE(NULLIF(BTRIM(d.constant), ''), d.id::text, '—')
                    END"""
                    team_options = conn.execute(text("""
                        SELECT DISTINCT """ + team_label_sql + """ AS value
                        FROM activity_monitor_riders r
                        WHERE """ + team_label_sql + """ IS NOT NULL
                        ORDER BY value
                    """)).scalars().all()
                    country_name_sql = _activity_monitor_country_sql()
                    country_options = conn.execute(text(f"""
                        SELECT DISTINCT {country_name_sql} AS value
                        FROM activity_monitor_riders r
                        ORDER BY value
                    """)).scalars().all()
                    division_options = conn.execute(text("""
                        SELECT value, value AS label
                        FROM (
                            SELECT DISTINCT """ + division_label_sql + """ AS value
                            FROM division d
                            JOIN team t ON t.division_id = d.id
                            WHERE EXISTS (
                                SELECT 1 FROM activity_monitor_riders r WHERE r.team_id = t.id
                            )
                        ) AS division_options
                        ORDER BY CASE value
                            WHEN 'Free' THEN 1 WHEN 'U23' THEN 2 WHEN 'WT' THEN 3
                            WHEN 'PT' THEN 4 WHEN 'CT' THEN 5 ELSE 6 END, label
                    """)).mappings().all()

                    where = []
                    params = {} if export_csv or show_all else {
                        'limit': per_page,
                        'offset': (page - 1) * per_page,
                    }
                    if filters['q']:
                        search_term = _normalize_name_search_term(filters['q'])
                        where.append("""(
                            """ + _normalized_name_sql("CONCAT_WS(' ', r.first_name, r.last_name)") + """ LIKE :q
                            OR """ + _normalized_name_sql('r.first_name') + """ LIKE :q
                            OR """ + _normalized_name_sql('r.last_name') + """ LIKE :q
                            OR r.source_worlddb_rider_id::text = :exact
                            OR r.first_cycling_id::text = :exact
                        )""")
                        params.update(q=f"%{search_term}%", exact=filters['q'])
                    if filters['team']:
                        where.append(f"({_normalized_name_sql(f'({team_label_sql})')} LIKE :team OR r.team_id::text = :team_exact)")
                        params.update(team=f"%{_normalize_name_search_term(filters['team'])}%", team_exact=filters['team'])
                    if filters['country']:
                        where.append(_normalized_name_sql(country_name_sql) + " LIKE :country")
                        params['country'] = f"%{_normalize_name_search_term(filters['country'])}%"
                    if filters['division']:
                        where.append(
                            "(" + _normalized_name_sql(f"({division_label_sql})") + " LIKE :division"
                            + " OR t.division_id::text = :division_exact"
                            + " OR " + _normalized_name_sql("COALESCE(d.constant, '')") + " LIKE :division)"
                        )
                        params.update(division=f"%{_normalize_name_search_term(filters['division'])}%", division_exact=filters['division'])

                    status_conditions = {
                        'monitored': 'r.eligible AND r.present_in_cdb',
                        'awaiting': """r.eligible AND r.present_in_cdb AND NOT EXISTS (
                            SELECT 1 FROM activity_monitor_source_checks c WHERE c.rider_id = r.id
                        )""",
                        'checked': """r.eligible AND r.present_in_cdb AND EXISTS (
                            SELECT 1 FROM activity_monitor_source_checks c WHERE c.rider_id = r.id
                        )""",
                        'attention': _activity_monitor_attention_sql('r'),
                        'no_evidence': """r.eligible AND r.present_in_cdb AND NOT EXISTS (
                            SELECT 1 FROM activity_monitor_results e
                            WHERE e.rider_id = r.id
                              AND e.identity_generation = r.identity_generation
                              AND e.identity_verified
                              AND (
                                  e.participation_status IN ('confirmed_participation', 'confirmed')
                                  OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                              )
                              AND e.result_date <= CURRENT_DATE
                              AND (
                                  LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'pcs_html', 'procyclingstats')
                                  OR EXISTS (
                                      SELECT 1 FROM activity_monitor_source_checks pcs_check
                                      WHERE pcs_check.id = e.source_check_id
                                        AND pcs_check.rider_id = r.id
                                        AND pcs_check.identity_generation = r.identity_generation
                                        AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                        AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                                            IN ('success', 'results parsed', 'results table empty')
                                        AND (
                                            regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                                            OR pcs_check.source_record_key LIKE 'source_checks:%'
                                        )
                                  )
                              )
                        )""",
                        'other': '(NOT r.eligible OR NOT r.present_in_cdb)',
                    }
                    if filters['status'] in status_conditions:
                        where.append(status_conditions[filters['status']])
                    where_sql = f"WHERE {' AND '.join(where)}" if where else ''

                    if request.args.get('select_filtered_ids') == '1':
                        selection_params = {
                            key: value for key, value in params.items()
                            if key not in {'limit', 'offset'}
                        }
                        selection_ids = conn.execute(text(f"""
                            SELECT r.id
                            FROM activity_monitor_riders r
                            LEFT JOIN team t ON t.id = r.team_id
                            LEFT JOIN division d ON d.id = t.division_id
                            {where_sql}
                            {'AND' if where_sql else 'WHERE'} r.eligible AND r.present_in_cdb
                            ORDER BY r.last_name ASC NULLS LAST,
                                     r.first_name ASC NULLS LAST,
                                     r.id ASC
                            LIMIT 251
                        """), selection_params).scalars().all()
                        return jsonify({
                            'ids': [int(value) for value in selection_ids[:250]],
                            'truncated': len(selection_ids) > 250,
                        })

                    sort_columns = {
                        'name': 'r.last_name',
                        'first_name': 'r.first_name',
                        'last_name': 'r.last_name',
                        'team': team_label_sql,
                        'country': 'country_name',
                        'division': 'division_name',
                        'age': "date_part('year', age(CURRENT_DATE, r.birth_date))",
                        'status': f"""CASE
                            WHEN NOT r.eligible OR NOT r.present_in_cdb THEN 'Not monitored'
                            WHEN {_activity_monitor_attention_sql('r')} THEN 'Needs Review'
                            WHEN latest_result.result_date IS NOT NULL THEN 'Activity documented'
                            WHEN latest_check.checked_at IS NULL THEN 'Awaiting first check'
                            ELSE 'No race evidence'
                        END""",
                        'checked': 'latest_check_at',
                        'race': 'latest_race_name',
                        'result': 'latest_result_date',
                        'days': '(CURRENT_DATE - latest_result.result_date)',
                        'race_days': "CASE WHEN pcs_year_activity.checked_this_year THEN pcs_year_activity.race_days ELSE NULL END",
                        'id': 'r.source_worlddb_rider_id',
                    }
                    if filters['sort'] not in sort_columns:
                        filters['sort'] = 'name'
                    sort_sql = sort_columns[filters['sort']]
                    sort_direction = filters['direction'].upper()
                    total = conn.execute(text(f"""
                        SELECT COUNT(*)
                        FROM activity_monitor_riders r
                        LEFT JOIN team t ON t.id = r.team_id
                        LEFT JOIN division d ON d.id = t.division_id
                        {where_sql}
                    """), params).scalar_one()
                    rider_limit_sql = '' if export_csv or show_all else 'LIMIT :limit OFFSET :offset'
                    rider_rows = conn.execute(text(f"""
                        SELECT r.id AS monitor_rider_id,
                               r.source_worlddb_rider_id AS worlddb_rider_id,
                               r.first_cycling_id, r.first_name, r.last_name, r.birth_date,
                               r.nationality, r.nationality_code, r.team_id,
                               NULLIF(r.team_name, '') AS team_name,
                               {country_name_sql} AS country_name,
                               {division_label_sql} AS division_name,
                               r.identity_status, r.identity_hold, r.identity_hold_reason,
                               CASE WHEN r.birth_date IS NOT NULL
                                    THEN date_part('year', age(CURRENT_DATE, r.birth_date))::integer
                                    ELSE NULL END AS age,
                               r.eligible, r.present_in_cdb,
                               EXISTS (
                                   SELECT 1 FROM activity_monitor_identity_alerts a
                                   WHERE a.rider_id = r.id AND a.status = 'open'
                               ) AS has_open_alert,
                               """ + _activity_monitor_attention_sql('r') + f""" AS needs_attention,
                               latest_check.checked_at AS latest_check_at,
                               latest_check.source AS latest_check_source,
                               latest_check.source_status AS latest_check_status,
                               latest_check.identity_result AS latest_check_identity_result,
                               latest_pcs.checked_at AS latest_pcs_check_at,
                               latest_pcs.request_path AS latest_pcs_check_path,
                               latest_pcs.source_status AS latest_pcs_check_status,
                               latest_pcs.identity_result AS latest_pcs_check_identity_result,
                               latest_pcs.message AS latest_pcs_check_message,
                               latest_result.result_date AS latest_result_date,
                               latest_result.race_name AS latest_race_name,
                               latest_result.result_position AS latest_result_position,
                               latest_result.participation_status AS latest_participation_status,
                               CASE WHEN pcs_year_activity.checked_this_year
                                    THEN pcs_year_activity.race_days ELSE NULL END AS pcs_race_days_current_year,
                               latest_profile.profile_path AS profile_path,
                               latest_profile.link_source AS profile_link_source,
                               latest_profile.link_status AS profile_link_status,
                               latest_profile.check_result AS profile_check_result,
                               latest_profile.checked_url AS profile_checked_url,
                               latest_profile.approved_profile_path AS approved_profile_path,
                               latest_profile.approved_pcs_birth_date AS approved_pcs_birth_date,
                               latest_profile.approved_worlddb_birth_date AS approved_worlddb_birth_date,
                               latest_profile.approved_at AS profile_approved_at,
                               latest_profile.profile_birth_date AS profile_birth_date
                        FROM activity_monitor_riders r
                        LEFT JOIN team t ON t.id = r.team_id
                        LEFT JOIN division d ON d.id = t.division_id
                        LEFT JOIN LATERAL (
                            SELECT c.checked_at, c.source, c.source_status, c.identity_result
                            FROM activity_monitor_source_checks c
                            WHERE c.rider_id = r.id
                            ORDER BY c.checked_at DESC, c.id DESC
                            LIMIT 1
                        ) latest_check ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT c.checked_at, c.request_path, c.source_status,
                                   c.identity_result, c.message
                            FROM activity_monitor_source_checks c
                            WHERE c.rider_id = r.id
                              AND c.identity_generation = r.identity_generation
                              AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                            ORDER BY c.checked_at DESC, c.id DESC
                            LIMIT 1
                        ) latest_pcs ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT e.result_date, e.race_name, e.result_position, e.participation_status
                            FROM activity_monitor_results e
                            WHERE e.rider_id = r.id
                              AND e.identity_verified
                              AND (
                                  e.participation_status IN ('confirmed_participation', 'confirmed')
                                  OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                              )
                              AND e.result_date <= CURRENT_DATE
                              AND (
                                  LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'pcs_html', 'procyclingstats')
                                  OR EXISTS (
                                      SELECT 1 FROM activity_monitor_source_checks pcs_check
                                      WHERE pcs_check.id = e.source_check_id
                                        AND pcs_check.rider_id = r.id
                                        AND pcs_check.identity_generation = r.identity_generation
                                        AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                        AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                                            IN ('success', 'results parsed', 'results table empty')
                                        AND (
                                            regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                                            OR pcs_check.source_record_key LIKE 'source_checks:%'
                                        )
                                  )
                              )
                            ORDER BY e.result_date DESC NULLS LAST, e.id DESC
                            LIMIT 1
                        ) latest_result ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT BOOL_OR(
                                       LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                       AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                           IN ('success', 'results parsed', 'results table empty')
                                       AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                       AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                       AND (
                                           regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                           OR c.source_record_key LIKE 'source_checks:%'
                                       )
                                   ) AS checked_this_year,
                                   COUNT(DISTINCT e.result_date) FILTER (
                                       WHERE LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                         AND e.identity_verified
                                         AND (
                                             e.participation_status IN ('confirmed_participation', 'confirmed')
                                             OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                         )
                                         AND e.result_date >= date_trunc('year', CURRENT_DATE)::date
                                         AND e.result_date <= CURRENT_DATE
                                         AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                         AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                             IN ('success', 'results parsed', 'results table empty')
                                         AND (
                                             regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                             OR c.source_record_key LIKE 'source_checks:%'
                                         )
                                   )::integer AS race_days
                            FROM activity_monitor_source_checks c
                            LEFT JOIN activity_monitor_results e
                              ON e.source_check_id = c.id
                             AND e.rider_id = r.id
                             AND e.identity_generation = r.identity_generation
                            WHERE c.rider_id = r.id
                              AND c.identity_generation = r.identity_generation
                              AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                        ) pcs_year_activity ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT COALESCE(p.approved_profile_path, p.profile_path,
                                            p.override_url) AS profile_path,
                                   p.link_source, p.link_status, p.check_result,
                                   p.checked_url, p.approved_profile_path,
                                   p.approved_pcs_birth_date, p.approved_worlddb_birth_date,
                                   p.approved_at, p.profile_birth_date
                            FROM activity_monitor_profile_links p
                            WHERE p.rider_id = r.id
                              AND p.identity_generation = r.identity_generation
                              AND p.is_current = TRUE
                            ORDER BY p.is_current DESC,
                                     COALESCE(p.verified_at, p.checked_at, p.created_at) DESC NULLS LAST,
                                     p.id DESC
                            LIMIT 1
                        ) latest_profile ON TRUE
                        {where_sql}
                        ORDER BY {sort_sql} {sort_direction} NULLS LAST,
                                 r.id ASC
                        {rider_limit_sql}
                    """), params).mappings().all()
                    riders = [dict(row) for row in rider_rows]
        except Exception:
            logger.exception(
                'Failed to load Activity Monitor riders for namespace %s',
                selected_namespace.slug,
            )
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    total_pages = 1 if show_all else max((int(total) + per_page - 1) // per_page, 1)
    if page > total_pages and total:
        return redirect(url_for('web.activity_monitor_riders', **{
            key: value for key, value in filters.items() if value
        }, per_page=per_page_arg, page=total_pages))

    for rider in riders:
        birth_date = rider.get('birth_date')
        rider['team_display'] = (
            'Juniors'
            if birth_date and 0 <= date.today().year - birth_date.year <= 18
            else rider.get('team_name') or '—'
        )
        result_date = rider.get('latest_result_date')
        rider['days_since_last_race'] = (date.today() - result_date).days if result_date else None
        raw_path = (rider.get('profile_path') or '').strip()
        parsed = urllib.parse.urlsplit(raw_path if '://' in raw_path else '')
        trimmed_path = raw_path.lstrip('/')
        path = parsed.path if parsed.scheme else f'/{trimmed_path}' if raw_path else ''
        if parsed.scheme and parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            path = ''
        rider['pcs_url'] = (
            urllib.parse.urlunsplit(('https', 'www.procyclingstats.com', path, '', ''))
            if path.startswith('/rider/') else None
        )
        rider['pcs_slug'] = ''
        if rider['pcs_url']:
            slug_match = re.fullmatch(r'/rider/([A-Za-z0-9_-]+)(?:/road)?/?', path, re.IGNORECASE)
            rider['pcs_slug'] = slug_match.group(1) if slug_match else ''
        rider['pcs_slug_assumed'] = False
        if not rider['pcs_url'] and not rider.get('identity_hold'):
            rider['pcs_slug'], rider['pcs_url'] = _assumed_pcs_profile(
                rider.get('first_name'), rider.get('last_name')
            )
            rider['pcs_slug_assumed'] = bool(rider['pcs_slug'])
        rider['pcs_slug_color'] = _activity_pcs_slug_color(
            {
                'link_source': rider.get('profile_link_source'),
                'check_result': rider.get('profile_check_result'),
                'checked_url': rider.get('profile_checked_url'),
                'approved_profile_path': rider.get('approved_profile_path'),
                'approved_pcs_birth_date': rider.get('approved_pcs_birth_date'),
                'approved_worlddb_birth_date': rider.get('approved_worlddb_birth_date'),
                'approved_at': rider.get('profile_approved_at'),
                'profile_birth_date': rider.get('profile_birth_date'),
            },
            {
                'request_path': rider.get('latest_pcs_check_path'),
                'source_status': rider.get('latest_pcs_check_status'),
                'identity_result': rider.get('latest_pcs_check_identity_result'),
                'message': rider.get('latest_pcs_check_message'),
            },
            rider.get('pcs_url'),
            birth_date,
        )
        source = (rider.get('profile_link_source') or '').lower()
        rider['link_source_display'] = {
            'manual': 'Manual link',
            'manual_override': 'Manual link',
            'verified': 'Verified PCS link',
            'auto_verified': 'Verified PCS link',
            'assumed': 'Name-based candidate',
            'candidate': 'PCS candidate',
        }.get(source, 'PCS profile')
        check_source = re.sub(
            r'[^a-z0-9]+', '_', str(rider.get('latest_check_source') or '').strip().casefold()
        ).strip('_')
        rider['latest_check_source_display'] = {
            'pcs': 'PCS',
            'pcs_html': 'PCS',
            'procyclingstats': 'PCS',
            'firstcycling': 'FirstCycling',
            'fc_json': 'FirstCycling',
            'firstcycling_json': 'FirstCycling',
            'fc_api': 'FirstCycling',
        }.get(check_source, check_source.replace('_', ' ').title() or '—')
        check_status = (rider.get('latest_check_status') or '').replace('_', ' ').strip()
        rider['latest_check_status_display'] = check_status or '—'
        rider['needs_attention'] = bool(rider.get('needs_attention'))
        if not rider.get('eligible') or not rider.get('present_in_cdb'):
            rider['status_label'] = 'Not monitored'
        elif rider['needs_attention']:
            rider['status_label'] = 'Needs Review'
        elif rider.get('latest_result_date'):
            rider['status_label'] = 'Activity documented'
        elif not rider.get('latest_check_at'):
            rider['status_label'] = 'Awaiting first check'
        else:
            rider['status_label'] = 'No race evidence'

    if export_csv:
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow([
            'WorldDB ID', 'First name', 'Last name', 'FirstCycling ID', 'Team',
            'Nationality', 'Age', 'Division', 'Monitor status', 'Last check',
            'Last check source', 'Last check outcome', 'Last race', 'Race date',
            'Days since race', f'PCS race days {date.today().year}', 'PCS profile',
        ])
        for rider in riders:
            writer.writerow([
                rider.get('worlddb_rider_id'), rider.get('first_name'), rider.get('last_name'),
                rider.get('first_cycling_id'), rider.get('team_display'), rider.get('country_name'),
                rider.get('age'), rider.get('division_name'), rider.get('status_label'),
                rider.get('latest_check_at'), rider.get('latest_check_source_display'),
                rider.get('latest_check_status_display'), rider.get('latest_race_name'),
                rider.get('latest_result_date'), rider.get('days_since_last_race'),
                rider.get('pcs_race_days_current_year'), rider.get('pcs_url') or '',
            ])
        response = make_response('\ufeff' + output.getvalue())
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename="worlddb-activity-riders.csv"'
        return response

    return render_template(
        'activity_monitor_riders.html',
        selected_namespace=selected_namespace,
        riders=riders,
        current_year=date.today().year,
        filters=filters,
        team_options=team_options,
        country_options=country_options,
        division_options=division_options,
        page=page,
        per_page=per_page,
        per_page_arg=per_page_arg,
        show_all=show_all,
        total=int(total),
        total_pages=total_pages,
        start=1 if show_all and total else (((page - 1) * per_page + 1) if total else 0),
        end=int(total) if show_all else min(page * per_page, int(total)),
        load_error=load_error,
        history_unavailable=history_unavailable,
    )


@web_bp.route('/activity-monitor/problems')
@login_required
def activity_monitor_problems():
    """Read-only identity alert history for the selected Activity Monitor namespace."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    status = request.args.get('status', 'open').strip().lower()
    if status not in {'open', 'resolved', 'all'}:
        status = 'open'
    alerts = []
    source_problems = []
    counts = {'open': 0, 'resolved': 0, 'all': 0}
    load_error = False
    history_unavailable = False

    if selected_namespace is not None:
        engine = None
        try:
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                table_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_identity_alerts') IS NOT NULL")
                ).scalar_one()
                source_tables_ready = conn.execute(text("""
                    SELECT to_regclass('activity_monitor_source_checks') IS NOT NULL
                       AND to_regclass('activity_monitor_runs') IS NOT NULL
                """)).scalar_one()
                source_history_unavailable = not source_tables_ready
                if not table_ready:
                    history_unavailable = True
                else:
                    counts_row = conn.execute(text("""
                        SELECT COUNT(*) AS all_count,
                               COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                               COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_count
                        FROM activity_monitor_identity_alerts
                    """)).mappings().one()
                    counts = {
                        'all': counts_row['all_count'],
                        'open': counts_row['open_count'],
                        'resolved': counts_row['resolved_count'],
                    }
                    status_filter = '' if status == 'all' else 'AND a.status = :status'
                    params = {} if status == 'all' else {'status': status}
                    alerts = [dict(item) for item in conn.execute(text(f"""
                        SELECT a.id, a.rider_id, a.source_worlddb_rider_id,
                               a.created_at, a.reason, a.status, a.resolved_at,
                               a.resolution, r.first_name, r.last_name
                        FROM activity_monitor_identity_alerts a
                        LEFT JOIN activity_monitor_riders r ON r.id = a.rider_id
                        WHERE TRUE {status_filter}
                        ORDER BY CASE WHEN a.status = 'open' THEN 0 ELSE 1 END,
                                 a.created_at DESC, a.id DESC
                        LIMIT 500
                    """), params).mappings().all()]
                if source_tables_ready:
                    # Match Arie Tool semantics: Problems is for a human to fix
                    # rider/profile identity data. Transport errors and an empty
                    # result history belong in check evidence, not this queue.
                    schedule_table_ready = conn.execute(text(
                        "SELECT to_regclass('activity_monitor_rider_schedules') IS NOT NULL"
                    )).scalar_one()
                    profile_links_ready = conn.execute(text(
                        "SELECT to_regclass('activity_monitor_profile_links') IS NOT NULL"
                    )).scalar_one()
                    next_check_sql = "NULL AS next_check_date"
                    if profile_links_ready:
                        pcs_profile_sql = """
                                   COALESCE(NULLIF(current_profile.approved_profile_path, ''),
                                            NULLIF(current_profile.override_url, ''),
                                            NULLIF(current_profile.profile_path, '')) AS pcs_profile_path,
                                   current_profile.link_source AS pcs_link_source,
                                   current_profile.check_result AS pcs_link_check_result,
                                   current_profile.checked_url AS pcs_link_checked_url,
                                   current_profile.approved_profile_path AS approved_profile_path,
                                   current_profile.approved_pcs_birth_date AS approved_pcs_birth_date,
                                   current_profile.approved_worlddb_birth_date AS approved_worlddb_birth_date,
                                   current_profile.approved_at AS profile_approved_at,
                                   current_profile.profile_birth_date AS pcs_profile_birth_date
                        """
                        pcs_profile_join = """
                        LEFT JOIN LATERAL (
                            SELECT p.profile_path, p.override_url,
                                   p.approved_profile_path, p.link_source, p.check_result,
                                   p.checked_url, p.approved_pcs_birth_date,
                                   p.approved_worlddb_birth_date, p.approved_at,
                                   p.profile_birth_date
                            FROM activity_monitor_profile_links p
                            WHERE p.rider_id = r.id
                              AND p.identity_generation = r.identity_generation
                              AND p.is_current = TRUE
                            ORDER BY p.id DESC LIMIT 1
                        ) current_profile ON TRUE
                        """
                    else:
                        pcs_profile_sql = """
                                   NULL AS pcs_profile_path,
                                   NULL AS pcs_link_source, NULL AS pcs_link_check_result,
                                   NULL AS pcs_link_checked_url, NULL AS approved_profile_path,
                                   NULL AS approved_pcs_birth_date,
                                   NULL AS approved_worlddb_birth_date,
                                   NULL AS profile_approved_at, NULL AS pcs_profile_birth_date
                        """
                        pcs_profile_join = ''
                    source_problems = [dict(item) for item in conn.execute(text(f"""
                        SELECT c.id, c.rider_id, c.source_worlddb_rider_id,
                               c.source, c.source_status, c.coverage_status,
                               c.identity_result, c.checked_at, c.http_status,
                               c.message, r.first_name, r.last_name,
                               r.birth_date,
                               CASE
                                   WHEN r.birth_date IS NOT NULL
                                    AND EXTRACT(YEAR FROM CURRENT_DATE)::integer - EXTRACT(YEAR FROM r.birth_date)::integer BETWEEN 0 AND 18
                                   THEN 'Juniors'
                                   ELSE COALESCE(NULLIF(BTRIM(r.team_name), ''), '—')
                               END AS team_name,
                               CASE
                                   WHEN upper(BTRIM(COALESCE(d.constant, ''))) = 'FA' THEN 'Free'
                                   WHEN upper(BTRIM(COALESCE(d.constant, ''))) IN ('U23', 'WT', 'PT', 'CT') THEN upper(BTRIM(d.constant))
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'free[[:space:]_-]*(agent|agents)?' THEN 'Free'
                                   WHEN lower(COALESCE(d.constant, '')) ~ '(u[[:space:]_-]*23|under[[:space:]_-]*23)' THEN 'U23'
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'world[[:space:]_-]*tour' THEN 'WT'
                                   WHEN lower(BTRIM(COALESCE(d.constant, ''))) ~ '.*pro[[:space:]_-]*(team|tour).*' THEN 'PT'
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'continental' THEN 'CT'
                                   ELSE COALESCE(NULLIF(BTRIM(d.constant), ''), d.id::text, '—')
                               END AS division_name,
                               {next_check_sql},
                               {pcs_profile_sql},
                               latest_pcs.request_path AS latest_pcs_check_path,
                               latest_pcs.source_status AS latest_pcs_check_status,
                               latest_pcs.identity_result AS latest_pcs_check_identity_result,
                               latest_pcs.message AS latest_pcs_check_message
                        FROM activity_monitor_source_checks c
                        JOIN activity_monitor_runs run ON run.id = c.run_id
                        LEFT JOIN activity_monitor_riders r ON r.id = c.rider_id
                        LEFT JOIN team t ON t.id = r.team_id
                        LEFT JOIN division d ON d.id = t.division_id
                        {pcs_profile_join}
                        LEFT JOIN LATERAL (
                            SELECT pcs.request_path, pcs.source_status,
                                   pcs.identity_result, pcs.message
                            FROM activity_monitor_source_checks pcs
                            WHERE pcs.rider_id = r.id
                              AND pcs.identity_generation = r.identity_generation
                              AND lower(trim(coalesce(pcs.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                            ORDER BY pcs.checked_at DESC, pcs.id DESC
                            LIMIT 1
                        ) latest_pcs ON TRUE
                        WHERE (run.source_record_key LIKE 'manual-run:%' OR run.source_record_key LIKE 'scheduled-run:%')
                          AND (
                              c.source_status IN (
                                  'missing_link', 'profile_not_found',
                                  'identity_not_verified', 'identity_mismatch'
                              )
                              OR (
                                  c.source_status NOT IN ('http_error', 'network_error', 'invalid_response')
                                  AND c.identity_result IN (
                                      'mismatch', 'ambiguous', 'unverified',
                                      'unknown', 'unavailable', 'manual_review',
                                      'needs_review'
                                  )
                              )
                          )
                          AND NOT (
                              lower(trim(coalesce(c.source, ''))) IN (
                                  'firstcycling', 'firstcycling_json', 'fc_json'
                              )
                              AND EXISTS (
                                  SELECT 1
                                  FROM activity_monitor_source_checks pcs_success
                                  WHERE pcs_success.rider_id = c.rider_id
                                    AND pcs_success.identity_generation = c.identity_generation
                                    AND lower(trim(coalesce(pcs_success.source, ''))) IN (
                                        'pcs', 'pcs_html', 'procyclingstats'
                                    )
                                    AND pcs_success.identity_result = 'verified'
                                    AND pcs_success.source_status IN (
                                        'success', 'results_parsed', 'results_table_empty'
                                    )
                                    AND (
                                        pcs_success.run_id = c.run_id
                                        OR pcs_success.checked_at > c.checked_at
                                    )
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM activity_monitor_source_checks newer
                              JOIN activity_monitor_runs newer_run ON newer_run.id = newer.run_id
                              WHERE newer.rider_id = c.rider_id
                                AND newer.source = c.source
                                AND newer.identity_generation = c.identity_generation
                                AND newer.identity_result = 'verified'
                                AND newer.source_status IN ('success', 'results_parsed', 'results_table_empty')
                                AND (newer_run.source_record_key LIKE 'manual-run:%' OR newer_run.source_record_key LIKE 'scheduled-run:%')
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM activity_monitor_source_checks latest
                                    JOIN activity_monitor_runs latest_run ON latest_run.id = latest.run_id
                                    WHERE latest.rider_id = c.rider_id
                                      AND latest.source = c.source
                                      AND latest.identity_generation = c.identity_generation
                                      AND (latest_run.source_record_key LIKE 'manual-run:%' OR latest_run.source_record_key LIKE 'scheduled-run:%')
                                      AND (latest.checked_at, latest.id) > (newer.checked_at, newer.id)
                                )
                          )
                        ORDER BY c.checked_at DESC, c.id DESC
                        LIMIT 300
                    """)).mappings().all()]
                    next_check_by_rider = {}
                    if schedule_table_ready:
                        problem_rider_ids = {
                            check['rider_id'] for check in source_problems
                            if check.get('rider_id') is not None
                        }
                        if problem_rider_ids:
                            _, planned_riders = due_activity_riders(
                                conn,
                                datetime.now(ZoneInfo('Europe/Amsterdam')).date(),
                                limit=len(problem_rider_ids),
                                include_upcoming=True,
                                include_checked_today=True,
                                rider_ids=problem_rider_ids,
                            )
                            next_check_by_rider = {
                                item['monitor_rider_id']: item.get('next_research_date')
                                for item in planned_riders
                            }
                    for check in source_problems:
                        check['next_check_date'] = next_check_by_rider.get(check.get('rider_id'))
                        raw_profile_path = str(check.get('pcs_profile_path') or '').strip()
                        parsed_profile = urllib.parse.urlsplit(
                            raw_profile_path if '://' in raw_profile_path else ''
                        )
                        profile_path = parsed_profile.path if parsed_profile.scheme else f"/{raw_profile_path.lstrip('/')}"
                        slug_match = re.fullmatch(r'/rider/([A-Za-z0-9_-]+)(?:/road)?/?', profile_path, re.IGNORECASE)
                        check['pcs_slug'] = slug_match.group(1) if slug_match else ''
                        check['pcs_url'] = (
                            f"https://www.procyclingstats.com/rider/{check['pcs_slug']}/road"
                            if check['pcs_slug'] else ''
                        )
                        check['pcs_slug_assumed'] = False
                        if not check['pcs_slug'] and check.get('rider_id'):
                            check['pcs_slug'], check['pcs_url'] = _assumed_pcs_profile(
                                check.get('first_name'), check.get('last_name')
                            )
                            check['pcs_slug_assumed'] = bool(check['pcs_slug'])
                        check['pcs_slug_color'] = _activity_pcs_slug_color(
                            {
                                'link_source': check.get('pcs_link_source'),
                                'check_result': check.get('pcs_link_check_result'),
                                'checked_url': check.get('pcs_link_checked_url'),
                                'approved_profile_path': check.get('approved_profile_path'),
                                'approved_pcs_birth_date': check.get('approved_pcs_birth_date'),
                                'approved_worlddb_birth_date': check.get('approved_worlddb_birth_date'),
                                'approved_at': check.get('profile_approved_at'),
                                'profile_birth_date': check.get('pcs_profile_birth_date'),
                            },
                            {
                                'request_path': check.get('latest_pcs_check_path'),
                                'source_status': check.get('latest_pcs_check_status'),
                                'identity_result': check.get('latest_pcs_check_identity_result'),
                                'message': check.get('latest_pcs_check_message'),
                            },
                            check.get('pcs_url'),
                            check.get('birth_date'),
                        )
        except Exception:
            logger.exception('Failed to load Activity Monitor problems for namespace %s', selected_namespace.slug)
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    return render_template(
        'activity_monitor_problems.html',
        selected_namespace=selected_namespace,
        alerts=alerts,
        source_problems=source_problems,
        counts=counts,
        status=status,
        load_error=load_error,
        history_unavailable=history_unavailable,
        source_history_unavailable=source_history_unavailable,
    )


@web_bp.route('/activity-monitor/automation', methods=['POST'])
@login_required
def activity_monitor_save_automation():
    """Save an opt-in, namespace-scoped automatic check schedule."""
    if not _can_access_panel('activity_monitor'):
        abort(403)
    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before changing automatic checks.', 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))

    enabled = request.form.get('enabled') == 'yes'
    checks_per_hour = request.form.get('checks_per_hour', type=int) or 1
    request_speed = request.form.get('automation_speed', 'very_slow')
    speed_delays = {'fast': 2, 'medium': 5, 'slow': 15, 'very_slow': 60}
    if request_speed not in speed_delays:
        flash('Choose a valid check speed.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks'))
    request_delay_seconds = speed_delays[request_speed]

    window_start = (request.form.get('window_start', type=str) or '').strip()
    window_end = (request.form.get('window_end', type=str) or '').strip()
    try:
        start_time = datetime.strptime(window_start, '%H:%M').time()
        end_time = datetime.strptime(window_end, '%H:%M').time()
    except ValueError:
        flash('Enter a valid start and end time.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks'))
    if not 1 <= checks_per_hour <= 5:
        flash('Choose between 1 and 5 rounds per hour.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks'))
    if start_time >= end_time:
        flash('The end time must be later than the start time on the same day.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks'))
    if enabled and not current_app.config.get('ACTIVITY_MONITOR_PCS_ENABLED', False):
        flash('Automatic checks were not enabled. PCS requests are disabled for this server.', 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        now = datetime.now(ZoneInfo('Europe/Amsterdam')).replace(tzinfo=None)
        with engine.begin() as conn:
            has_schedule_table = conn.execute(text(
                "SELECT to_regclass('activity_monitor_schedules') IS NOT NULL"
            )).scalar_one()
            if not has_schedule_table:
                flash('Automatic check settings are not installed in this namespace yet.', 'warning')
                return redirect(url_for('web.activity_monitor_run_checks'))
            conn.execute(text("""
                INSERT INTO activity_monitor_schedules (
                    schedule_key, enabled, checks_per_hour, window_start,
                    window_end, delay_min_seconds, delay_max_seconds,
                    next_run_at, updated_by_user_id, updated_at
                ) VALUES (
                    'activity-monitor', :enabled, :checks_per_hour,
                    :window_start, :window_end, :delay_min_seconds,
                    :delay_max_seconds, :next_run_at, :user_id, :updated_at
                )
                ON CONFLICT (schedule_key) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    checks_per_hour = EXCLUDED.checks_per_hour,
                    window_start = EXCLUDED.window_start,
                    window_end = EXCLUDED.window_end,
                    delay_min_seconds = EXCLUDED.delay_min_seconds,
                    delay_max_seconds = EXCLUDED.delay_max_seconds,
                    next_run_at = EXCLUDED.next_run_at,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = EXCLUDED.updated_at
            """), {
                'enabled': enabled,
                'checks_per_hour': checks_per_hour,
                'window_start': window_start,
                'window_end': window_end,
                'delay_min_seconds': max(1, request_delay_seconds - 1),
                'delay_max_seconds': request_delay_seconds + 1,
                'next_run_at': (
                    next_planned_round(now, window_start, window_end, checks_per_hour)
                    if enabled else None
                ),
                'user_id': getattr(current_user, 'id', None),
                'updated_at': datetime.utcnow(),
            })
        flash(
            'Automatic checks enabled for this namespace.' if enabled
            else 'Automatic checks disabled for this namespace.',
            'success',
        )
    except Exception:
        logger.exception('Failed to save Activity Monitor schedule for namespace %s', selected_namespace.slug)
        flash('Automatic check settings could not be saved.', 'error')
    finally:
        if engine is not None:
            engine.dispose()
    return redirect(url_for('web.activity_monitor_run_checks'))


@web_bp.route('/activity-monitor/run-checks/schedule-status', methods=['GET'])
@login_required
def activity_monitor_run_checks_schedule_status():
    """Return the selected namespace's current schedule times for live display."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        return jsonify({
            'enabled': False,
            'next_round': None,
            'last_round': None,
            'planned_rounds': [],
        })

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        with engine.connect() as conn:
            if not conn.execute(text(
                "SELECT to_regclass('activity_monitor_schedules') IS NOT NULL"
            )).scalar_one():
                saved = None
            else:
                saved = conn.execute(text("""
                    SELECT enabled, checks_per_hour, window_start, window_end,
                           next_run_at, last_run_at
                    FROM activity_monitor_schedules
                    WHERE schedule_key = 'activity-monitor'
                """)).mappings().first()

        now = datetime.now(ZoneInfo('Europe/Amsterdam')).replace(tzinfo=None)
        enabled = bool(saved and saved['enabled'])
        next_round = saved['next_run_at'] if enabled else None
        window_start = saved['window_start'] if saved else '08:00'
        window_end = saved['window_end'] if saved else '22:00'
        checks_per_hour = saved['checks_per_hour'] if saved else 1
        if enabled and (next_round is None or next_round <= now):
            next_round = next_planned_round(
                now, window_start, window_end, checks_per_hour
            )
        rounds = [
            round_at.strftime('%H:%M')
            for round_at in planned_rounds(
                now.date(), window_start, window_end, checks_per_hour
            )
        ]
        last_round = saved['last_run_at'] if saved else None
        return jsonify({
            'enabled': enabled,
            'next_round': next_round.strftime('%Y-%m-%d %H:%M:%S') if next_round else None,
            'last_round': last_round.strftime('%Y-%m-%d %H:%M:%S') if last_round else None,
            'planned_rounds': rounds,
        })
    except Exception:
        current_app.logger.exception(
            'Could not load live Activity Monitor schedule status for namespace %s',
            selected_namespace.slug,
        )
        return jsonify({'error': 'Schedule status is temporarily unavailable.'}), 503
    finally:
        if engine is not None:
            engine.dispose()


@web_bp.route('/activity-monitor/run-checks', methods=['GET', 'POST'])
@login_required
def activity_monitor_run_checks():
    """Show the check plan and preview a selected batch without executing it."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    summary = None
    latest_runs = []
    recent_source_checks = []
    selected_run = None
    selected_run_tasks = []
    selected_run_id = request.args.get('run_id', type=int)
    load_error = False
    history_unavailable = False
    due_plan = []
    due_plan_count = 0
    due_plan_as_of = datetime.now(ZoneInfo('Europe/Amsterdam')).date().isoformat()
    selected_plan = None
    selection_error = None
    hide_manual_panel = False
    requested_speed = 'medium'
    source_settings = {
        'firstcycling_enabled': bool(current_app.config.get('ACTIVITY_MONITOR_FC_ENABLED', False)),
        'pcs_enabled': bool(current_app.config.get('ACTIVITY_MONITOR_PCS_ENABLED', False)),
    }
    automation_settings = {
        'enabled': False,
        'checks_per_hour': 1,
        'window_start': '08:00',
        'window_end': '22:00',
        'delay_min_seconds': 59,
        'delay_max_seconds': 61,
        'request_speed': 'very_slow',
        'next_run_at': None,
        'last_run_at': None,
    }
    automation_rounds = []

    if selected_namespace is not None:
        engine = None
        try:
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                tables_ready = conn.execute(text("""
                    SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                       AND to_regclass('activity_monitor_source_checks') IS NOT NULL
                       AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                       AND to_regclass('activity_monitor_runs') IS NOT NULL
                       AND to_regclass('activity_monitor_tasks') IS NOT NULL
                       AND to_regclass('activity_monitor_task_events') IS NOT NULL
                       AND to_regclass('activity_monitor_rider_schedules') IS NOT NULL
                       AND to_regclass('activity_monitor_results') IS NOT NULL
                       AND to_regclass('activity_monitor_legacy_records') IS NOT NULL
                """)).scalar_one()
                if not tables_ready:
                    history_unavailable = True
                else:
                    schedule_table_ready = conn.execute(text(
                        "SELECT to_regclass('activity_monitor_schedules') IS NOT NULL"
                    )).scalar_one()
                    if schedule_table_ready:
                        saved_automation = conn.execute(text("""
                            SELECT enabled, checks_per_hour, window_start, window_end,
                                   delay_min_seconds, delay_max_seconds,
                                   next_run_at, last_run_at
                            FROM activity_monitor_schedules
                            WHERE schedule_key = 'activity-monitor'
                        """)).mappings().first()
                        if saved_automation:
                            automation_settings.update(dict(saved_automation))
                            saved_midpoint = (
                                int(saved_automation['delay_min_seconds'])
                                + int(saved_automation['delay_max_seconds'])
                            ) // 2
                            automation_settings['request_speed'] = min(
                                {'fast': 2, 'medium': 5, 'slow': 15, 'very_slow': 60}.items(),
                                key=lambda speed: abs(speed[1] - saved_midpoint),
                            )[0]
                    automation_rounds = [
                        round_at.strftime('%H:%M')
                        for round_at in planned_rounds(
                            date.fromisoformat(due_plan_as_of),
                            automation_settings['window_start'],
                            automation_settings['window_end'],
                            automation_settings['checks_per_hour'],
                        )
                    ]
                    def load_due_plan():
                        with engine.connect() as planning_conn:
                            planning_conn.execute(text('SET LOCAL jit = off'))
                            return due_activity_riders(
                                planning_conn, due_plan_as_of, limit=100000
                            )

                    # Avoid spending about a second compiling the correlated
                    # Needs Review count before executing it.
                    conn.execute(text('SET LOCAL jit = off'))
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        due_plan_future = executor.submit(load_due_plan)
                        summary = dict(conn.execute(text(f"""
                        SELECT COUNT(*) FILTER (WHERE eligible AND present_in_cdb) AS monitored,
                               COUNT(*) FILTER (WHERE eligible AND present_in_cdb
                                   AND NOT EXISTS (
                                       SELECT 1 FROM activity_monitor_source_checks c
                                       WHERE c.rider_id = r.id
                                   )) AS awaiting_first_check,
                               COUNT(*) FILTER (WHERE eligible AND present_in_cdb
                                   AND {_activity_monitor_attention_sql('r')}) AS needs_attention
                        FROM activity_monitor_riders r
                    """)).mappings().one())
                        due_plan_count, all_due_riders = due_plan_future.result()
                    summary['overdue_review_date'] = sum(
                        rider.get('next_research_date') < due_plan_as_of
                        for rider in all_due_riders
                        if rider.get('next_research_date')
                    )
                    due_plan = all_due_riders[:25]
                    latest_runs = [dict(item) for item in conn.execute(text("""
                        SELECT id, run_type, status, requested_count, attempted_count,
                               (SELECT COUNT(*) FROM activity_monitor_tasks t
                                WHERE t.run_id = activity_monitor_runs.id
                                  AND t.state IN ('completed', 'needs_review', 'failed')) AS completed_count,
                               (SELECT COUNT(*) FROM activity_monitor_tasks t
                                WHERE t.run_id = activity_monitor_runs.id
                                  AND t.state = 'completed') AS succeeded_count,
                               (SELECT COUNT(*) FROM activity_monitor_tasks t
                                WHERE t.run_id = activity_monitor_runs.id
                                  AND t.state = 'needs_review') AS needs_review_count,
                               (SELECT COUNT(DISTINCT resume_event.task_id)
                                FROM activity_monitor_task_events resume_event
                                JOIN activity_monitor_tasks resumed_task
                                  ON resumed_task.id = resume_event.task_id
                                WHERE resumed_task.run_id = activity_monitor_runs.id
                                  AND resume_event.notes LIKE 'Resumed after the previous background worker stopped mid-check.%'
                               ) AS resumed_count,
                               COALESCE(finished_at, started_at, created_at) AS run_at
                        FROM activity_monitor_runs
                        WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                        ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                        LIMIT 25
                    """)).mappings().all()]
                    # Keep the current run visible when the user leaves this page
                    # and returns through the Run checks navigation link.
                    if selected_run_id is None:
                        selected_run_id = conn.execute(text("""
                            SELECT id
                            FROM activity_monitor_runs
                            WHERE status IN ('queued', 'running', 'retry_wait')
                              AND (source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%')
                            ORDER BY COALESCE(started_at, created_at) DESC, id DESC
                            LIMIT 1
                        """)).scalar_one_or_none()
                    selected_run_is_valid = selected_run_id is not None and conn.execute(text("""
                        SELECT EXISTS (
                            SELECT 1 FROM activity_monitor_runs
                            WHERE id = :run_id AND (source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%')
                        )
                    """), {'run_id': selected_run_id}).scalar_one()
                    if not selected_run_is_valid:
                        selected_run_id = None
                    else:
                        selected_run = dict(conn.execute(text("""
                            SELECT id, run_type, status, requested_count, attempted_count,
                                   successful_count, needs_review_count, error_message,
                                   stop_reason,
                                   COALESCE(finished_at, started_at, created_at) AS run_at
                            FROM activity_monitor_runs WHERE id = :run_id
                        """), {'run_id': selected_run_id}).mappings().one())
                        selected_run_tasks = [dict(item) for item in conn.execute(text("""
                            SELECT t.id, t.state, t.source_scope, t.outcome, t.updated_at,
                                   t.next_retry_at,
                                   EXISTS (
                                       SELECT 1 FROM activity_monitor_task_events resume_event
                                       WHERE resume_event.task_id = t.id
                                         AND resume_event.notes LIKE 'Resumed after the previous background worker stopped mid-check.%'
                                   ) AS resumed_after_interruption,
                                   r.id AS monitor_rider_id,
                                   r.source_worlddb_rider_id AS worlddb_rider_id,
                                   r.first_name, r.last_name
                            FROM activity_monitor_tasks t
                            LEFT JOIN activity_monitor_riders r ON r.id = t.rider_id
                            WHERE t.run_id = :run_id
                            ORDER BY t.id
                        """), {'run_id': selected_run_id}).mappings().all()]
                        for task in selected_run_tasks:
                            raw_outcome = task.get('outcome') or ''
                            try:
                                parsed_outcome = json.loads(raw_outcome)
                            except (TypeError, ValueError):
                                parsed_outcome = None
                            if isinstance(parsed_outcome, dict) and parsed_outcome.get('sources'):
                                def outcome_source_status(source):
                                    status = source.get('status') or 'unknown'
                                    normalized_status = str(status).strip().casefold().replace('_', ' ')
                                    successful_status = normalized_status in {
                                        'success', 'results parsed', 'results table empty'
                                    }
                                    if successful_status and source.get('identity_result') == 'mismatch':
                                        status = 'identity_mismatch'
                                    elif successful_status and source.get('identity_result') not in (None, 'verified'):
                                        status = 'identity_not_verified'
                                    return _activity_check_status_label(status)

                                task['outcome_details'] = '; '.join(
                                    f"{(source.get('source') or 'Source').replace('_', ' ').title()}: "
                                    f"{outcome_source_status(source)}"
                                    + (f" — {source['message']}" if source.get('message') else '')
                                    for source in parsed_outcome['sources']
                                )
                                source_states = [source.get('status') for source in parsed_outcome['sources']]
                                identity_unconfirmed = any(
                                    source.get('identity_result') not in (None, 'verified')
                                    for source in parsed_outcome['sources']
                                    if str(source.get('status') or '').strip().casefold().replace('_', ' ')
                                    in {'success', 'results parsed', 'results table empty'}
                                )
                                pcs_succeeded = any(
                                    source.get('source') == 'pcs'
                                    and str(source.get('status') or '').strip().casefold().replace('_', ' ')
                                    in {'success', 'results parsed', 'results table empty'}
                                    and source.get('identity_result') == 'verified'
                                    for source in parsed_outcome['sources']
                                )
                                task['outcome_needs_review'] = (
                                    identity_unconfirmed
                                    or (
                                        not pcs_succeeded
                                        and (
                                            task.get('state') == 'needs_review'
                                            or any(
                                                state is not None
                                                and str(state).strip().casefold().replace('_', ' ')
                                                not in {'success', 'results parsed', 'results table empty'}
                                                for state in source_states
                                            )
                                        )
                                    )
                                )
                                if task['outcome_needs_review']:
                                    task['outcome_summary'] = 'This check needs a quick review.'
                                elif any(
                                    str(state or '').strip().casefold().replace('_', ' ')
                                    in {'success', 'results parsed', 'results table empty'}
                                    for state in source_states
                                ):
                                    task['outcome_summary'] = 'Check completed.'
                                else:
                                    task['outcome_summary'] = 'No check result is available yet.'
                            else:
                                task['outcome_details'] = raw_outcome
                                task['outcome_summary'] = {
                                    'queued': 'Waiting to be checked.',
                                    'running': 'This rider is being checked.',
                                    'retry_wait': 'This check will try again shortly.',
                                    'needs_review': 'This check needs a quick review.',
                                    'failed': 'This check could not be completed.',
                                    'cancelled': 'This check was stopped.',
                                }.get(task.get('state'), 'No check details are available yet.')
                            if task.get('state') == 'retry_wait' and task.get('next_retry_at') is None:
                                required_sources = task.get('source_scope') or []
                                if isinstance(required_sources, str):
                                    try:
                                        required_sources = json.loads(required_sources)
                                    except (TypeError, ValueError):
                                        required_sources = []
                                disabled_sources = [
                                    label for source, label, enabled in (
                                        ('pcs', 'PCS', source_settings['pcs_enabled']),
                                        ('firstcycling', 'FirstCycling', source_settings['firstcycling_enabled']),
                                    )
                                    if source in required_sources and not enabled
                                ]
                                task['setup_wait_message'] = (
                                    f"Waiting for {', '.join(disabled_sources)} to be enabled on the server. "
                                    "It will resume automatically after the run has been idle for about five minutes."
                                    if disabled_sources else
                                    'The required source is enabled; this check will resume automatically after the run has been idle for about five minutes.'
                                )
                        task_states = [task.get('state') for task in selected_run_tasks]
                        selected_run['completed_count'] = sum(
                            state in {'completed', 'needs_review', 'failed'}
                            for state in task_states
                        )
                        selected_run['succeeded_count'] = sum(
                            task.get('state') in {'completed', 'needs_review'}
                            and not task.get('outcome_needs_review')
                            for task in selected_run_tasks
                        )
                        selected_run['needs_review_count'] = sum(
                            bool(task.get('outcome_needs_review'))
                            for task in selected_run_tasks
                        )
                        selected_run['queued_count'] = task_states.count('queued')
                        selected_run['running_count'] = task_states.count('running')
                        selected_run['retry_wait_count'] = task_states.count('retry_wait')
                        selected_run['resumed_count'] = sum(
                            bool(task.get('resumed_after_interruption'))
                            for task in selected_run_tasks
                        )
                        selected_run['setup_wait_count'] = sum(
                            task.get('state') == 'retry_wait' and task.get('next_retry_at') is None
                            for task in selected_run_tasks
                        )
                        selected_run['failed_task_count'] = task_states.count('failed')
                        selected_run['cancelled_count'] = task_states.count('cancelled')
                        selected_run['finished_rider_ids'] = [
                            task['monitor_rider_id']
                            for task in selected_run_tasks
                            if task.get('monitor_rider_id') is not None
                            and task.get('state') in {'completed', 'needs_review'}
                        ]
                    recent_source_checks = [dict(item) for item in conn.execute(text("""
                        SELECT c.id, c.run_id, c.source, c.source_status,
                               c.coverage_status, c.identity_result, c.checked_at,
                               c.http_status, c.message,
                               r.id AS monitor_rider_id,
                               r.source_worlddb_rider_id AS worlddb_rider_id,
                               r.first_name, r.last_name,
                               (SELECT COUNT(*) FROM activity_monitor_results e
                                WHERE e.source_check_id = c.id) AS evidence_count
                        FROM activity_monitor_source_checks c
                        JOIN activity_monitor_runs run ON run.id = c.run_id
                        LEFT JOIN activity_monitor_riders r ON r.id = c.rider_id
                        WHERE (run.source_record_key LIKE 'manual-run:%' OR run.source_record_key LIKE 'scheduled-run:%')
                          AND (CAST(:run_id AS INTEGER) IS NULL OR c.run_id = :run_id)
                        ORDER BY c.checked_at DESC, c.id DESC
                        LIMIT :row_limit
                    """), {
                        'run_id': selected_run_id,
                        'row_limit': 500 if selected_run_id else 20,
                    }).mappings().all()]
                    checks_by_rider = {}
                    for check in recent_source_checks:
                        if check.get('monitor_rider_id') is not None:
                            checks_by_rider.setdefault(check['monitor_rider_id'], []).append(check)
                    for task in selected_run_tasks:
                        task['source_check_rows'] = checks_by_rider.get(task.get('monitor_rider_id'), [])

                    if request.method == 'POST':
                        requested_speed = request.form.get('request_speed', 'medium')
                        if requested_speed not in {'fast', 'medium', 'slow'}:
                            requested_speed = 'medium'
                        source_mode = (
                            'combined'
                            if current_app.config.get('ACTIVITY_MONITOR_FC_ENABLED', False)
                            else 'pcs_only'
                        )
                        recommended_selection = request.form.get('selection_mode') == 'recommended'
                        roster_bulk_selection = (
                            request.form.get('selection_origin') == 'riders_page_bulk'
                            and not recommended_selection
                        )
                        hide_preview = request.form.get('preview_action') == 'hide'
                        upcoming_count = 0
                        # A second click on the preview button submits the hide action.
                        # In that case there is intentionally no rider selection to rebuild.
                        rider_ids = []
                        if recommended_selection and not hide_preview:
                            try:
                                requested_count = int(request.form.get('recommended_count', ''))
                            except (TypeError, ValueError):
                                requested_count = 0
                            if not 1 <= requested_count <= 25:
                                selection_error = 'Choose a number between 1 and 25 riders.'
                                rider_ids = []
                            else:
                                _, recommended_rows = due_activity_riders(
                                    conn, due_plan_as_of, limit=requested_count,
                                    source_mode=source_mode, include_upcoming=True,
                                )
                                rider_ids = [row['monitor_rider_id'] for row in recommended_rows]
                                upcoming_count = sum(not row.get('is_due') for row in recommended_rows)
                                if not rider_ids:
                                    selection_error = 'No eligible riders are due or scheduled next for a check.'
                        elif not recommended_selection:
                            raw_ids = request.form.getlist('rider_ids')
                            try:
                                rider_ids = list(dict.fromkeys(int(value) for value in raw_ids))
                            except (TypeError, ValueError):
                                rider_ids = []
                                selection_error = 'The selection contained an invalid rider.'

                        if not selection_error and not rider_ids and not hide_preview:
                            selection_error = 'Select at least one rider from the Riders page.'
                        elif (
                            not selection_error
                            and not hide_preview
                            and len(rider_ids) > (250 if roster_bulk_selection else 25)
                        ):
                            max_preview_riders = 250 if roster_bulk_selection else 25
                            selection_error = (
                                f'A preview can include up to {max_preview_riders} riders. '
                                'No check was started.'
                            )

                        if not selection_error and not hide_preview:
                            id_params = {f'rider_id_{i}': rider_id for i, rider_id in enumerate(rider_ids)}
                            id_placeholders = ', '.join(f':rider_id_{i}' for i in range(len(rider_ids)))
                            selected_rows = conn.execute(text(f"""
                                SELECT r.id AS monitor_rider_id,
                                       r.source_worlddb_rider_id AS worlddb_rider_id,
                                       r.first_cycling_id, r.team_id, r.first_name, r.last_name,
                                       NULLIF(r.team_name, '') AS team_name,
                                       (
                                           SELECT COALESCE(NULLIF(p.approved_profile_path, ''),
                                                           NULLIF(p.profile_path, ''),
                                                           NULLIF(p.override_url, ''))
                                           FROM activity_monitor_profile_links p
                                           WHERE p.rider_id = r.id AND p.is_current
                                             AND p.identity_generation = r.identity_generation
                                           ORDER BY p.id DESC LIMIT 1
                                       ) AS pcs_profile_path,
                                       (SELECT MAX(c.checked_at)::date
                                        FROM activity_monitor_source_checks c
                                        WHERE c.rider_id = r.id
                                          AND c.identity_generation = r.identity_generation
                                       ) AS last_check_date,
                                       (SELECT s.schedule_data->>'next_research_date'
                                        FROM activity_monitor_rider_schedules s
                                        WHERE s.rider_id = r.id
                                        ORDER BY s.reference_date DESC, s.recorded_at DESC, s.id DESC
                                        LIMIT 1
                                       ) AS scheduled_date
                                FROM activity_monitor_riders r
                                WHERE r.id IN ({id_placeholders})
                                  AND r.eligible AND r.present_in_cdb
                                ORDER BY r.last_name, r.first_name, r.id
                            """), id_params).mappings().all()
                            if len(selected_rows) != len(rider_ids):
                                selection_error = 'One or more selected riders are no longer eligible in this namespace. Review the Riders page and try again.'
                            else:
                                selected_rows_by_id = {
                                    row['monitor_rider_id']: dict(row)
                                    for row in selected_rows
                                }
                                selected_riders = [
                                    selected_rows_by_id[rider_id]
                                    for rider_id in rider_ids
                                ]
                                for rider in selected_riders:
                                    profile_path = str(rider.get('pcs_profile_path') or '').strip()
                                    if profile_path.startswith(('https://', 'http://')):
                                        rider['pcs_profile_url'] = profile_path.replace('http://', 'https://', 1)
                                    elif profile_path:
                                        profile_path = profile_path.strip('/')
                                        if not profile_path.startswith('rider/'):
                                            profile_path = f'rider/{profile_path}'
                                        if not profile_path.endswith('/road'):
                                            profile_path = f'{profile_path}/road'
                                        rider['pcs_profile_url'] = f'https://www.procyclingstats.com/{profile_path}'
                                    else:
                                        _, rider['pcs_profile_url'] = _assumed_pcs_profile(
                                            rider.get('first_name'), rider.get('last_name')
                                        )
                                    rider['firstcycling_profile_url'] = (
                                        f"https://firstcycling.com/rider.php?r={int(rider['first_cycling_id'])}"
                                        if rider.get('first_cycling_id') is not None else None
                                    )
                                    rider['source_scope'] = []
                                    if (
                                        source_mode == 'combined'
                                        and rider['team_id'] != 119
                                        and rider['first_cycling_id'] is not None
                                    ):
                                        rider['source_scope'].append('FirstCycling')
                                    # PCS always receives the saved or name-based candidate.
                                    # The worker verifies identity before saving activity.
                                    rider['source_scope'].append('PCS')
                                selected_plan = {
                                    'source_mode': source_mode,
                                    'selection_strategy': 'recommended' if recommended_selection else 'manual',
                                    'selection_origin': 'riders_page_bulk' if roster_bulk_selection else 'manual',
                                    'requested_count': requested_count if recommended_selection else len(rider_ids),
                                    'upcoming_count': upcoming_count,
                                    'request_speed': requested_speed,
                                    'riders': selected_riders,
                                }
                                hide_manual_panel = roster_bulk_selection
        except Exception:
            logger.exception('Failed to load Activity Monitor check plan for namespace %s', selected_namespace.slug)
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    return render_template(
        'activity_monitor_run_checks.html',
        selected_namespace=selected_namespace,
        summary=summary,
        latest_runs=latest_runs,
        recent_source_checks=recent_source_checks,
        selected_run_id=selected_run_id,
        selected_run=selected_run,
        selected_run_tasks=selected_run_tasks,
        due_plan=due_plan,
        due_plan_count=due_plan_count,
        due_plan_as_of=due_plan_as_of,
        load_error=load_error,
        history_unavailable=history_unavailable,
        selected_plan=selected_plan,
        requested_speed=requested_speed,
        selection_error=selection_error,
        source_settings=source_settings,
        automation_settings=automation_settings,
        automation_rounds=automation_rounds,
        hide_manual_panel=hide_manual_panel,
    )


@web_bp.route('/activity-monitor/run-checks/queue', methods=['POST'])
@login_required
def activity_monitor_queue_checks():
    """Create shared run/task records, then dispatch the namespace run to Celery."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before starting checks.', 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))

    source_mode = (
        'combined'
        if current_app.config.get('ACTIVITY_MONITOR_FC_ENABLED', False)
        else 'pcs_only'
    )

    required_sources = {'pcs'} if source_mode == 'pcs_only' else {'pcs', 'firstcycling'}
    enabled_sources = {
        'pcs': bool(current_app.config.get('ACTIVITY_MONITOR_PCS_ENABLED', False)),
        'firstcycling': bool(current_app.config.get('ACTIVITY_MONITOR_FC_ENABLED', False)),
    }
    disabled_sources = sorted(source for source in required_sources if not enabled_sources[source])
    if disabled_sources:
        labels = {'firstcycling': 'FirstCycling', 'pcs': 'PCS'}
        flash(
            'Checks were not started. Enable the required source first: '
            + ', '.join(labels[source] for source in disabled_sources) + '.',
            'warning',
        )
        return redirect(url_for('web.activity_monitor_run_checks'))

    start_recommended = request.form.get('start_recommended') == '1'
    roster_bulk_selection = (
        request.form.get('selection_origin') == 'riders_page_bulk'
        and not start_recommended
    )
    max_manual_riders = 250 if roster_bulk_selection else 25
    raw_ids = request.form.getlist('rider_ids')
    request_speed = request.form.get('request_speed', 'medium')
    speed_delays = {'fast': 2, 'medium': 5, 'slow': 15}
    if request_speed not in speed_delays:
        request_speed = 'medium'
    requested_count = None
    if start_recommended:
        try:
            requested_count = int(request.form.get('recommended_count', ''))
        except (TypeError, ValueError):
            requested_count = 0
        if not 1 <= requested_count <= 25:
            flash('Choose a number between 1 and 25 riders.', 'warning')
            return redirect(url_for('web.activity_monitor_run_checks'))
        rider_ids = []
    else:
        try:
            rider_ids = list(dict.fromkeys(int(value) for value in raw_ids))
        except (TypeError, ValueError):
            flash('The selection contained an invalid rider.', 'warning')
            return redirect(url_for('web.activity_monitor_run_checks'))
        if not rider_ids or len(rider_ids) > max_manual_riders:
            flash(
                f'Select between 1 and {max_manual_riders} riders for a manual check run.',
                'warning',
            )
            return redirect(url_for('web.activity_monitor_run_checks'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        with engine.connect() as conn:
            queue_tables_ready = conn.execute(text("""
                SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                   AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                   AND to_regclass('activity_monitor_runs') IS NOT NULL
                   AND to_regclass('activity_monitor_tasks') IS NOT NULL
                   AND to_regclass('activity_monitor_task_events') IS NOT NULL
            """)).scalar_one()
            if queue_tables_ready and start_recommended:
                _, recommended_rows = due_activity_riders(
                    conn,
                    datetime.now(ZoneInfo('Europe/Amsterdam')).date(),
                    limit=requested_count,
                    source_mode=source_mode,
                    include_upcoming=True,
                )
                rider_ids = [row['monitor_rider_id'] for row in recommended_rows]
        if not queue_tables_ready:
            flash('The shared Activity Monitor queue is not installed in this namespace.', 'warning')
            return redirect(url_for('web.activity_monitor_run_checks'))
        if not rider_ids:
            flash('No eligible riders are due or scheduled next for a check.', 'warning')
            return redirect(url_for('web.activity_monitor_run_checks'))

        queued = enqueue_activity_monitor_run(
            engine,
            riders=rider_ids,
            max_riders=max_manual_riders,
            source_mode=source_mode,
            initiated_by_user_id=current_user.id,
            run_settings={
                'request_speed': request_speed,
                'request_delay_seconds': speed_delays[request_speed],
            },
        )
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))
    except Exception:
        logger.exception('Could not queue Activity Monitor checks for namespace %s', selected_namespace.slug)
        flash('The check run could not be saved. No source request was started.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks'))
    finally:
        if engine is not None:
            engine.dispose()

    try:
        run_activity_monitor_run_task.delay(selected_namespace.slug, queued['run_id'])
    except Exception:
        logger.exception(
            'Activity Monitor run %s was saved but could not be dispatched to Celery',
            queued['run_id'],
        )
        flash(
            f"Run {queued['run_id']} was saved in the shared queue, but the background worker did not start.",
            'warning',
        )
        return redirect(url_for(
            'web.activity_monitor_run_checks',
            run_id=queued['run_id'],
            **({'from_riders': '1'} if roster_bulk_selection else {}),
        ))

    flash(
        f"Run {queued['run_id']} was queued for {len(queued['task_ids'])} riders.",
        'success',
    )
    return redirect(url_for(
        'web.activity_monitor_run_checks',
        run_id=queued['run_id'],
        **({'from_riders': '1'} if roster_bulk_selection else {}),
    ))


@web_bp.route('/activity-monitor/run-checks/<int:run_id>/stop', methods=['POST'])
@login_required
def activity_monitor_stop_run(run_id):
    """Stop a manual run after its current source request has finished."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before stopping checks.', 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        result = request_activity_monitor_run_stop(
            engine,
            run_id,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))
    except Exception:
        logger.exception(
            'Could not request Activity Monitor run %s to stop in namespace %s',
            run_id,
            selected_namespace.slug,
        )
        flash('The run could not be stopped. Its current work may still be running.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))
    finally:
        if engine is not None:
            engine.dispose()

    if result['changed']:
        flash(
            f"Stop requested. The current source request will finish; "
            f"{result['cancelled_count']} rider check(s) that had not started were cancelled.",
            'success',
        )
    else:
        flash(f"This run is already {result['status'].replace('_', ' ')}.", 'info')
    return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))


@web_bp.route('/activity-monitor/run-checks/<int:run_id>/resume', methods=['POST'])
@login_required
def activity_monitor_resume_run(run_id):
    """Resume the remaining rider checks from a stopped monitor run."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before resuming checks.', 'warning')
        return redirect(url_for('web.activity_monitor_run_checks'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        result = resume_activity_monitor_run(
            engine,
            run_id,
            actor_user_id=current_user.id,
        )
        if not result['changed']:
            flash('There are no stopped rider checks left to resume.', 'info')
            return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))
        run_activity_monitor_run_task.delay(selected_namespace.slug, run_id)
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))
    except Exception:
        logger.exception(
            'Could not request Activity Monitor run %s to resume in namespace %s',
            run_id,
            selected_namespace.slug,
        )
        flash('The background worker could not be reached. The saved run and its results were not changed.', 'error')
        return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))
    finally:
        if engine is not None:
            engine.dispose()

    flash(f"Run {run_id} resumed. {result['requeued_count']} remaining rider check(s) are back in the queue; completed checks were left as they were.", 'success')
    return redirect(url_for('web.activity_monitor_run_checks', run_id=run_id))

@web_bp.route('/activity-monitor/riders/<int:rider_id>')
@login_required
def activity_monitor_rider_detail(rider_id):
    """Show the imported evidence for a rider without changing monitor data."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    back_params = {
        key: (request.args.get(key, type=str) or '').strip()
        for key in ('q', 'team', 'country', 'division', 'status', 'sort', 'direction')
    }
    back_params.update({
        'per_page': request.args.get('per_page', type=int),
        'page': request.args.get('page', type=int),
    })
    back_url = url_for('web.activity_monitor_riders', **{
        key: value for key, value in back_params.items() if value is not None and value != ''
    })
    if selected_namespace is None:
        return render_template('activity_monitor_rider_detail.html', selected_namespace=None, back_url=back_url)

    engine = None
    load_error = False
    rider = None
    checks, results, profile_links, alerts, research_checks = [], [], [], [], []
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT r.id, r.source_worlddb_rider_id AS worlddb_rider_id,
                       r.identity_generation, r.first_cycling_id,
                       r.first_name, r.last_name, r.birth_date,
                       {_activity_monitor_country_sql()} AS nationality,
                       NULLIF(r.team_name, '') AS team_name,
                       COALESCE(NULLIF(d.constant, ''), d.id::text, '—') AS division_name,
                       r.identity_status, r.identity_hold,
                       r.identity_hold_reason, r.eligible, r.present_in_cdb
                FROM activity_monitor_riders r
                LEFT JOIN team t ON t.id = r.team_id
                LEFT JOIN division d ON d.id = t.division_id
                WHERE r.id = :rider_id
            """), {'rider_id': rider_id}).mappings().first()
            if row is not None:
                rider = dict(row)
                race_days_row = conn.execute(text(f"""
                    SELECT CASE WHEN BOOL_OR(
                                      LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                      AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                          IN ('success', 'results parsed', 'results table empty')
                                      AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                      AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                      AND (
                                          regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                          OR c.source_record_key LIKE 'source_checks:%'
                                      )
                                  )
                             THEN COUNT(DISTINCT e.result_date) FILTER (
                                      WHERE LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                                        AND e.identity_verified
                                        AND (
                                            e.participation_status IN ('confirmed_participation', 'confirmed')
                                            OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                        )
                                        AND e.result_date >= date_trunc('year', CURRENT_DATE)::date
                                        AND e.result_date <= CURRENT_DATE
                                        AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                            IN ('success', 'results parsed', 'results table empty')
                                        AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                        AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                        AND (
                                            regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                            OR c.source_record_key LIKE 'source_checks:%'
                                        )
                                  )::integer
                             ELSE NULL END AS pcs_race_days_current_year
                    FROM activity_monitor_source_checks c
                    LEFT JOIN activity_monitor_results e
                      ON e.source_check_id = c.id
                     AND e.rider_id = c.rider_id
                     AND e.identity_generation = c.identity_generation
                    WHERE c.rider_id = :rider_id
                      AND c.identity_generation = :identity_generation
                      AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'pcs_html', 'procyclingstats')
                """), {
                    'rider_id': rider_id,
                    'identity_generation': rider['identity_generation'],
                }).mappings().one()
                rider.update(race_days_row)
                checks = [dict(item) for item in conn.execute(text("""
                    SELECT id, source_record_key, source, source_status, coverage_status, identity_result,
                           checked_at, season, http_status, request_path, message,
                           identity_generation,
                           (SELECT COUNT(*) FROM activity_monitor_results e
                            WHERE e.source_check_id = activity_monitor_source_checks.id) AS results_found
                    FROM activity_monitor_source_checks WHERE rider_id = :rider_id
                    ORDER BY checked_at DESC, id DESC LIMIT 100
                """), {'rider_id': rider_id}).mappings().all()]
                research_rows = conn.execute(text("""
                    SELECT id, source_record_key, occurred_at, record_data
                    FROM activity_monitor_legacy_records
                    WHERE rider_id = :rider_id
                      AND record_type = 'research_findings'
                      AND COALESCE(record_data->>'identity_generation', '0')::integer = :generation
                    ORDER BY occurred_at DESC NULLS LAST, id DESC
                    LIMIT 100
                """), {
                    'rider_id': rider_id,
                    'generation': rider['identity_generation'],
                }).mappings().all()
                for research_row in research_rows:
                    record_data = research_row['record_data'] or {}
                    raw_sources = record_data.get('checked_sources') or []
                    if isinstance(raw_sources, str):
                        raw_sources = raw_sources.split(';')
                    safe_sources = []
                    for raw_source in raw_sources if isinstance(raw_sources, list) else []:
                        parsed_source = urllib.parse.urlsplit(str(raw_source).strip())
                        if (parsed_source.scheme.casefold() in {'http', 'https'}
                                and parsed_source.hostname
                                and parsed_source.username is None
                                and parsed_source.password is None):
                            safe_sources.append(urllib.parse.urlunsplit(parsed_source))
                    research_checks.append({
                        'id': research_row['id'],
                        'source_record_key': research_row['source_record_key'],
                        'research_date': record_data.get('research_date') or research_row['occurred_at'],
                        'check_quality': record_data.get('check_quality') or 'incomplete',
                        'checked_sources': safe_sources,
                        'race_basis': record_data.get('race_basis') or '',
                        'research_notes': record_data.get('research_notes') or '',
                        'newer_evidence': bool(record_data.get('newer_evidence')),
                    })
                results = [dict(item) for item in conn.execute(text("""
                    SELECT source_check_id, source, race_id, edition_id, stage_id,
                           race_name, race_category, race_url, result_date,
                           stage_number, stage_name, row_type, result_position,
                           gc_position, participation_status, identity_verified,
                           identity_generation
                    FROM activity_monitor_results WHERE rider_id = :rider_id
                    ORDER BY result_date DESC NULLS LAST, id DESC LIMIT 250
                """), {'rider_id': rider_id}).mappings().all()]
                profile_links = [dict(item) for item in conn.execute(text("""
                    SELECT profile_path, override_url, approved_profile_path,
                           link_source, link_status, is_current,
                           checked_at, check_result, profile_name, profile_birth_date,
                           profile_nationality, identity_reason, message,
                           checked_url, http_status, approved_pcs_birth_date,
                           approved_worlddb_birth_date, approved_at
                    FROM activity_monitor_profile_links WHERE rider_id = :rider_id
                    ORDER BY is_current DESC,
                             COALESCE(verified_at, checked_at, created_at) DESC NULLS LAST,
                             id DESC LIMIT 30
                """), {'rider_id': rider_id}).mappings().all()]
                alerts = [dict(item) for item in conn.execute(text("""
                    SELECT created_at, reason, status, resolution, resolved_at
                    FROM activity_monitor_identity_alerts WHERE rider_id = :rider_id
                    ORDER BY created_at DESC, id DESC LIMIT 30
                """), {'rider_id': rider_id}).mappings().all()]
    except Exception:
        logger.exception('Failed to load Activity Monitor rider detail %s', rider_id)
        load_error = True
    finally:
        if engine is not None:
            engine.dispose()

    if rider is None and not load_error:
        abort(404)

    def pcs_profile_url(raw_value):
        raw_value = (raw_value or '').strip()
        parsed = urllib.parse.urlsplit(raw_value if '://' in raw_value else '')
        path = parsed.path if parsed.scheme else ('/' + raw_value.lstrip('/')) if raw_value else ''
        if parsed.scheme and parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            return None
        return urllib.parse.urlunsplit(('https', 'www.procyclingstats.com', path, '', '')) if path.startswith('/rider/') else None

    def race_result_url(raw_value):
        raw_value = (raw_value or '').strip()
        if not raw_value:
            return None
        parsed = urllib.parse.urlsplit(raw_value)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {
            'www.procyclingstats.com', 'procyclingstats.com',
            'www.firstcycling.com', 'firstcycling.com',
        }:
            return None
        host = 'www.procyclingstats.com' if parsed.hostname.endswith('procyclingstats.com') else 'www.firstcycling.com'
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ''))

    if rider:
        current_link = next((link for link in profile_links if link.get('is_current')), None)
        current_profile_path = None
        if current_link:
            current_profile_path = (
                current_link.get('approved_profile_path')
                or current_link.get('override_url')
                or current_link.get('profile_path')
            )
        rider['pcs_edit_value'] = current_profile_path or ''
        rider['pcs_url'] = pcs_profile_url(rider['pcs_edit_value'])
        if not rider['pcs_url']:
            _, assumed_pcs_url = _assumed_pcs_profile(
                rider.get('first_name'), rider.get('last_name')
            )
            rider['pcs_url'] = assumed_pcs_url
        rider['pcs_has_override'] = bool(
            current_link and (
                current_link.get('link_source') == 'manual_override'
                or current_link.get('override_url')
            )
        )
        rider['pcs_profile_mismatch'] = bool(
            current_link and (
                str(current_link.get('check_result') or '').casefold() in {
                    'identity_mismatch', 'mismatch', 'rider_details_dont_match',
                    'ambiguous', 'unavailable',
                }
                or str(current_link.get('identity_reason') or '').strip()
            )
        )
        rider['pcs_profile_approval_matches'] = bool(
            current_link
            and current_link.get('approved_profile_path')
            and current_link.get('approved_pcs_birth_date')
            and current_link.get('approved_worlddb_birth_date') == rider.get('birth_date')
            and current_link.get('profile_birth_date') == current_link.get('approved_pcs_birth_date')
        )
        if rider['pcs_profile_approval_matches']:
            rider['pcs_profile_mismatch'] = False
    else:
        current_link = None
    for link in profile_links:
        link['display_profile_path'] = (
            link.get('approved_profile_path')
            or link.get('override_url')
            or link.get('profile_path')
        )
        link['pcs_url'] = pcs_profile_url(link['display_profile_path'])
    for result in results:
        result['safe_race_url'] = race_result_url(result.get('race_url'))

    checks_by_id = {check['id']: check for check in checks}

    def normalized_activity_source(value):
        return re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().casefold()).strip('_')

    firstcycling_source_aliases = {'firstcycling', 'fc_json', 'firstcycling_json'}
    pcs_source_aliases = {'pcs', 'pcs_html', 'procyclingstats'}

    def is_firstcycling_source(value):
        return normalized_activity_source(value) in firstcycling_source_aliases

    # Imported Arie Tool checks predate the online check identity field. The
    # importer preserves verified result-level identity evidence, so show that
    # evidence explicitly instead of presenting the check's legacy NULL state
    # as if the imported rider/results had never been verified.
    for check in checks:
        normalized_source = normalized_activity_source(check.get('source'))
        if normalized_source in pcs_source_aliases:
            check['source_display'] = 'PCS'
        elif is_firstcycling_source(check.get('source')):
            check['source_display'] = 'FirstCycling'
        else:
            check['source_display'] = (str(check.get('source') or 'unknown').replace('_', ' ').title())
        imported_verified_result = (
            str(check.get('source_record_key') or '').startswith('source_checks:')
            and str(check.get('source') or '').strip().casefold() in {'pcs', 'pcs_html', 'procyclingstats'}
            and str(check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
            and any(
                result.get('source_check_id') == check.get('id')
                and result.get('identity_verified')
                for result in results
            )
        )
        check['identity_result_display'] = (
            'verified_from_imported_results' if imported_verified_result
            else check.get('identity_result')
        )
        check['source_status_display'] = check.get('source_status')
        if str(check.get('source_status') or '').strip().casefold().replace('_', ' ') \
                in {'success', 'results parsed', 'results table empty'}:
            if check.get('identity_result') == 'mismatch':
                check['source_status_display'] = 'identity_mismatch'
            elif check.get('identity_result') not in {'verified', 'verified_from_imported_results', None}:
                check['source_status_display'] = 'identity_not_verified'

    def is_road_path(value):
        raw = str(value or '').strip()
        path = urllib.parse.urlsplit(raw).path if '://' in raw else raw
        return path.rstrip('/').casefold().endswith('/road')

    def is_pcs_check(check):
        return bool(
            check
            and str(check.get('source') or '').strip().casefold()
            in {'pcs', 'pcs_html', 'procyclingstats'}
        )

    def is_pcs_result(result):
        source_check = checks_by_id.get(result.get('source_check_id'))
        return (
            str(result.get('source') or '').strip().casefold()
            in {'pcs', 'pcs_html', 'procyclingstats'}
            or is_pcs_check(source_check)
        )

    def activity_exclusion_reason(result):
        if result.get('result_date') and result['result_date'] > date.today():
            return 'Future-dated result · excluded from activity'
        source_check = checks_by_id.get(result.get('source_check_id'))
        source = str(result.get('source') or '').casefold()
        is_pcs_result = source in {'pcs', 'pcs_html', 'procyclingstats'} or is_pcs_check(source_check)
        is_imported_arie_evidence = bool(
            source_check
            and str(source_check.get('source_record_key') or '').startswith('source_checks:')
            and str(source_check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
        )
        if is_pcs_result and not (
            is_pcs_check(source_check)
            and str(source_check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
            and (is_road_path(source_check.get('request_path')) or is_imported_arie_evidence)
        ):
            return 'PCS history · original /road check cannot be verified'
        return None

    for result in results:
        result['activity_exclusion_reason'] = activity_exclusion_reason(result)
        if not result.get('race_category'):
            category_match = re.search(r'\((C[1-4]|CX)\)', result.get('race_name') or '', re.I)
            result['race_category_display'] = category_match.group(1).upper() if category_match else '—'
        else:
            result['race_category_display'] = result['race_category']

    for check in checks:
        check['message_display'] = check.get('message')

    confirmed_statuses = {'confirmed_participation', 'confirmed'}

    def counts_as_confirmed_activity(result):
        return (
            result.get('participation_status') in confirmed_statuses
            or str(result.get('result_position') or '').strip().upper() == 'DSQ'
        )
    latest_pcs_check = next((
        check for check in checks
        if check.get('identity_generation') == rider.get('identity_generation')
        and str(check.get('source') or '').strip().casefold() in {'pcs', 'pcs_html', 'procyclingstats'}
    ), None)
    # Only the latest PCS check may keep a mismatch alert open. Historical
    # mismatches must not reappear after a newer check confirms the profile.
    pcs_mismatch_check = (
        latest_pcs_check
        if latest_pcs_check
        and latest_pcs_check.get('identity_result') == 'mismatch'
        and latest_pcs_check.get('http_status') == 200
        and latest_pcs_check.get('message')
        else None
    )
    pcs_check_birth_date = None
    if pcs_mismatch_check:
        date_match = re.search(r"PCS '?(\d{4}-\d{2}-\d{2})'?", str(pcs_mismatch_check['message']))
        if date_match:
            try:
                pcs_check_birth_date = date.fromisoformat(date_match.group(1))
            except ValueError:
                pcs_check_birth_date = None
    if rider and latest_pcs_check:
        current_pcs_path = urllib.parse.urlsplit(
            rider['pcs_edit_value'] if '://' in rider['pcs_edit_value']
            else f"https://www.procyclingstats.com/{str(rider['pcs_edit_value']).lstrip('/')}"
        ).path.rstrip('/').casefold()
        checked_pcs_path = str(latest_pcs_check.get('request_path') or '').rstrip('/').casefold()
        if current_pcs_path == checked_pcs_path:
            if latest_pcs_check.get('identity_result') == 'verified':
                # A successful check against the currently saved PCS page
                # resolves any older profile-link mismatch state.
                rider['pcs_profile_mismatch'] = False
                rider['pcs_profile_review_message'] = None
                rider['pcs_profile_approval_available'] = False
            elif (not rider.get('pcs_profile_approval_matches')
                    and latest_pcs_check.get('identity_result') in {
                'mismatch', 'ambiguous', 'unavailable'
            }):
                rider['pcs_profile_mismatch'] = True
                rider['pcs_profile_review_message'] = latest_pcs_check.get('message')
    if rider and pcs_mismatch_check and pcs_check_birth_date:
        rider['pcs_profile_check_birth_date'] = pcs_check_birth_date
        rider['pcs_profile_check_path'] = pcs_mismatch_check.get('request_path')
        rider['pcs_profile_review_message'] = pcs_mismatch_check.get('message')
        if not rider.get('pcs_profile_approval_matches'):
            rider['pcs_profile_mismatch'] = True
    if rider and current_link:
        selected_path = urllib.parse.urlsplit(rider.get('pcs_url') or '').path.rstrip('/').casefold()
        checked_path = urllib.parse.urlsplit(current_link.get('checked_url') or '').path.rstrip('/').casefold()
        checked_mismatch_path = str(rider.get('pcs_profile_check_path') or '').rstrip('/').casefold()
        rider['pcs_profile_approval_available'] = bool(
            not rider.get('identity_hold')
            and not rider.get('pcs_profile_approval_matches')
            and (
                current_link.get('check_result') in {'identity_mismatch', 'mismatch', 'rider_details_dont_match'}
                or (
                    pcs_mismatch_check
                    and rider.get('pcs_profile_check_birth_date')
                )
            )
            and (
                current_link.get('http_status') == 200
                or (pcs_mismatch_check and pcs_mismatch_check.get('http_status') == 200)
            )
            and selected_path.endswith('/road')
            and (selected_path == checked_path or selected_path == checked_mismatch_path)
            and rider.get('birth_date')
            and (
                (current_link.get('profile_birth_date')
                 and rider.get('birth_date') != current_link.get('profile_birth_date'))
                or (rider.get('pcs_profile_check_birth_date')
                    and rider.get('birth_date') != rider.get('pcs_profile_check_birth_date'))
            )
        )
    elif rider:
        rider['pcs_profile_approval_available'] = bool(
            not rider.get('identity_hold')
            and not rider.get('pcs_profile_approval_matches')
            and pcs_mismatch_check
            and pcs_check_birth_date
            and rider.get('birth_date')
            and rider.get('birth_date') != pcs_check_birth_date
            and rider.get('pcs_url')
            and urllib.parse.urlsplit(rider['pcs_url']).path.rstrip('/').casefold()
                == str(pcs_mismatch_check.get('request_path') or '').rstrip('/').casefold()
        )
    latest_firstcycling_check = next((
        check for check in checks
        if check.get('identity_generation') == rider.get('identity_generation')
        and is_firstcycling_source(check.get('source'))
    ), None)

    def normalized_race_identity(value):
        decomposed = unicodedata.normalize('NFKD', str(value or ''))
        ascii_name = decomposed.encode('ascii', 'ignore').decode('ascii').casefold()
        tokens = re.findall(r'[a-z0-9]+', ascii_name)
        return ' '.join(token for token in tokens if token not in {'elite', 'men', 'women'})

    def race_identities_match(left, right):
        left_tokens = normalized_race_identity(left).split()
        right_tokens = normalized_race_identity(right).split()
        if not left_tokens or not right_tokens:
            return False
        if left_tokens == right_tokens:
            return True
        shorter, longer = sorted((left_tokens, right_tokens), key=len)
        if len(shorter) < 2:
            return False
        return any(
            longer[index:index + len(shorter)] == shorter
            for index in range(len(longer) - len(shorter) + 1)
        )

    def result_kind(result):
        stage_number = result.get('stage_number')
        stage_name = ' '.join(str(result.get('stage_name') or '').split()).casefold()
        if stage_number is None:
            stage_match = re.search(r'\b(?:stage|prologue)\s+(\d+)\b', stage_name, re.I)
            stage_number = int(stage_match.group(1)) if stage_match else None
        if stage_number is not None or str(result.get('row_type') or '').casefold() == 'stage_result':
            return f'stage:{stage_number}' if stage_number is not None else f'stage:{stage_name}'
        category = str(result.get('race_category_display') or result.get('race_category') or '').strip()
        if (
            re.match(r'^2(?:\.|$)', category)
            and str(result.get('row_type') or '').casefold()
            in {'classification', 'overall', 'gc', 'general_classification'}
        ):
            return 'general_classification'
        return 'race_result'

    # A PCS result replaces only the matching race result type on that date.
    # In stage races, preserve both a final-stage result and a separate GC.
    current_generation = rider.get('identity_generation')
    pcs_primary_results = [
        result
        for result in results
        if result.get('result_date')
        and result.get('identity_generation') == current_generation
        and is_pcs_result(result)
    ]
    grouped_results = {}
    for result in results:
        if (
            result.get('identity_generation') == current_generation
            and not is_pcs_result(result)
            and (
                is_firstcycling_source(result.get('source'))
                or is_firstcycling_source(
                    (checks_by_id.get(result.get('source_check_id')) or {}).get('source')
                )
            )
            and any(
                candidate.get('result_date') == result.get('result_date')
                and result_kind(candidate) == result_kind(result)
                and race_identities_match(candidate.get('race_name'), result.get('race_name'))
                for candidate in pcs_primary_results
            )
        ):
            continue
        participation = str(result.get('participation_status') or 'unknown').casefold()
        is_disqualified = str(result.get('result_position') or '').strip().upper() == 'DSQ'
        if participation in confirmed_statuses or is_disqualified:
            participation = 'confirmed'
        race_identity = ' '.join(str(result.get('race_name') or '').split()).casefold()
        stage_identity = ' '.join(str(result.get('stage_name') or '').split()).casefold()
        normalized_identity = normalized_race_identity(result.get('race_name'))
        kind = result_kind(result)
        is_firstcycling_result = (
            is_firstcycling_source(result.get('source'))
            or is_firstcycling_source(
                (checks_by_id.get(result.get('source_check_id')) or {}).get('source')
            )
        )
        if is_firstcycling_result:
            # Imported fc_json rows and new checks come from the same feed.
            # Historical and current records can carry different internal IDs
            # or row-type labels, so match the actual event/day/stage and only
            # collapse them when their recorded result and participation agree.
            stage_number = result.get('stage_number')
            if stage_number is None:
                stage_match = re.search(
                    r'\b(?:stage|prologue)\s+(\d+)\b', stage_identity, re.I
                )
                stage_number = int(stage_match.group(1)) if stage_match else None
            stage_identity = (
                f"stage:{stage_number}"
                if stage_number is not None
                else stage_identity
            )
            key = (
                'firstcycling', result.get('result_date'), normalized_identity, kind,
                str(result.get('result_position') or '').strip().casefold(),
                str(result.get('gc_position') or '').strip().casefold(),
                participation,
                bool(result.get('identity_verified')),
                result.get('activity_exclusion_reason'),
            )
        else:
            key = (
                result.get('result_date'), race_identity, result.get('race_category_display'),
                result.get('stage_number'), stage_identity, result.get('row_type'),
                result.get('result_position'), result.get('gc_position'), participation,
                bool(result.get('identity_verified')), result.get('activity_exclusion_reason'),
            )
        grouped = grouped_results.get(key)
        if grouped is None:
            grouped = dict(result)
            grouped['evidence_sources'] = []
            grouped['evidence_count'] = 0
            grouped_results[key] = grouped
        source_name = {
            'pcs': 'PCS',
            'pcs_html': 'PCS',
            'procyclingstats': 'PCS',
            'fc_json': 'FirstCycling',
            'firstcycling': 'FirstCycling',
            'firstcycling_json': 'FirstCycling',
        }.get(normalized_activity_source(result.get('source')))
        if is_firstcycling_result:
            source_name = 'FirstCycling'
        if source_name is None:
            source_name = (str(result.get('source') or 'Unknown').replace('_', ' ').title())
        if source_name not in grouped['evidence_sources']:
            grouped['evidence_sources'].append(source_name)
        grouped['evidence_count'] += 1
        if participation == 'confirmed':
            grouped['participation_display'] = 'Confirmed'
    display_results = list(grouped_results.values())

    activity_candidates = [
        result for result in results
        if result.get('identity_generation') == current_generation
        and result.get('identity_verified')
        and counts_as_confirmed_activity(result)
        and not result.get('activity_exclusion_reason')
        and result.get('result_date')
    ]
    latest_activity_date = max(
        (result['result_date'] for result in activity_candidates),
        default=None,
    )
    latest_activity_on_date = [
        result for result in activity_candidates
        if result['result_date'] == latest_activity_date
    ]
    latest_activity = next((
        result for result in latest_activity_on_date if is_pcs_result(result)
    ), None) or next(iter(latest_activity_on_date), None)
    latest_activity_days_ago = (
        (date.today() - latest_activity['result_date']).days
        if latest_activity else None
    )

    return render_template(
        'activity_monitor_rider_detail.html',
        selected_namespace=selected_namespace,
        back_url=back_url,
        rider=rider,
        checks=checks,
        results=display_results,
        saved_result_count=len(results),
        profile_links=profile_links,
        alerts=alerts,
        latest_activity=latest_activity,
        latest_activity_days_ago=latest_activity_days_ago,
        current_year=date.today().year,
        latest_pcs_check=latest_pcs_check,
        latest_firstcycling_check=latest_firstcycling_check,
        research_checks=research_checks,
        research_today=datetime.now(ZoneInfo('Europe/Amsterdam')).date().isoformat(),
        load_error=load_error,
    )


@web_bp.route('/activity-monitor/riders/<int:rider_id>/research-checks', methods=['POST'])
@login_required
def activity_monitor_save_research_check(rider_id):
    """Append a full or partial manual research record for this rider."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before recording research.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    as_of = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    raw_date = (request.form.get('research_date', type=str) or '').strip()
    try:
        research_date = date.fromisoformat(raw_date)
    except ValueError:
        flash('Enter a valid research date.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    if research_date > as_of:
        flash('Research date cannot be in the future.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    quality = (request.form.get('check_quality', type=str) or '').strip().casefold()
    if quality not in {'complete', 'incomplete', 'failed'}:
        flash('Choose complete, incomplete, or failed research.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    source_lines = (request.form.get('checked_sources', type=str) or '').splitlines()
    checked_sources = []
    for raw_url in source_lines:
        raw_url = raw_url.strip()
        if not raw_url:
            continue
        if len(raw_url) > 1000:
            flash('Each source link must be 1,000 characters or fewer.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        try:
            parsed = urllib.parse.urlsplit(raw_url)
            parsed.port
        except ValueError:
            flash('One of the source links is invalid.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        if (parsed.scheme.casefold() not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            flash('Source links must be valid http or https URLs.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        checked_sources.append(urllib.parse.urlunsplit(parsed))
    if len(checked_sources) > 30:
        flash('Add no more than 30 source links to one research record.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    notes = (request.form.get('research_notes', type=str) or '').strip()
    if len(notes) > 5000:
        flash('Research notes must be 5,000 characters or fewer.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    identity_confirmed = request.form.get('identity_confirmed') == 'yes'
    if quality == 'complete' and (not identity_confirmed or not checked_sources or not notes):
        flash('To mark research complete, confirm the rider identity, add at least one source link, and explain the search and its limits.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, source_worlddb_rider_id, identity_generation,
                       first_name, last_name, identity_hold
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')
            if quality == 'complete' and rider['identity_hold']:
                raise ValueError('This rider has an identity hold. Resolve it before marking research complete.')

            latest_attempt = conn.execute(text("""
                SELECT MAX(occurred_at::date)
                FROM activity_monitor_legacy_records
                WHERE rider_id = :rider_id
                  AND record_type = 'research_findings'
                  AND COALESCE(record_data->>'identity_generation', '0')::integer = :generation
                  AND occurred_at::date <= :research_date
            """), {
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'research_date': research_date,
            }).scalar_one()
            if latest_attempt and 0 < (research_date - latest_attempt).days < 20:
                raise ValueError('The Arie Tool spaces separate research attempts by at least 20 days. You can add to an attempt on the same date.')

            race_basis = conn.execute(text("""
                SELECT MAX(e.result_date)
                FROM activity_monitor_results e
                WHERE e.rider_id = :rider_id
                  AND e.identity_generation = :generation
                  AND e.identity_verified
                  AND (
                      e.participation_status IN ('confirmed_participation', 'confirmed')
                      OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                  )
                  AND e.result_date <= :research_date
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
            """), {
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'research_date': research_date,
            }).scalar_one()
            record_data = {
                'worlddb_rider_id': rider['source_worlddb_rider_id'],
                'identity_generation': rider['identity_generation'],
                'rider_name': ' '.join(filter(None, (rider['first_name'], rider['last_name']))),
                'research_date': research_date.isoformat(),
                'check_quality': quality,
                'checked_sources': checked_sources,
                'race_basis': race_basis.isoformat() if race_basis else '',
                'newer_evidence': False,
                'research_notes': notes,
                'research_status': 'unresolved',
                'identity_confidence': 'high' if identity_confirmed else 'unknown',
                'participation_status': 'not_found',
                'race_date': race_basis.isoformat() if race_basis else '',
                'race_name': '',
                'source_name': 'PCM Manager manual research',
                'recorded_by_user_id': getattr(current_user, 'id', None),
            }
            conn.execute(text("""
                INSERT INTO activity_monitor_legacy_records (
                    record_type, source_record_key, rider_id, occurred_at,
                    record_data, imported_at
                ) VALUES (
                    'research_findings', :source_record_key, :rider_id,
                    :occurred_at, CAST(:record_data AS JSON), :imported_at
                )
            """), {
                'source_record_key': f"online-research-check:{secrets.token_urlsafe(18)}",
                'rider_id': rider_id,
                'occurred_at': datetime.combine(research_date, datetime.min.time()),
                'record_data': json.dumps(record_data),
                'imported_at': datetime.utcnow(),
            })
        flash('Research record saved. Its date and coverage are now part of the rider history and planning.', 'success')
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), 'error')
    except Exception:
        logger.exception('Failed to save Activity Monitor research check for rider %s', rider_id)
        flash('Research could not be saved. No partial record was kept.', 'error')
    finally:
        if engine is not None:
            engine.dispose()
    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile', methods=['POST'])
@login_required
def activity_monitor_save_pcs_profile(rider_id):
    """Save a shared, unverified PCS /road profile override for a rider."""
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def respond(message, category='error', status_code=200, slug=None, profile_url=None):
        if wants_json:
            return jsonify({
                'ok': category == 'success',
                'message': message,
                'slug': slug,
                'url': profile_url,
            }), status_code
        flash(message, category)
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        if wants_json:
            return jsonify({'ok': False, 'message': 'Select an active namespace before changing a PCS profile link.'}), 400
        flash('Select an active namespace before changing a PCS profile link.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    entered_value = (request.form.get('pcs_profile', type=str) or '').strip()
    slug = entered_value
    if '://' in entered_value:
        try:
            parsed = urllib.parse.urlsplit(entered_value)
            parsed_port = parsed.port
        except ValueError:
            return respond('Enter a valid PCS rider slug or profile URL.', status_code=400)
        match = re.fullmatch(r'/rider/([A-Za-z0-9_-]+)(?:/road)?/?', parsed.path, re.IGNORECASE)
        if (
            parsed.scheme.lower() not in {'http', 'https'}
            or parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}
            or parsed_port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not match
        ):
            return respond('Enter a PCS rider slug or a PCS rider profile URL. Other URLs are not accepted.', status_code=400)
        slug = match.group(1)

    if len(slug) > 255 or not re.fullmatch(r'[A-Za-z0-9_-]+', slug):
        return respond('Enter only the PCS rider slug, for example kahiri-endeler, or its PCS profile URL.', status_code=400)

    profile_url = f'https://www.procyclingstats.com/rider/{slug}/road'
    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        now = datetime.utcnow()
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')

            conn.execute(text("""
                UPDATE activity_monitor_profile_links
                SET is_current = FALSE
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']})
            conn.execute(text("""
                INSERT INTO activity_monitor_profile_links (
                    source_record_key, rider_id, identity_generation, profile_path,
                    link_source, link_status, is_current, override_url, entered_value,
                    override_updated_at, profile_slug,
                    check_result, message, created_at
                ) VALUES (
                    :source_record_key, :rider_id, :generation, :profile_path,
                    'manual_override', 'candidate', TRUE, :override_url, :entered_value,
                    :updated_at, :profile_slug,
                    'not_checked', 'Manually entered PCS profile; identity has not been checked.', :created_at
                )
            """), {
                'source_record_key': f'manual-pcs-profile:{rider_id}:{rider["identity_generation"]}:{secrets.token_hex(8)}',
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'profile_path': profile_url,
                'override_url': profile_url,
                'entered_value': entered_value,
                'updated_at': now,
                'profile_slug': slug,
                'created_at': now,
            })
    except LookupError:
        if wants_json:
            return jsonify({'ok': False, 'message': 'Activity Monitor rider no longer exists.'}), 404
        abort(404)
    except Exception:
        logger.exception('Failed to save PCS profile override for rider %s', rider_id)
        return respond('The PCS profile link could not be saved. No PCS request was sent.', status_code=500)
    else:
        if wants_json:
            return jsonify({
                'ok': True,
                'message': 'Saved as a shared, unverified /road link. No PCS request was sent.',
                'slug': slug,
                'url': profile_url,
            })
        flash('PCS profile link saved as an unverified /road link. No PCS request was sent.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile/approve', methods=['POST'])
@login_required
def activity_monitor_approve_pcs_profile(rider_id):
    """Approve one checked PCS profile for its exact, mismatching DOB pair."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before approving a PCS profile.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation, birth_date, identity_hold
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                flash('Activity Monitor rider not found.', 'error')
                return redirect(url_for('web.activity_monitor_riders'))
            if rider['identity_hold']:
                flash('This rider has an identity hold. Resolve it before approving a PCS profile.', 'error')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            link = conn.execute(text("""
                SELECT id, approved_profile_path, override_url, profile_path,
                       check_result, checked_url, http_status, profile_birth_date,
                       approved_pcs_birth_date, approved_worlddb_birth_date
                FROM activity_monitor_profile_links
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            mismatch_check = conn.execute(text("""
                SELECT request_path, http_status, message
                FROM activity_monitor_source_checks
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND source IN ('pcs', 'pcs_html', 'procyclingstats')
                  AND identity_result = 'mismatch'
                  AND http_status = 200
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            selected_value = (
                (link['approved_profile_path'] or link['override_url']
                 or link['profile_path'])
                if link else None
            ) or (mismatch_check['request_path'] if mismatch_check else '')
            selected = urllib.parse.urlsplit(
                selected_value if '://' in selected_value
                else f'https://www.procyclingstats.com/{selected_value.lstrip("/")}'
            )
            checked_value = (link['checked_url'] if link else None) or (
                f"https://www.procyclingstats.com{mismatch_check['request_path']}"
                if mismatch_check else ''
            )
            checked = urllib.parse.urlsplit(checked_value)
            valid_hosts = {'www.procyclingstats.com', 'procyclingstats.com'}
            profile_dob = link['profile_birth_date'] if link else None
            if profile_dob is None and mismatch_check:
                date_match = re.search(r"PCS '?(\d{4}-\d{2}-\d{2})'?", str(mismatch_check['message'] or ''))
                if date_match:
                    try:
                        profile_dob = date.fromisoformat(date_match.group(1))
                    except ValueError:
                        profile_dob = None
            worlddb_dob = rider['birth_date']
            already_approved = bool(
                link and link['approved_profile_path']
                and link['approved_pcs_birth_date'] == profile_dob
                and link['approved_worlddb_birth_date'] == worlddb_dob
            )
            if already_approved:
                flash('This exact PCS profile and birthdate pair has already been approved.', 'info')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
            if not (
                selected.scheme == 'https'
                and selected.hostname in valid_hosts
                and selected.path.rstrip('/').casefold().endswith('/road')
                and checked.scheme == 'https'
                and checked.hostname in valid_hosts
                and checked.path.rstrip('/').casefold() == selected.path.rstrip('/').casefold()
                and ((link and link['http_status'] == 200
                      and link['check_result'] in {'identity_mismatch', 'mismatch', 'rider_details_dont_match'})
                     or (mismatch_check and mismatch_check['http_status'] == 200))
                and worlddb_dob and profile_dob and worlddb_dob != profile_dob
            ):
                flash('Approval requires a current successful /road check with a confirmed, differing birthdate.', 'error')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            if link:
                conn.execute(text("""
                    UPDATE activity_monitor_profile_links
                    SET approved_profile_path = :profile_path,
                        approved_pcs_birth_date = :pcs_birth_date,
                        approved_worlddb_birth_date = :worlddb_birth_date,
                        approved_at = :approved_at,
                        approved_by_user_id = :user_id
                    WHERE id = :link_id AND is_current = TRUE
                """), {
                    'profile_path': selected.path.rstrip('/'),
                    'pcs_birth_date': profile_dob,
                    'worlddb_birth_date': worlddb_dob,
                    'approved_at': datetime.utcnow(),
                    'user_id': current_user.id,
                    'link_id': link['id'],
                })
            else:
                conn.execute(text("""
                    INSERT INTO activity_monitor_profile_links (
                        source_record_key, rider_id, identity_generation, profile_path,
                        link_source, link_status, is_current, approved_profile_path,
                        checked_at, checked_url, http_status, check_result,
                        profile_birth_date, approved_pcs_birth_date,
                        approved_worlddb_birth_date, approved_at, approved_by_user_id,
                        message, created_at
                    ) VALUES (
                        :source_record_key, :rider_id, :generation, :profile_path,
                        'assumed', 'candidate', TRUE, :profile_path,
                        :checked_at, :checked_url, 200, 'identity_mismatch',
                        :pcs_birth_date, :pcs_birth_date, :worlddb_birth_date,
                        :approved_at, :user_id, :message, :created_at
                    )
                """), {
                    'source_record_key': f"approved-pcs:{rider_id}:{rider['identity_generation']}:{secrets.token_hex(8)}",
                    'rider_id': rider_id,
                    'generation': rider['identity_generation'],
                    'profile_path': selected.path.rstrip('/'),
                    'checked_at': datetime.utcnow(),
                    'checked_url': checked_value,
                    'pcs_birth_date': profile_dob,
                    'worlddb_birth_date': worlddb_dob,
                    'approved_at': datetime.utcnow(),
                    'user_id': current_user.id,
                    'message': str(mismatch_check['message'] or '') if mismatch_check else 'Approved after a confirmed PCS birthdate mismatch.',
                    'created_at': datetime.utcnow(),
                })
    except Exception:
        logger.exception('Failed to approve PCS profile for rider %s', rider_id)
        flash('The PCS profile could not be approved. No results were changed.', 'error')
    else:
        flash('PCS profile approved for this exact profile and birthdate pair. Run a new PCS check before its results count as verified activity.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile/clear', methods=['POST'])
@login_required
def activity_monitor_clear_pcs_profile(rider_id):
    """Clear the current manual PCS override without restoring a CDB link."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before changing a PCS profile link.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        now = datetime.utcnow()
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')

            current_link = conn.execute(text("""
                SELECT id, link_source, override_url
                FROM activity_monitor_profile_links
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            if current_link is None or not (
                current_link['link_source'] == 'manual_override'
                or current_link['override_url']
            ):
                flash('There is no current manual PCS override to clear.', 'info')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            conn.execute(text("""
                UPDATE activity_monitor_profile_links
                SET is_current = FALSE
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']})

            conn.execute(text("""
                INSERT INTO activity_monitor_profile_links (
                    source_record_key, rider_id, identity_generation, profile_path,
                    link_source, link_status, is_current,
                    check_result, message, created_at
                ) VALUES (
                    :source_record_key, :rider_id, :generation, :profile_path,
                    'manual_override', 'cleared', TRUE,
                    'not_checked', :message, :created_at
                )
            """), {
                'source_record_key': f'manual-pcs-clear:{rider_id}:{rider["identity_generation"]}:{secrets.token_hex(8)}',
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'profile_path': None,
                'message': 'Manual PCS override cleared; no profile link is selected.',
                'created_at': now,
            })
    except LookupError:
        abort(404)
    except Exception:
        logger.exception('Failed to clear PCS profile override for rider %s', rider_id)
        flash('The PCS profile override could not be cleared.', 'error')
    else:
        flash('Manual PCS override cleared. No profile link is selected.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/teams/review', endpoint='teams_review')
@web_bp.route('/riders/review', endpoint='riders_review')
@web_bp.route('/races/review', endpoint='races_review')
@login_required
def object_review(object_type=None):
    object_type = object_type or request.endpoint.rsplit('.', 1)[-1].removesuffix('_review')
    panel = 'stat_editor' if object_type == 'races' else 'team_manager'
    if not _can_access_panel(panel):
        abort(403)
    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    review_tab = request.args.get('tab', 'stats') if object_type == 'riders' else 'attributes'
    if review_tab not in ('stats', 'identity', 'activity', 'attributes'):
        abort(404)
    if object_type == 'riders' and review_tab == 'stats' and not _can_access_panel('stat_editor'):
        abort(403)
    changes, attribute_history, pending_stats, historical_stats = [], [], [], []
    identity_review = None
    if selected_namespace is not None and review_tab == 'attributes':
        changes = _load_attribute_review(selected_namespace, object_type[:-1])
        if object_type == 'riders':
            attribute_history = _load_attribute_review(selected_namespace, 'rider', include_history=True)
    elif selected_namespace is not None and review_tab == 'stats':
        pending_stats = _load_namespace_rider_stat_edits(selected_namespace, approved=False, limit=200)
        historical_stats = _load_namespace_rider_stat_edits(selected_namespace, approved=True, limit=200)
    elif selected_namespace is not None and review_tab == 'identity':
        flags = list(dict.fromkeys(request.args.getlist('flag'))) if 'filtered' in request.args or 'flag' in request.args else list(DEFAULT_IDENTITY_REVIEW_FLAGS)
        if any(flag not in IDENTITY_REVIEW_FILTERS for flag in flags):
            abort(400)
        query = (request.args.get('q') or '').strip()
        page = max(request.args.get('page', 1, type=int) or 1, 1)
        per_page = 50
        filters = dict(view='identity', identity_review=True, identity_flags=flags, q=query)
        riders, total, _, _ = _load_namespace_roster_riders(selected_namespace, filters, page, per_page)
        pages = max((total + per_page - 1) // per_page, 1)
        if page > pages:
            return redirect(url_for('web.riders_review', namespace_slug=selected_namespace.slug,
                                    tab='identity', flag=flags, filtered=1, q=query, page=pages))
        for rider in riders:
            rider.identity_check_slug = request_rider_slug(rider.pcs_slug, rider.first_name, rider.last_name)
            rider.identity_saved_slug = rider.pcs_slug if valid_rider_slug(rider.pcs_slug) else None
        identity_review = dict(riders=riders, total=total, page=page, pages=pages, flags=flags, q=query)
    return render_template(
        'object_review.html', object_type=object_type,
        selected_namespace=selected_namespace, changes=changes,
        review_tab=review_tab, attribute_history=attribute_history,
        pending_stats=pending_stats, historical_stats=historical_stats,
        can_approve_stats=_can_approve_stats(),
        can_approve_attributes=_can_attribute_action('approve'),
        identity_review=identity_review, identity_review_filters=IDENTITY_REVIEW_FILTERS,
        identity_flag_labels=IDENTITY_FILTERS, can_check_identity=_can_check_rider_identity(),
        rider_task_types=_rider_bulk_task_types(), rider_selection_limit=MAX_BULK_TASK_RIDERS,
    )


def _can_attribute_action(action):
    return current_user.is_superadmin or any(
        current_user.has_permission(slug, action)
        for slug in field_configuration.field_slugs() if slug != 'rider:stat'
    )


def _can_rider_list_action(list_type, action):
    if list_type == 'attribute':
        return _can_attribute_action('update')
    return _can_create_stats() if action == 'create' else _can_update_stats()


def _load_attribute_review(namespace, entity_type, include_history=False, applied_entity_id=None):
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            if include_history:
                rows = attribute_changes.history_events(conn, entity_type, applied_entity_id=applied_entity_id)
            else:
                rows = attribute_changes.review_queue(conn, entity_type)
            changes = [dict(change) for change in rows]
            if not include_history:
                configuration = field_configuration.stored_fields(conn, entity_type)
                for change in changes:
                    name = change['attribute_name']
                    slug = configuration.get((entity_type, name), {}).get(
                        'slug', field_configuration.field_slug(entity_type, name),
                    )
                    change['can_approve'] = current_user.has_permission(slug, 'approve')
            riders = {}
            if entity_type == 'rider' and changes:
                rider_ids = list({change['entity_id'] for change in changes})
                riders = {row['id']: _cdb_rider_display_name(row) for row in conn.execute(text(
                    'SELECT id, first_name, last_name, first_last_name FROM rider WHERE id = ANY(:ids)'
                ), {'ids': rider_ids}).mappings()}
        actor_field = 'actor_id' if include_history else 'created_by_id'
        user_ids = {change[actor_field] for change in changes if change[actor_field] is not None}
        usernames = {user.id: user.username for user in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        for change in changes:
            change['submitted_by'] = usernames.get(change[actor_field], 'Unknown user') if change[actor_field] is not None else 'System'
            change['rider_name'] = riders.get(change['entity_id']) or f"Rider #{change['entity_id']}"
        return changes
    finally:
        engine.dispose()


def _attribute_editor_context(namespace, entity_type, entity_id):
    if namespace is None:
        return {}
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            fields = attribute_changes.editor_fields(conn, entity_type, entity_id, current_user.has_permission)
        pending = [dict(field['pending'], can_approve=field['can_approve']) for field in fields if field['pending']]
        actor_ids = {change['created_by_id'] for change in pending if change['created_by_id'] is not None}
        actors = {user.id: user.username for user in User.query.filter(User.id.in_(actor_ids)).all()} if actor_ids else {}
        for change in pending:
            change['submitted_by'] = actors.get(change['created_by_id'], 'Unknown user') if change['created_by_id'] is not None else 'System'
        return {'attribute_fields': fields, 'attribute_field_map': {field['name']: field for field in fields},
                'pending_attribute_changes': pending,
                'can_edit_attributes': _can_attribute_action('update'),
                'can_approve_attributes': _can_attribute_action('approve'), 'attribute_entity_type': entity_type,
                'attribute_entity_id': entity_id, 'attribute_namespace_slug': namespace.slug}
    finally:
        engine.dispose()


@web_bp.route('/attributes/<entity_type>/<int:entity_id>/propose', methods=['POST'])
@login_required
def propose_attribute(entity_type, entity_id):
    if not _can_attribute_action('update'):
        abort(403)
    if entity_type not in ('rider', 'team', 'race'):
        abort(404)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    list_id = request.form.get('list_id', type=int)
    if 'list_id' in request.form and (list_id is None or list_id < 1 or entity_type != 'rider'):
        abort(400)
    if list_id is not None and not _can_access_panel('stat_editor'):
        abort(403)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.begin() as conn:
            if list_id is not None:
                _lock_editable_stat_list(conn, namespace, list_id, required_type='attribute')
                if conn.execute(text('''
                    SELECT 1 FROM rider_list_item WHERE rider_list_id = :list_id AND rider_id = :rider_id
                '''), {'list_id': list_id, 'rider_id': entity_id}).first() is None:
                    abort(404)
            else:
                _ensure_namespace_database_connection(conn, namespace)
            change_id = attribute_changes.propose(
                conn, entity_type, entity_id, request.form.get('attribute_name', ''),
                None if request.form.get('clear_value') == '1' else request.form.get('value', ''),
                current_user.id, stat_list_id=list_id, authorize=current_user.has_permission,
            )
            applied = conn.execute(text('SELECT applied_at FROM cdb_changes WHERE id = :id'),
                                   {'id': change_id}).scalar_one() is not None
    except PermissionError:
        abort(403)
    except LookupError:
        abort(404)
    except attribute_changes.AttributeConflict as exc:
        flash(str(exc), 'warning')
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        flash('Change applied.' if applied else 'Change submitted for review.', 'success')
    finally:
        engine.dispose()
    if list_id is not None:
        return redirect(url_for('web.bulk_editor', namespace_slug=namespace.slug, tab='editor', list_id=list_id))
    return redirect(url_for('web.' + ('cdb_race' if entity_type == 'race' else entity_type) + '_detail',
                            **{entity_type + '_id': entity_id}, namespace_slug=namespace.slug))


@web_bp.route('/attributes/<entity_type>/review/<int:change_id>/<action>', methods=['POST'])
@login_required
def review_attribute(entity_type, change_id, action):
    if not _can_attribute_action('approve'):
        abort(403)
    if entity_type not in ('rider', 'team', 'race') or action not in ('approve', 'reject'):
        abort(404)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            attribute_changes.decide(conn, entity_type, change_id, action, current_user.id,
                                     authorize=current_user.has_permission)
    except PermissionError:
        abort(403)
    except LookupError:
        abort(404)
    except attribute_changes.AttributeConflict as exc:
        flash(str(exc), 'warning')
    except ValueError as exc:
        flash(str(exc), 'error')
    else:
        flash('Change approved and applied.' if action == 'approve' else 'Change rejected.', 'success')
    finally:
        engine.dispose()
    rider_id = request.form.get('return_to_rider_id', type=int)
    if entity_type == 'rider' and rider_id is not None and rider_id >= 0:
        return redirect(url_for('web.rider_detail', rider_id=rider_id, namespace_slug=namespace.slug))
    return redirect(url_for('web.' + entity_type + 's_review', namespace_slug=namespace.slug,
                            **({'tab': 'attributes'} if entity_type == 'rider' else {})))


@web_bp.route('/teams/change-log', endpoint='teams_change_log')
@web_bp.route('/riders/change-log', endpoint='riders_change_log')
@web_bp.route('/races/change-log', endpoint='races_change_log')
@login_required
def object_change_log():
    """Display recorded attribute events from the selected namespace."""
    object_type = request.endpoint.rsplit('.', 1)[-1].removesuffix('_change_log')
    panel = 'stat_editor' if object_type == 'races' else 'team_manager'
    if not _can_access_panel(panel):
        abort(403)
    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    return render_template(
        'object_change_log.html', object_type=object_type,
        selected_namespace=selected_namespace,
        changes=_load_attribute_review(selected_namespace, object_type[:-1], include_history=True)
        if selected_namespace is not None else [],
    )


@web_bp.route('/riders', methods=['GET', 'POST'], endpoint='riders')
@web_bp.route('/teams', methods=['GET', 'POST'])
@login_required
def teams():
    """Dedicated team and rider pages using selected namespace CDB roster data."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None

    active_method = 'riders' if request.endpoint == 'web.riders' else 'teams'

    team_division_options = []
    team_country_options = []
    teams = []
    rider_counts = {}
    team_filters = _load_teams_state(selected_namespace)
    rider_filters = _load_riders_state(selected_namespace)
    riders = []
    rider_total = 0
    snapshot_riders = []
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
                rider_filters = {**_default_riders_state(), 'view': rider_filters['view']}
            elif action == 'set_view':
                view = request.form.get('view', '')
                if view not in RIDER_VIEWS:
                    abort(400)
                if view == 'activity' and not _can_access_panel('activity_monitor'):
                    abort(403)
                rider_filters.update(view=view, page=1)
            elif action == 'set_page':
                rider_filters['page'] = max(request.form.get('page', type=int) or 1, 1)
            else:
                stat_key = request.form.get('stat_key', rider_filters['stat_key'])
                if stat_key and stat_key not in STAT_COLUMN_MAP:
                    abort(400)
                stat_bounds = {key: request.form.get(key, rider_filters[key]).strip() for key in ('stat_min', 'stat_max')}
                if any(value and (not value.isdigit() or not 0 <= int(value) <= 99) for value in stat_bounds.values()):
                    abort(400)
                if all(stat_bounds.values()) and int(stat_bounds['stat_min']) > int(stat_bounds['stat_max']):
                    abort(400)
                activity_status = request.form.get('activity_status', rider_filters['activity_status'])
                if activity_status not in ACTIVITY_FILTERS:
                    abort(400)
                if activity_status and not _can_access_panel('activity_monitor'):
                    abort(403)
                identity_flag = request.form.get('identity_flag', rider_filters['identity_flag'])
                if identity_flag not in IDENTITY_FILTERS:
                    abort(400)
                rider_filters.update({
                    'stat_key': stat_key,
                    **stat_bounds,
                    'activity_status': activity_status,
                    'identity_flag': identity_flag,
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
        return redirect(url_for('web.riders' if requested_method == 'riders' else 'web.teams'))

    if active_method == 'riders' and rider_filters['view'] == 'activity' and not _can_access_panel('activity_monitor'):
        abort(403)

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
        filters = dict(rider_filters)
        page = rider_filters['page']
        per_page = rider_filters['per_page']

        snapshot_riders, _, rider_country_options, rider_division_options = _load_namespace_roster_riders(
            selected_namespace, {}, snapshot=True, include_activity=_can_access_panel('activity_monitor'),
        )
        if rider_filters['activity_status'] and not _can_access_panel('activity_monitor'):
            abort(403)
        filtered_riders = filter_snapshot(snapshot_riders, filters, _normalize_name_search_term)
        rider_total = len(filtered_riders)
        total_pages = max((rider_total + per_page - 1) // per_page, 1)
        page = min(page, total_pages)
        riders = filtered_riders[(page - 1) * per_page:page * per_page]
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

    response = make_response(render_template(
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
        rider_views=RIDER_VIEWS,
        rider_snapshot=_rider_snapshot_payload(snapshot_riders, selected_namespace) if active_method == 'riders' and season_selected else None,
        rider_pagination=rider_pagination,
        per_page_options=(50, 100, 150, 250),
        stat_fields=STAT_DELTA_FIELDS,
        rider_stats_map={rider.rider_id: _namespace_roster_stats_from_rider(rider) for rider in riders},
        can_view_activity=_can_access_panel('activity_monitor'),
        activity_filter_options=ACTIVITY_FILTERS,
        identity_filter_options=IDENTITY_FILTERS,
        can_create_rider_list=_can_access_panel('stat_editor') and (
            _can_create_stats() or _can_attribute_action('update')),
        can_start_stat_list=_can_access_panel('stat_editor') and _can_create_stats(),
        can_bulk_edit_attributes=_can_attribute_action('update'),
        rider_task_types=_rider_bulk_task_types(),
        rider_selection_limit=MAX_BULK_TASK_RIDERS,
    ))
    if active_method == 'riders':
        response.cache_control.no_store = True
    return response


def _rider_snapshot_payload(riders, namespace):
    can_view_activity = _can_access_panel('activity_monitor')
    activity_fields = {'monitor_id', 'activity_available', 'latest_check_at', 'latest_check_status',
                       'latest_result_date', 'latest_race_name', 'days_since_last_race'}
    records = []
    omitted_fields = {'cyclist_id', 'birth_date', 'pcs_birth_date', 'region_id', 'first_name', 'last_name'}
    for rider in riders:
        record = {key: value.isoformat() if isinstance(value, (date, datetime)) else value
                  for key, value in vars(rider).items()
                  if value is not None and key not in omitted_fields
                  and (can_view_activity or key not in activity_fields)}
        record['rider_url'] = url_for('web.rider_detail', namespace_slug=namespace.slug, rider_id=rider.rider_id)
        record['team_url'] = url_for('web.team_detail', namespace_slug=namespace.slug, team_id=rider.team_id) if rider.team_id else None
        if can_view_activity:
            record['check_display'] = _activity_datetime_label(rider.latest_check_at) if rider.latest_check_at else '-'
            record['check_status_display'] = _activity_check_status_label(rider.latest_check_status) if rider.latest_check_status else (
                'History unavailable' if rider.activity_available is False else
                'Awaiting check' if rider.monitor_id else 'Not monitored')
        records.append(record)
    return dict(riders=records, namespace_id=namespace.id, updated_at=datetime.utcnow().isoformat() + 'Z')


@web_bp.route('/riders/snapshot')
@login_required
def rider_snapshot():
    if not _can_access_panel('team_manager'):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    riders, _, countries, divisions = _load_namespace_roster_riders(
        namespace, {}, snapshot=True, include_activity=_can_access_panel('activity_monitor'),
    )
    response = jsonify(**_rider_snapshot_payload(riders, namespace), countries=countries, divisions=divisions)
    response.cache_control.no_store = True
    return response


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
        **_attribute_editor_context(selected_namespace, 'team', team_id),
        team=team,
        riders=riders,
        rider_stats_map=rider_stats_map,
        stat_fields=STAT_DELTA_FIELDS,
        back_params={
            'division': request.args.get('division', type=str) or '',
        },
    )


def _rider_bulk_task_types():
    can_run_fc = _can_run_firstcycling_integrations()
    can_run_pcs = _can_check_rider_identity()
    run_permissions = {
        FC_RIDER_SYNC: can_run_fc,
        PCS_RIDER_SYNC: can_run_pcs,
        IDENTITY_CHECK: can_run_pcs,
        ACTIVITY_CHECK: can_run_fc and can_run_pcs and _can_access_panel('activity_monitor'),
    }
    return {
        kind: _external_sync_type_display(kind) for kind, allowed in run_permissions.items() if allowed
    }


def _parse_bulk_rider_ids(value):
    tokens = re.findall(r'[^,\s]+', value)
    if not 1 <= len(tokens) <= MAX_BULK_TASK_RIDERS or any(
        not re.fullmatch(r'[0-9]{1,10}', token) or not 0 < int(token) <= 2147483647
        for token in tokens
    ):
        return []
    return list(dict.fromkeys(map(int, tokens)))


@web_bp.route('/riders/bulk-tasks', methods=['GET', 'POST'])
@login_required
def rider_bulk_tasks():
    if not _can_access_panel('rider_manager'):
        abort(403)
    can_read = _can_read_external_call_groups()
    runnable_types = _rider_bulk_task_types()
    if not can_read and not runnable_types:
        abort(403)

    namespace = _get_selected_namespace()
    tab = request.args.get('tab', 'groups' if can_read else 'editor')
    if tab not in ('groups', 'editor'):
        abort(404)
    groups = ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id) if namespace else None
    if groups is not None and not can_read:
        groups = groups.filter_by(executed_by_id=current_user.id)
    values = request.form if request.method == 'POST' else request.args
    group_id = values.get('group_id', type=int)
    if values.get('group_id') and group_id is None:
        abort(400)
    group = groups.filter_by(id=group_id).with_for_update().first_or_404() if group_id and groups is not None else None
    if group_id and group is None:
        abort(404)
    if request.method == 'POST':
        if namespace is None:
            abort(404)
        if request.form.get('action') == 'run_data_sync':
            if group_id is not None:
                abort(400)
            return _run_rider_data_sync(namespace, runnable_types)
        return _save_rider_task_group(namespace, group, runnable_types)

    selected_type = request.args.get('sync_type', '').strip()
    page = None
    if groups is not None and tab == 'groups':
        if selected_type:
            groups = groups.filter_by(sync_type=selected_type)
        page = groups.order_by(ExternalCallTaskGroup.timestamp.desc(), ExternalCallTaskGroup.id.desc()).paginate(per_page=50)
    members = []
    search_results = []
    rider_term = request.args.get('rider_search', '').strip()[:120]
    team_term = request.args.get('team_search', '').strip()[:120]
    if groups is not None and tab == 'editor':
        if group:
            tasks = ExternalCallTask.query.filter_by(task_group_id=group.id).order_by(ExternalCallTask.id).all()
            rider_ids = {task.related_to_id for task in tasks if task.related_to_table == 'rider'}
            names = {}
            if rider_ids:
                try:
                    with rider_database(namespace) as conn:
                        names = {row.id: _cdb_rider_display_name(row._mapping) for row in conn.execute(text(
                            'SELECT id, first_name, last_name FROM rider WHERE id = ANY(:ids)'
                        ), {'ids': list(rider_ids)})}
                except (ValueError, SQLAlchemyError):
                    flash('Could not load riders from this namespace.', 'warning')
            members = [dict(id=task.id, rider_id=task.related_to_id,
                            name=names.get(task.related_to_id, f'Rider #{task.related_to_id}') if task.related_to_table == 'rider' else task.endpoint,
                            provider=task.provider, status=task.status) for task in tasks]
            if group.status == 'draft' and group.sync_type in runnable_types:
                search_results = _search_stat_list_riders(namespace, rider_ids, rider_term, team_term)
    editor_types = {kind: label for kind, label in runnable_types.items()
                    if kind in (FC_RIDER_SYNC, PCS_RIDER_SYNC) or (group and kind == group.sync_type)}
    response = make_response(render_template(
        'rider_bulk_tasks.html', selected_namespace=namespace, page=page, runnable_types=runnable_types,
        max_riders=MAX_BULK_TASK_RIDERS, sync_type_display=_external_sync_type_display,
        tab=tab, group=group, members=members,
        editor_types=editor_types, selected_type=selected_type, task_types=EXTERNAL_SYNC_TYPE_LABELS,
        rider_search=rider_term, team_search=team_term, search_results=search_results,
    ))
    response.cache_control.no_store = True
    return response


def _rider_task_requests(namespace, rider_ids, sync_type):
    with rider_database(namespace) as conn:
        riders = conn.execute(text("""
            SELECT id, first_name, last_name, first_cycling_id, pcs_slug
            FROM rider WHERE id = ANY(:ids) AND is_deleted IS FALSE ORDER BY id
        """), {'ids': rider_ids}).mappings().all()
    if len(riders) != len(rider_ids):
        raise ValueError('One or more riders are unavailable in this namespace.')
    requests = []
    for rider in riders:
        for provider in RIDER_BULK_TASK_PROVIDERS[sync_type]:
            if provider == 'pcs':
                slug = request_rider_slug(rider['pcs_slug'], rider['first_name'], rider['last_name'])
                params = {'rider_slug': slug} if slug else None
                identifier = 'PCS slug or name'
            else:
                fc_id = rider['first_cycling_id']
                params = {'rider_id': fc_id} if fc_id and fc_id > 0 else None
                identifier = 'FirstCycling ID'
            if params is None:
                raise ValueError(f"Rider #{rider['id']} needs a valid {identifier}.")
            requests.append(dict(provider=provider, endpoint=PROFILE_ENDPOINT, params=params,
                                 related_to_table='rider', related_to_id=rider['id']))
    return requests


def _run_rider_data_sync(namespace, runnable_types):
    if PCS_RIDER_SYNC not in runnable_types or FC_RIDER_SYNC not in runnable_types:
        abort(403)
    return_url = url_for('web.riders', namespace_slug=namespace.slug)
    try:
        rider_ids = _parse_bulk_rider_ids(request.form.get('rider_ids', ''))
        if not rider_ids:
            raise ValueError(f'Select between 1 and {MAX_BULK_TASK_RIDERS} valid riders.')
        # Validate both providers before staging either group; commit them together.
        requests = _rider_task_requests(namespace, rider_ids, ACTIVITY_CHECK)
        groups = [enqueue_group(
            [item for item in requests if item['provider'] == provider],
            namespace_id=namespace.id, executed_by_id=current_user.id,
            provider=provider, sync_type=sync_type, message='Bulk Run Data Sync',
        ) for provider, sync_type in (('pcs', PCS_RIDER_SYNC), ('firstcycling', FC_RIDER_SYNC))]
        group_ids = [group.id for group in groups]
        db.session.commit()
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else 'Could not queue rider data sync.', 'error')
        return redirect(return_url)
    for group_id in group_ids:
        try:
            submit_external_group(group_id)
        except Exception:
            db.session.rollback()
            logger.exception('Could not submit rider task group %s', group_id)
            flash('The group is queued and will be retried by the dispatcher.', 'warning')
    flash('PCS and FirstCycling rider data sync groups queued.', 'success')
    return redirect(url_for('web.rider_bulk_tasks', tab='groups'))


def _save_rider_task_group(namespace, group, runnable_types):
    action = request.form.get('action', 'create')
    if action not in ('create', 'save', 'add', 'remove', 'queue', 'cancel'):
        abort(400)
    if action != 'create' and group is None:
        abort(400)
    if action == 'create' and group is not None:
        abort(400)
    sync_type = request.form.get('sync_type', group.sync_type if group else None)
    if sync_type not in RIDER_BULK_TASK_PROVIDERS:
        abort(400)
    if sync_type not in runnable_types or (group and group.sync_type not in runnable_types):
        abort(403)
    if group and group.status != 'draft' and action != 'cancel':
        abort(409, description='Only draft groups can be edited or queued.')
    return_url = url_for('web.rider_bulk_tasks', tab='editor', group_id=group.id if group else None)
    try:
        if action == 'cancel':
            cancel_external_group(group.id)
        elif action == 'remove':
            rider_id = request.form.get('rider_id', type=int)
            removed = ExternalCallTask.query.filter_by(
                task_group_id=group.id, related_to_table='rider', related_to_id=rider_id,
            ).delete()
            if not removed:
                abort(404)
            group.requested_count = ExternalCallTask.query.filter_by(task_group_id=group.id).count()
        else:
            tasks = ExternalCallTask.query.filter_by(task_group_id=group.id).all() if group else []
            rider_ids = list(dict.fromkeys(task.related_to_id for task in tasks if task.related_to_table == 'rider'))
            if action in ('create', 'add'):
                raw_ids = request.form.get('rider_ids', '').strip()
                added = _parse_bulk_rider_ids(raw_ids) if raw_ids else []
                if raw_ids and not added:
                    raise ValueError(f'Enter between 1 and {MAX_BULK_TASK_RIDERS} valid rider IDs.')
                rider_ids = list(dict.fromkeys(rider_ids + added))
            if len(rider_ids) > MAX_BULK_TASK_RIDERS:
                raise ValueError(f'A group can contain up to {MAX_BULK_TASK_RIDERS} riders.')
            if action == 'queue' and not rider_ids:
                raise ValueError('Add at least one rider before queuing this group.')
            requests = _rider_task_requests(namespace, rider_ids, sync_type)
            message = (request.form.get('name', group.message if group else '') or '').strip()
            if len(message) > 255:
                raise ValueError('Enter a group name of up to 255 characters.')
            providers = RIDER_BULK_TASK_PROVIDERS[sync_type]
            if group is None:
                group = enqueue_group(requests, namespace_id=namespace.id, executed_by_id=current_user.id,
                                      draft=True, sync_type=sync_type)
            else:
                replace_draft_requests(group, requests)
            group.message = message or f'{_external_sync_type_display(sync_type)} bulk request'
            group.sync_type = sync_type
            group.provider = providers[0] if len(providers) == 1 else None
            if action == 'queue':
                group.status = 'queued'
                ExternalCallTask.query.filter_by(task_group_id=group.id).update(
                    dict(status='queued', available_at=datetime.utcnow()), synchronize_session='fetch')
        group_id = group.id
        db.session.commit()
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        message = str(exc) if isinstance(exc, ValueError) else 'Could not save this task group.'
        flash(message, 'error')
        return redirect(return_url)
    if action == 'queue':
        try:
            submit_external_group(group_id)
        except Exception:
            db.session.rollback()
            logger.exception('Could not submit rider task group %s', group_id)
            flash('The group is queued and will be retried by the dispatcher.', 'warning')
    flash('Task group cancelled.' if action == 'cancel' else 'Task group queued.' if action == 'queue' else 'Draft saved.', 'success')
    return redirect(url_for('web.rider_bulk_tasks', tab='editor', group_id=group_id))


@login_required
def check_rider_identity(rider_id):
    if not _can_check_rider_identity():
        abort(403)

    namespace = _get_selected_namespace()
    rider = _load_namespace_roster_rider(namespace, rider_id)
    if rider is None or rider.is_deleted:
        abort(404)

    return_url = url_for('web.rider_detail', namespace_slug=namespace.slug, rider_id=rider_id)
    if request.form.get('return_to') == 'identity_review':
        flags = list(dict.fromkeys(request.form.getlist('return_flag')))
        if any(flag not in IDENTITY_REVIEW_FILTERS for flag in flags):
            abort(400)
        return_url = url_for('web.riders_review', namespace_slug=namespace.slug, tab='identity',
                             filtered=1, flag=flags, q=(request.form.get('return_q') or '').strip(),
                             page=max(request.form.get('return_page', 1, type=int) or 1, 1))
    slug = request_rider_slug(rider.pcs_slug, rider.first_name, rider.last_name)
    if not slug:
        flash('Save a valid PCS slug before checking this rider’s identity.', 'error')
        return redirect(return_url)

    create_manual_external_group(
        [dict(provider='pcs', endpoint=PROFILE_ENDPOINT, params={'rider_slug': slug},
              related_to_table='rider', related_to_id=rider.cyclist_id)],
        namespace_id=namespace.id, executed_by_id=current_user.id, provider='pcs',
        message=f'PCS rider profile for {slug}'[:255],
    )
    flash('PCS identity check queued.', 'info')
    return redirect(return_url)


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
    rider_external_calls = None
    if _can_read_external_call_groups():
        rider_external_calls = ExternalCallTask.query.with_entities(
            ExternalCallTask.id, ExternalCallTask.task_group_id, ExternalCallTask.created_at,
            ExternalCallTask.provider, ExternalCallTask.endpoint, ExternalCallTask.request_url,
            ExternalCallTask.status, ExternalCallTask.outcome, ExternalCallTask.http_status,
        ).join(ExternalCallTask.task_group).filter(
            ExternalCallTaskGroup.namespace_id == selected_namespace.id,
            ExternalCallTask.related_to_table == 'rider',
            ExternalCallTask.related_to_id == rider.cyclist_id,
        ).order_by(ExternalCallTask.created_at.desc(), ExternalCallTask.id.desc()).all()
    rider_history = _load_attribute_review(
        selected_namespace, 'rider', include_history=True, applied_entity_id=rider.cyclist_id,
    )
    attribute_history_at_limit = len(rider_history) == 200
    # Legacy stat edits record the applied values, without per-edit timestamps or actors.
    rider_history.extend({
        'event_type': 'applied',
        'attribute_name': edit['stat'],
        'before_value': edit['original_value'],
        'after_value': edit['final_value'],
        'source': edit['source']['type'] if edit['source']['type'] == 'Individual' else edit['source']['label'],
        'source_url': edit['source']['url'] if edit['source']['type'] != 'Individual' else None,
        'status': 'approved',
        'release_status': edit['release_status'],
    } for edit in rider_stat_edits if edit['is_approved'])

    back_team_id = request.args.get('team_id', type=int)
    if back_team_id is None and rider.team_id is not None:
        back_team_id = rider.team_id
    referrer_path = ''
    if request.referrer:
        parsed_referrer = urllib.parse.urlsplit(request.referrer)
        if not parsed_referrer.netloc or parsed_referrer.netloc == request.host:
            referrer_path = parsed_referrer.path or ''
    came_from_riders = referrer_path.endswith('/riders')

    attribute_context = _attribute_editor_context(selected_namespace, 'rider', rider_id)
    pcs_slug_display = (rider.pcs_slug or '').strip()
    if pcs_slug_display in ('', '0'):
        pcs_slug_display, _ = _assumed_pcs_profile(rider.first_name, rider.last_name)
        pcs_slug_field = attribute_context['attribute_field_map'].get('pcs_slug')
        if pcs_slug_field is not None:
            pcs_slug_field['value'] = pcs_slug_display

    pcs_profile = PcsRider.query.filter_by(rider_slug=rider.pcs_slug).first() if valid_rider_slug(rider.pcs_slug) else None
    debug_sources = {'CDB': getattr(rider, 'debug_columns', {})}
    for label, profile in [('FC', getattr(rider, 'firstcycling_rider', None)), ('PCS', pcs_profile)]:
        if profile is not None:
            debug_sources[label] = {
                column.name: value.isoformat() if isinstance(value, (date, datetime)) else value
                for column in profile.__table__.columns
                for value in [getattr(profile, column.key)]
            }

    profile_context = dict(
        **attribute_context,
        rider=rider,
        rider_debug_sources=debug_sources,
        pcs_slug_display=pcs_slug_display,
        team=team,
        rider_stats=rider_stats,
        rider_measurements=rider_measurements,
        pending_stats=[edit for edit in rider_stat_edits if not edit['is_approved']],
        rider_results=_load_firstcycling_rider_results(getattr(rider, 'first_cycling_id', None)),
        pcs_activity=_rider_pcs_activity(rider.pcs_slug),
        rider_external_calls=rider_external_calls,
        rider_history=rider_history,
        attribute_history_at_limit=attribute_history_at_limit,
        selected_namespace=selected_namespace,
        can_check_identity=(_can_check_rider_identity() and not rider.is_deleted
                            and bool(request_rider_slug(rider.pcs_slug, rider.first_name, rider.last_name))),
        stat_fields=STAT_DELTA_FIELDS,
        back_url=url_for('web.riders' if came_from_riders else 'web.teams'),
        back_label='Back to Riders' if came_from_riders else 'Back to Teams',
        back_params={
            'team_id': back_team_id,
            'division': request.args.get('division', type=str) or '',
        },
    )
    individual = _load_individual_stat_editor(selected_namespace, rider.cyclist_id, rider_stats)
    return render_template(
        'stat_list_detail.html', profile_context=profile_context, **profile_context,
        stat_list={'id': 0, 'list_name': rider.name},
        rows=[{'rider_id': rider.cyclist_id, 'rider_link_id': rider.cyclist_id,
               'rider_name': rider.name, 'rider_in_namespace': True,
               'locked_stats': individual['locked_stats'],
               'stats': individual['stats'], 'original_stats': individual['original_stats']}],
        scope_delta_status=individual['status'],
        stat_conflicts=individual['conflicts'], is_submitted_for_approval=individual['submitted'],
        editable_rider_fields=individual['editable_rider_fields'],
        can_update_stats=_can_update_stats(), can_approve_stats=_can_approve_stats(),
    )


INDIVIDUAL_STAT_SCOPE = 'race_result_id IS NULL AND stat_list_id IS NULL AND rider_id = :rider_id'


def _rider_pcs_activity(slug):
    """Show saved source facts only for the selected rider's exact PCS mapping."""
    if not valid_rider_slug(slug):
        return []
    profile = PcsRider.query.filter_by(rider_slug=slug).first()
    if profile is None:
        return []
    race_class = ' '.join(value.strip() for value in (
        profile.latest_participation_race_type, profile.latest_participation_race_category,
    ) if value and value.strip())
    race_name = (profile.latest_participation_race_name or '').strip()
    last_race = ' - '.join(value for value in (race_class, race_name) if value)
    stage = ' - '.join(str(value) for value in (
        profile.latest_participation_stage_number, profile.latest_participation_stage_name,
    ) if value is not None and value != '')
    return [
        ('Days since last race', (date.today() - profile.latest_participation_date).days
         if profile.latest_participation_date and profile.latest_participation_date <= date.today() else None),
        ('Last race date', profile.latest_participation_date),
        ('Last race', last_race),
        ('Stage', stage),
        ('Position', profile.latest_participation_result_position),
        ('Participation status', profile.latest_participation_status),
        ('Last PCS sync (UTC)', profile.last_synced_at.strftime('%Y-%m-%d %H:%M:%S') if profile.last_synced_at else None),
    ]


def _individual_pending_fingerprint(conn, rider_id):
    rows = conn.execute(text(f'''
        SELECT row_to_json(changes)::text FROM stat_changes changes
        WHERE {INDIVIDUAL_STAT_SCOPE} AND is_approved = FALSE ORDER BY id
    '''), {'rider_id': rider_id}).scalars().all()
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest() if rows else None


def _individual_submission_matches(namespace, rider_id, fingerprint):
    if not fingerprint:
        return False
    submission = AuditLog.query.filter_by(
        action='individual_stats_submitted',
        resource_type=f'namespace:{namespace.id}:individual', resource_id=rider_id,
    ).filter(AuditLog.details['fingerprint'].as_string() == fingerprint).first()
    return submission is not None


def _load_individual_stat_editor(namespace, rider_id, current_stats):
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            edits = conn.execute(text(f'''
                SELECT stat_name, original_value, final_value, is_approved,
                    COALESCE((to_jsonb(stat_changes)->>'is_applied_immediately')::boolean, FALSE)
                        AS is_applied_immediately
                FROM stat_changes WHERE {INDIVIDUAL_STAT_SCOPE} AND release_id IS NULL ORDER BY id
            '''), {'rider_id': rider_id}).mappings().all()
            conflicts = _load_scope_stat_change_conflicts(conn, INDIVIDUAL_STAT_SCOPE, {'rider_id': rider_id})
            fingerprint = _individual_pending_fingerprint(conn, rider_id)
            editable = _editable_stat_fields(_stat_field_settings(conn))
        original = dict(current_stats)
        final = dict(current_stats)
        for edit in edits:
            original[edit['stat_name']] = edit['original_value']
            final[edit['stat_name']] = edit['final_value']
        return {
            'original_stats': original, 'stats': final, 'conflicts': conflicts,
            'editable_rider_fields': editable,
            'locked_stats': {edit['stat_name'] for edit in edits if edit['is_applied_immediately']},
            'submitted': _individual_submission_matches(namespace, rider_id, fingerprint),
            'status': {'approved_changes': sum(bool(edit['is_approved']) for edit in edits),
                       'unapproved_changes': sum(not edit['is_approved'] for edit in edits)},
        }
    finally:
        engine.dispose()


@web_bp.route('/riders/<int:rider_id>/stats/<action>', methods=['POST'])
@login_required
def rider_stats_action(rider_id, action):
    if action not in ('save', 'submit', 'approve'):
        abort(404)
    if not (_can_approve_stats() if action == 'approve' else _can_update_stats()):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None or _load_namespace_roster_rider(namespace, rider_id) is None:
        abort(404)
    changes = []
    if action == 'save':
        try:
            changes = json.loads(request.form.get('changes_json', ''))
            if not isinstance(changes, list) or any(
                not isinstance(item, dict)
                or _safe_int(item.get('first_cycling_rider_id')) != rider_id
                or item.get('stat_name') not in STAT_DELTA_FIELDS
                or _safe_int(item.get('final_value')) is None
                or not STAT_MIN_VALUE <= _safe_int(item['final_value']) <= STAT_MAX_VALUE
                for item in changes
            ):
                abort(400)
        except (ValueError, TypeError):
            abort(400)
    engine = create_engine(namespace.build_connection_string())
    submission_fingerprint = None
    immediate_ids = []
    try:
        with engine.begin() as conn:
            # Serialize individual saves and approvals for this rider within this namespace.
            _ensure_namespace_database_connection(conn, namespace)
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': -rider_id})
            params = {'rider_id': rider_id}
            if action == 'save':
                settings = _stat_field_settings(conn, lock=True)
                _require_editable_stat_changes(settings, changes)
                if not conn.execute(text(f'SELECT 1 FROM stat_changes WHERE {INDIVIDUAL_STAT_SCOPE} AND release_id IS NULL LIMIT 1'), params).first():
                    conn.execute(text(f'DELETE FROM stat_change_snapshot WHERE {INDIVIDUAL_STAT_SCOPE}'), params)
                _ensure_scope_stat_snapshots(conn, rider_ids=[rider_id])
                snapshot = conn.execute(text(f'''
                    SELECT {_snapshot_stat_select_list()} FROM stat_change_snapshot
                    WHERE {INDIVIDUAL_STAT_SCOPE}
                '''), params).mappings().one()
                for item in changes:
                    stat = item['stat_name']
                    final = _safe_int(item['final_value'])
                    original = snapshot[STAT_COLUMN_MAP[stat]]
                    _require_unapplied_stat(conn, INDIVIDUAL_STAT_SCOPE, params, rider_id, stat)
                    conn.execute(text(f'''
                        DELETE FROM stat_changes WHERE {INDIVIDUAL_STAT_SCOPE}
                        AND stat_name = :stat AND release_id IS NULL
                    '''), {**params, 'stat': stat})
                    if final != original or item.get('is_confilict_override'):
                        change_id = conn.execute(text('''
                            INSERT INTO stat_changes (rider_id, stat_name, original_value,
                                final_value, delta_value, is_approved, is_confilict_override)
                            VALUES (:rider_id, :stat, :original, :final, :delta, FALSE, :override)
                            RETURNING id
                        '''), {**params, 'stat': stat, 'original': original, 'final': final,
                               'delta': final - original, 'override': bool(item.get('is_confilict_override'))}).scalar_one()
                        if _stat_applies_immediately(settings, stat):
                            immediate_ids.append(change_id)
                _apply_immediate_stats(conn, immediate_ids)
                submission_fingerprint = _individual_pending_fingerprint(conn, rider_id)
                refresh_pending_flags(conn, [rider_id])
                if submission_fingerprint:
                    # Persist the submission while the namespace rider lock is
                    # still held. A failed audit write must roll back the edits.
                    AuditLog.log_action(
                        'individual_stats_submitted', current_user,
                        resource_type=f'namespace:{namespace.id}:individual', resource_id=rider_id,
                        details={'fingerprint': submission_fingerprint}, ip_address=request.remote_addr,
                        user_agent=request.headers.get('User-Agent'),
                    )
                    db.session.commit()
            else:
                fingerprint = _individual_pending_fingerprint(conn, rider_id)
                if action == 'submit':
                    if fingerprint:
                        AuditLog.log_action(
                            'individual_stats_submitted', current_user,
                            resource_type=f'namespace:{namespace.id}:individual', resource_id=rider_id,
                            details={'fingerprint': fingerprint}, ip_address=request.remote_addr,
                            user_agent=request.headers.get('User-Agent'),
                        )
                        db.session.commit()
                        flash('Submitted for approval.', 'success')
                    else:
                        flash('No pending stat edits to submit.', 'info')
                elif not _individual_submission_matches(namespace, rider_id, fingerprint):
                    flash('Submit the current saved changes for approval before approving.', 'warning')
                elif _load_scope_stat_change_conflicts(conn, INDIVIDUAL_STAT_SCOPE, params):
                    flash('Resolve stat conflicts before approving.', 'warning')
                else:
                    _apply_scope_stat_changes_to_cdb(conn, INDIVIDUAL_STAT_SCOPE, params)
                    conn.execute(text(f'UPDATE stat_changes SET is_approved = TRUE WHERE {INDIVIDUAL_STAT_SCOPE} AND is_approved = FALSE'), params)
                    refresh_pending_flags(conn, [rider_id])
                    AuditLog.log_action(
                        'individual_stats_approved', current_user,
                        resource_type=f'namespace:{namespace.id}:individual', resource_id=rider_id,
                        ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'),
                    )
                    db.session.commit()
                    flash('Approved stat changes.', 'success')
        if action == 'save':
            if submission_fingerprint:
                flash('Saved and submitted pending stat changes for approval.', 'success')
            elif immediate_ids:
                flash('Stat changes applied.', 'success')
            else:
                flash('No pending stat changes to submit.', 'info')
    finally:
        engine.dispose()
    return redirect(url_for('web.rider_detail', rider_id=rider_id))

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
    if active_method not in ('by_race', 'by_list', 'log'):
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
    if active_method not in ('by_race', 'by_list', 'log'):
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
        'view': 'stats',
        'stat_key': '',
        'stat_min': '',
        'stat_max': '',
        'activity_status': '',
        'identity_flag': '',
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
    for key in ('q', 'country', 'division', 'team', 'stat_key', 'stat_min', 'stat_max', 'activity_status', 'identity_flag'):
        state[key] = (saved_state.get(key, state[key]) or '').strip()
    if saved_state.get('view') in RIDER_VIEWS:
        state['view'] = saved_state['view']
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



def _load_namespace_catalog_races(namespace):
    if namespace.database_name == db.engines['main'].url.database:
        raise RuntimeError('A race catalog requires an isolated namespace database.')
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            return conn.execute(text('SELECT * FROM race WHERE NOT is_deleted ORDER BY id')).mappings().all()
    finally:
        engine.dispose()


@web_bp.after_request
def publish_committed_identity_pairs(response):
    namespace_url = getattr(g, 'identity_sync_url', None)
    if namespace_url is not None and response.status_code < 400:
        result = publish_namespace_pairs(namespace_url)
        if result['conflicts']:
            flash('Source mapping saved locally; a conflicting shared FC/PCS pair needs review.', 'warning')
        elif result['failed']:
            flash('Source mapping saved locally; shared identity publication will need a retry.', 'warning')
    return response


@web_bp.route('/identities/<kind>/retry', methods=['POST'])
@login_required
def retry_identity_sync(kind):
    if kind not in IDENTITY_MODELS:
        abort(404)
    if not current_user.has_permission('identities', 'update'):
        abort(403)
    namespace_id = request.form.get('namespace_id', type=int)
    namespace = next((item for item in _get_accessible_namespaces() if item.id == namespace_id), None)
    if namespace is None:
        abort(404)
    result = publish_namespace_pairs(namespace.build_connection_string(), retry_conflicts=True)
    flash(f"Published {result['published']} source pair(s); {result['conflicts']} conflict(s).",
          'warning' if result['failed'] or result['conflicts'] else 'success')
    return redirect(url_for('web.identities', kind=kind))


@web_bp.route('/identities/<kind>', methods=['GET', 'POST'])
@login_required
def identities(kind):
    """Review shared provider links; this table has no namespace object IDs."""
    if kind not in IDENTITY_MODELS:
        abort(404)
    if not current_user.has_permission('identities', 'read'):
        abort(403)
    model = IDENTITY_MODELS[kind]
    if request.method == 'POST':
        if not current_user.has_permission('identities', 'update'):
            abort(403)
        identity_id = request.form.get('identity_id', type=int)
        if identity_id is None:
            abort(400)
        table = model.__table__
        try:
            conn = db.session.connection(bind_arguments={'mapper': model})
            conn.execute(text(f'LOCK TABLE {table.name} IN SHARE ROW EXCLUSIVE MODE'))
            before = conn.execute(table.select().where(table.c.id == identity_id)).mappings().first()
            save_identity(conn, kind, request.form, identity_id=identity_id)
            after = conn.execute(table.select().where(table.c.id == identity_id)).mappings().one()
            fields = ('firstcycling_id', 'pcs_slug')
            AuditLog.log_action(
                'identity_updated', current_user, resource_type=table.name, resource_id=identity_id,
                details={'before': {key: before[key] for key in fields},
                         'after': {key: after[key] for key in fields}},
                ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
        except LookupError:
            db.session.rollback()
            abort(404)
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else 'A source ID is already linked to another identity.', 'error')
        else:
            flash('Shared FC/PCS mapping corrected. Namespace source values are unchanged.', 'success')
        return redirect(url_for('web.identities', kind=kind))
    query = model.query
    term = (request.args.get('q') or '').strip()
    if term:
        conditions = [model.pcs_slug.ilike(f'%{term}%')]
        if term.isdigit() and int(term) < 2**31:
            conditions.append(model.firstcycling_id == int(term))
        query = query.filter(or_(*conditions))
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    conflicts = []
    if namespace is not None:
        engine = create_engine(namespace.build_connection_string())
        try:
            with engine.connect() as conn:
                _ensure_namespace_database_connection(conn, namespace)
                if namespace.database_name == db.engines['main'].url.database:
                    abort(400)
                from sqlalchemy import inspect
                if inspect(conn).has_table('identity_sync_outbox'):
                    conflicts = conn.execute(text('''
                        SELECT firstcycling_id, pcs_slug, status, error FROM identity_sync_outbox
                        WHERE entity_type = :kind AND status IN ('pending', 'conflict') ORDER BY id
                    '''), {'kind': kind}).mappings().all()
        finally:
            engine.dispose()
    return render_template('identities.html', kind=kind, identities=query.order_by(model.id).all(),
                           sync_conflicts=conflicts, selected_namespace=namespace,
                           can_update=current_user.has_permission('identities', 'update'))


@web_bp.route('/races', methods=['GET', 'POST'], endpoint='races')
@web_bp.route('/riders/bulk-editor', methods=['GET', 'POST'], endpoint='bulk_editor')
@web_bp.route('/riders/stat-edit-log', methods=['GET', 'POST'], endpoint='stat_edit_log')
@web_bp.route('/stats-editor', methods=['GET', 'POST'])
@login_required
def stats_editor():
    """Stats editor with race results, lists, and a namespace-wide edit log."""
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None
    if request.endpoint == 'web.bulk_editor':
        bulk_tab = request.args.get('tab', 'lists')
        if bulk_tab not in ('lists', 'editor'):
            abort(400)
        if bulk_tab == 'editor':
            if 'list_id' in request.args:
                list_id = request.args.get('list_id', type=int)
                if list_id is None or list_id < 1:
                    abort(404)
                return stats_editor_list_detail(list_id)
            if _can_create_stats() or _can_attribute_action('update'):
                return bulk_editor_new()
            return render_template('bulk_editor_empty.html', selected_namespace=selected_namespace)
    editor_state = _load_stats_editor_state(selected_namespace)
    page_methods = {
        'web.races': 'by_race',
        'web.bulk_editor': 'by_list',
        'web.stat_edit_log': 'log',
    }
    page_endpoints = {method: endpoint for endpoint, method in page_methods.items()}
    if request.endpoint in page_methods:
        editor_state['active_method'] = page_methods[request.endpoint]
        _save_stats_editor_state(selected_namespace, editor_state)
    elif request.method == 'GET':
        return redirect(url_for(page_endpoints[editor_state['active_method']], **request.args))

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
            if requested_method in ('by_race', 'by_list', 'log'):
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
        return redirect(url_for(page_endpoints[next_state['active_method']]))

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

        fc_races = base_query.options(selectinload(FirstCyclingRace.race_stages)).all()
        cdb_races = _load_namespace_catalog_races(selected_namespace)
        catalog = unified_races(cdb_races, fc_races, IDENTITY_MODELS['race'].query.all())
        filter_choices['nations'] = sorted({race.nation_name for race in catalog if race.nation_name})
        races = filter_races(catalog, selected_filters, _normalize_name_search_term)

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
        creator_ids = {item.get('created_by_id') for item in list_entries} - {None}
        creators = {user.id: user.username for user in User.query.filter(User.id.in_(creator_ids)).all()} if creator_ids else {}
        for item in list_entries:
            item.setdefault('list_type', 'stat')
            item['creator_label'] = creators.get(item.get('created_by_id'), 'Unknown creator')
        creator_choices = sorted(
            {(str(item.get('created_by_id') or 'unknown'), item['creator_label']) for item in list_entries},
            key=lambda choice: choice[1].casefold(),
        )
        name_filter = (request.args.get('list_name') or '').strip()
        creator_filter = (request.args.get('creator_id') or '').strip()
        type_filter = (request.args.get('list_type') or '').strip()
        if type_filter not in ('', 'stat', 'attribute'):
            abort(400)
        list_entries = [item for item in list_entries
                        if (not name_filter or name_filter.casefold() in item['list_name'].casefold())
                        and (not creator_filter or str(item.get('created_by_id') or 'unknown') == creator_filter)
                        and (not type_filter or item['list_type'] == type_filter)]
        pending_lists = [item for item in list_entries
                         if item['list_type'] == 'stat'
                         and item.get('total_changes', 0) > item.get('approved_changes', 0)]
        attribute_lists = [item for item in list_entries if item['list_type'] == 'attribute']
        if pending_lists or attribute_lists:
            engine = create_engine(selected_namespace.build_connection_string())
            try:
                with engine.connect() as conn:
                    _ensure_namespace_database_connection(conn, selected_namespace)
                    for item in pending_lists:
                        fingerprint = _list_pending_submission_fingerprint(conn, item['id'])
                        item['status']['is_submitted'] = _list_submission_matches(
                            selected_namespace, item['id'], fingerprint,
                        )
                    attribute_summaries = _attribute_list_summaries(conn, [item['id'] for item in attribute_lists])
                    for item in attribute_lists:
                        item['approval_status'], item['total_changes'] = attribute_summaries.get(item['id'], ('Draft', 0))
            finally:
                engine.dispose()

    log_page = max(1, request.args.get('log_page', 1, type=int))
    log_entries = []
    if active_method == 'log' and selected_namespace is not None:
        log_entries = _load_namespace_approved_release_items(
            selected_namespace, log_mode=True, limit=101, offset=(log_page - 1) * 100,
        )

    return render_template(
        'stats_editor.html',
        creator_choices=creator_choices if active_method == 'by_list' else [],
        log_entries=log_entries[:100],
        log_has_next=len(log_entries) > 100,
        log_page=log_page,
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
        can_manage_identities=current_user.has_permission('identities', 'read'),
        can_create_stats=_can_create_stats() or _can_attribute_action('update'),
    )


def _search_stat_list_riders(namespace, rider_ids, list_search_rider, list_search_team):
    selected_namespace = namespace
    list_search_results = []
    engine = create_engine(namespace.build_connection_string())
    try:
        if list_search_rider or list_search_team:
            search_where = []
            search_params = {
                'selected_cdb_ids': list(rider_ids) or [-1],
            }
            if list_search_rider:
                search_where.append(
                    "(" + _normalized_name_sql('c.first_name') + " LIKE :rider_term"
                    + " OR " + _normalized_name_sql('c.last_name') + " LIKE :rider_term"
                    + " OR " + _normalized_name_sql('c.first_last_name') + " LIKE :rider_term"
                    + " OR " + _normalized_name_sql("CONCAT_WS(' ', c.first_name, c.last_name)") + " LIKE :rider_term)"
                )
                search_params['rider_term'] = f'%{_normalize_name_search_term(list_search_rider)}%'
            if list_search_team:
                search_where.append(
                    """
                    (
                        """ + _normalized_name_sql('t.team_name') + """ LIKE :team_term
                        OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_term
                        OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_term
                    )
                    """
                )
                search_params['team_term'] = f'%{_normalize_name_search_term(list_search_team)}%'

            search_filter = ' AND '.join(search_where) if search_where else 'TRUE'
            try:
                with engine.connect() as conn:
                    _ensure_namespace_database_connection(conn, namespace)
                    search_rows = conn.execute(
                        text(
                            f"""
                            SELECT
                                c.id AS cyclist_id,
                                c.first_cycling_id,
                                c.first_name,
                                c.last_name,
                                c.first_last_name,
                                c.birth_date,
                                c.size_cm,
                                c.weight_kg,
                                c.team_id,
                                COALESCE(t.team_name, t.short_name) AS team_name,
                                COALESCE(NULLIF(BTRIM(country.flag_name), ''), NULLIF(BTRIM(country.constant), ''), country.id::text) AS nation_name,
                                {_snapshot_stat_select_list('c')}
                            FROM rider c
                            LEFT JOIN team t ON t.id = c.team_id
                            LEFT JOIN region region ON region.id = c.region_id
                            LEFT JOIN country country ON country.id = region.country_id
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

            for row in search_rows:
                rider = _namespace_roster_rider_from_row(row)
                rider.stats = _snapshot_stats_from_row(row)
                list_search_results.append(rider)

    finally:
        engine.dispose()
    return list_search_results


@web_bp.route('/riders/bulk-editor/new')
@login_required
def bulk_editor_new():
    list_type = request.args.get('list_type', 'stat' if _can_create_stats() else 'attribute')
    if list_type not in ('stat', 'attribute'):
        abort(400)
    can_create_attribute_list = _can_attribute_action('update')
    if not _can_rider_list_action(list_type, 'create'):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    year, gender = _resolve_namespace_season(namespace)
    if namespace is None or year is None or not gender:
        flash('Select a namespace with Database Year and Gender before starting a list.', 'warning')
        return redirect(url_for('web.bulk_editor'))
    rider_term = (request.args.get('list_search_rider') or '').strip()
    team_term = (request.args.get('list_search_team') or '').strip()
    return render_template(
        'bulk_editor_new.html', new_list=True, selected_namespace=namespace,
        list_type=list_type, can_create_attribute_list=can_create_attribute_list,
        can_create_stat_list=_can_create_stats(),
        draft_list_name=request.args.get('list_name', '').strip()[:120],
        list_search_rider=rider_term, list_search_team=team_term,
        list_search_results=_search_stat_list_riders(namespace, [], rider_term, team_term),
        list_item_rider_ids=set(), stat_fields=STAT_DELTA_FIELDS,
    )


@web_bp.route('/stats-editor/lists/<int:list_id>/rename', methods=['POST'])
@login_required
def stats_editor_rename_list(list_id):
    if not (_can_create_stats() or _can_attribute_action('update')):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    name = (request.form.get('list_name') or '').strip()
    if not name or len(name) > 120:
        flash('Enter a list name between 1 and 120 characters.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.begin() as conn:
            list_type = _lock_editable_stat_list(conn, namespace, list_id)
            if not _can_rider_list_action(list_type, 'create'):
                abort(403)
            _ensure_namespace_database_connection(conn, namespace)
            row = conn.execute(text('UPDATE rider_list SET list_name=:name WHERE id=:id RETURNING id'),
                               {'name': name, 'id': list_id}).first()
            if row is None:
                abort(404)
    finally:
        engine.dispose()
    return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))


@web_bp.route('/stats-editor/lists/create', methods=['POST'])
@login_required
def stats_editor_create_list():
    list_type = request.form.get('list_type', 'stat')
    if list_type not in ('stat', 'attribute'):
        abort(400)
    if not _can_rider_list_action(list_type, 'create'):
        abort(403)

    list_name = (request.form.get('list_name', type=str) or '').strip()
    rider_id = request.form.get('rider_id', type=int)
    bulk_selection = 'rider_ids' in request.form
    if bulk_selection:
        if not _can_access_panel('stat_editor'):
            abort(403)
        rider_ids = _parse_bulk_rider_ids(request.form['rider_ids'])
    else:
        rider_ids = [rider_id] if rider_id is not None else []
    if not bulk_selection and not list_name and not rider_ids:
        flash('List Name is required.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_type=list_type))

    if len(list_name) > 120:
        flash('List names must be 120 characters or fewer.', 'warning')
        return redirect(url_for('web.bulk_editor_new'))
    if not list_name:
        list_name = f'Draft list - {datetime.utcnow():%Y-%m-%d %H:%M:%S} - {secrets.token_hex(3)}'

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))
    if bulk_selection and (not rider_ids or 'rider_id' in request.form):
        flash(f'Select between 1 and {MAX_BULK_TASK_RIDERS} valid riders.', 'warning')
        return redirect(url_for('web.riders'))

    season_year, gender = _resolve_namespace_season(selected_namespace)
    if season_year is None or not gender:
        flash('Selected namespace must have Database Year and Gender before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, selected_namespace)
            if bulk_selection:
                available_count = conn.scalar(text("""
                    SELECT COUNT(*) FROM rider r WHERE id = ANY(:rider_ids)
                    AND COALESCE((to_jsonb(r)->>'is_deleted')::boolean, FALSE) IS FALSE
                """), {'rider_ids': rider_ids})
                if available_count != len(rider_ids):
                    flash('One or more selected riders are unavailable in this namespace.', 'warning')
                    return redirect(url_for('web.riders'))
            elif rider_id is not None:
                rider_exists = conn.execute(text('SELECT id FROM rider WHERE id=:id'), {'id': rider_id}).first()
                if rider_exists is None:
                    flash('That rider was not found in this namespace CDB.', 'warning')
                    return redirect(url_for('web.bulk_editor_new'))
            stat_list_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'rider_list'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                raise RuntimeError('rider_list table is missing in namespace database')
            if list_type == 'attribute' and 'list_type' not in stat_list_columns:
                flash('This namespace needs the Bulk Editor list type schema update before creating Attribute Lists.', 'error')
                return redirect(url_for('web.bulk_editor_new', list_type=list_type))

            insert_columns = ['list_name']
            insert_values = [':list_name']
            insert_params = {'list_name': list_name}

            if 'list_type' in stat_list_columns:
                insert_columns.append('list_type')
                insert_values.append(':list_type')
                insert_params['list_type'] = list_type

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
                    "INSERT INTO rider_list ({columns}) VALUES ({values}) RETURNING id".format(
                        columns=', '.join(insert_columns),
                        values=', '.join(insert_values),
                    )
                ),
                insert_params,
            ).mappings().first()
            if rider_ids:
                conn.execute(text('INSERT INTO rider_list_item (rider_list_id, rider_id) VALUES (:list_id, :rider_id)'),
                             [{'list_id': row['id'], 'rider_id': rider_id} for rider_id in rider_ids])
        new_list_id = row['id'] if row else None
    except Exception:
        logger.exception('Failed to create stat list in namespace %s', selected_namespace.slug)
        flash('Failed to create stat list.', 'error')
        return redirect(url_for('web.stats_editor'))
    finally:
        engine.dispose()

    flash(f"Created list '{list_name}'.", 'success')
    return redirect(url_for(
        'web.bulk_editor', tab='editor', list_id=new_list_id,
        list_search_rider=request.form.get('list_search_rider', ''),
        list_search_team=request.form.get('list_search_team', ''),
    ))


@web_bp.route('/stats-editor/lists/<int:list_id>/add-rider', methods=['POST'])
@login_required
def stats_editor_add_list_rider(list_id):
    if not (_can_update_stats() or _can_attribute_action('update')):
        abort(403)

    rider_id = request.form.get('rider_id', type=int)
    first_cycling_id = request.form.get('first_cycling_id', type=int)
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if rider_id is None and first_cycling_id is None:
        flash('Select a rider to add.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before adding riders.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            list_type = _lock_editable_stat_list(conn, selected_namespace, list_id)
            if not _can_rider_list_action(list_type, 'update'):
                abort(403)
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM rider
                    WHERE (:rider_id IS NOT NULL AND id = :rider_id)
                       OR (:rider_id IS NULL AND first_cycling_id = :first_cycling_id)
                    LIMIT 1
                    """
                ),
                {
                    'rider_id': rider_id,
                    'first_cycling_id': first_cycling_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider was not found in this namespace CDB.', 'warning')
                return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

            conn.execute(
                text(
                    """
                    INSERT INTO rider_list_item (rider_list_id, rider_id)
                    VALUES (:list_id, :rider_id)
                    ON CONFLICT (rider_list_id, rider_id)
                    DO NOTHING
                    """
                ),
                {
                    'list_id': list_id,
                    'rider_id': rider_row['id'],
                },
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to add rider to stat list in namespace %s', selected_namespace.slug)
        flash('Failed to add rider to list.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


@web_bp.route('/stats-editor/lists/<int:list_id>/toggle-rider', methods=['POST'])
@login_required
def stats_editor_toggle_list_rider(list_id):
    if not (_can_update_stats() or _can_attribute_action('update')):
        abort(403)

    rider_id = request.form.get('rider_id', type=int)
    first_cycling_id = request.form.get('first_cycling_id', type=int)
    is_checked = request.form.get('is_checked', type=str) == '1'
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if rider_id is None and first_cycling_id is None:
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before editing list riders.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            list_type = _lock_editable_stat_list(conn, selected_namespace, list_id)
            if not _can_rider_list_action(list_type, 'update'):
                abort(403)
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM rider
                    WHERE (:rider_id IS NOT NULL AND id = :rider_id)
                       OR (:rider_id IS NULL AND first_cycling_id = :first_cycling_id)
                    LIMIT 1
                    """
                ),
                {
                    'rider_id': rider_id,
                    'first_cycling_id': first_cycling_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider is not linked in this namespace CDB yet.', 'warning')
                return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))
            rider_id = rider_row['id']

            if is_checked:
                conn.execute(
                    text(
                        """
                        INSERT INTO rider_list_item (rider_list_id, rider_id)
                        VALUES (:list_id, :rider_id)
                        ON CONFLICT (rider_list_id, rider_id)
                        DO NOTHING
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                    },
                )
            else:
                if list_type == 'attribute' and conn.execute(text('''
                    SELECT 1 FROM cdb_changes
                    WHERE stat_list_id = :list_id AND entity_type = 'rider' AND entity_id = :rider_id
                      AND source = 'attribute_editor' AND status = 'pending'
                '''), {'list_id': list_id, 'rider_id': rider_id}).first() is not None:
                    abort(409, description='Review this rider\'s pending list changes before removing the rider.')
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE stat_list_id = :list_id
                          AND race_result_id IS NULL
                          AND rider_id = :rider_id
                          AND NOT is_approved
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
                        DELETE FROM rider_list_item
                        WHERE rider_list_id = :list_id
                          AND rider_id = :rider_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                    },
                )
            refresh_pending_flags(conn, [rider_id])
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to toggle rider in stat list in namespace %s', selected_namespace.slug)
        flash('Failed to update list rider selection.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


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
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, selected_namespace)
            list_row = conn.execute(
                text(
                    """
                    SELECT id, list_name, season_year, gender,
                           COALESCE(to_jsonb(l)->>'list_type', 'stat') AS list_type
                    FROM rider_list l
                    WHERE id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().first()
            if list_row is None:
                abort(404)
            is_attribute_list = list_row['list_type'] == 'attribute'
            list_is_approved = not is_attribute_list and _stat_list_is_approved(conn, list_id)
            rider_ids = conn.execute(
                text(
                    """
                    SELECT rider_id FROM rider_list_item WHERE rider_list_id = :list_id ORDER BY id
                    """
                ),
                {'list_id': list_id},
            ).scalars().all()
            if is_attribute_list:
                approval_status, _ = _attribute_list_summaries(conn, [list_id]).get(list_id, ('Draft', 0))
                pending_attribute_changes = attribute_changes.pending_list_changes(conn, list_id)
                attribute_configuration = field_configuration.stored_fields(conn, 'rider')
                can_approve_attribute_list = bool(pending_attribute_changes) and all(
                    current_user.has_permission(attribute_configuration.get(('rider', change['attribute_name']), {}).get(
                        'slug', field_configuration.field_slug('rider', change['attribute_name']),
                    ), 'approve') for change in pending_attribute_changes
                )
                attribute_fields = attribute_changes.editor_fields_by_id(conn, 'rider', rider_ids, current_user.has_permission)
                attribute_names = attribute_changes.editable_attributes(
                    'rider', field_configuration.stored_fields(conn, 'rider'),
                )
            editable_rider_fields = _editable_stat_fields(_stat_field_settings(conn))
    finally:
        engine.dispose()

    list_search_rider = (request.args.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.args.get('list_search_team', type=str) or '').strip()
    list_item_rider_ids = set(rider_ids)
    rider_rows = []
    if rider_ids:
        try:
            with engine.connect() as conn:
                _ensure_namespace_database_connection(conn, selected_namespace)
                rider_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            c.id AS rider_id,
                            c.first_cycling_id,
                            c.first_name,
                            c.last_name,
                            c.first_last_name,
                            c.team_id,
                            COALESCE(t.team_name, t.short_name) AS team_name,
                            {_snapshot_stat_select_list('c')}
                        FROM rider c
                        LEFT JOIN team t ON t.id = c.team_id
                        WHERE c.id = ANY(:rider_ids)
                        ORDER BY c.id ASC
                        """
                    ),
                    {'rider_ids': rider_ids},
                ).mappings().all()
        except Exception:
            logger.exception('Failed to load CDB cyclists for stat list %s in namespace %s', list_id, selected_namespace.slug)
            rider_rows = []
        finally:
            engine.dispose()
    rider_map = {
        row['rider_id']: row
        for row in rider_rows
    }

    list_search_results = _search_stat_list_riders(
        selected_namespace, rider_ids, list_search_rider, list_search_team,
    )

    if is_attribute_list:
        is_submitted_for_approval = False
        scope_delta_status = stat_conflicts = None
    else:
        rider_snapshot_map = _load_namespace_scope_snapshots(selected_namespace, stat_list_id=list_id, rider_ids=rider_ids)
        rider_delta_map = _load_namespace_list_result_deltas(selected_namespace, list_id, rider_ids)
        scope_delta_status = _load_namespace_scope_delta_status(
            selected_namespace, first_cycling_race_id=None, first_cycling_stage_id=None, stat_list_id=list_id,
        )
        stat_conflicts = _load_namespace_scope_conflicts(selected_namespace, stat_list_id=list_id)
        is_submitted_for_approval = _list_is_submitted_for_approval(selected_namespace, list_id)
        approval_status = 'Approved' if list_is_approved else (
            'Approval pending' if is_submitted_for_approval else 'Draft'
        )

    rows = []
    for rider_id in rider_ids:
        rider = rider_map.get(rider_id)
        rider_name = _cdb_rider_display_name(rider) if rider is not None else ''
        rider_name = rider_name or f'Rider #{rider_id}'

        rider_in_namespace = rider is not None
        if rider_in_namespace and not is_attribute_list:
            base_stats = _snapshot_stats_from_row(rider)
            snapshot_stats = rider_snapshot_map.get(rider_id) or {}
            original_stats = dict(snapshot_stats or base_stats)
            final_stats = dict(original_stats)
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
            'rider_link_id': rider_id,
            'rider_name': rider_name,
            'team_id': rider.get('team_id') if rider is not None else None,
            'team_name': (rider.get('team_name') if rider is not None and rider.get('team_name') else '-'),
            'rider_in_namespace': rider_in_namespace,
            'stats': final_stats,
            'original_stats': original_stats,
            'locked_stats': {name for name, change in (rider_delta_map.get(rider_id) or {}).items()
                             if change.get('is_applied_immediately')} if not is_attribute_list else set(),
        })

    return render_template(
        'stat_list_detail.html',
        stat_list=list_row,
        approval_status=approval_status,
        is_attribute_list=is_attribute_list,
        attribute_names=attribute_names if is_attribute_list else [],
        editable_rider_fields=editable_rider_fields,
        attribute_fields={rider_id: {field['name']: field for field in fields}
                          for rider_id, fields in attribute_fields.items()} if is_attribute_list else {},
        can_edit_attributes=is_attribute_list and _can_attribute_action('update'),
        can_approve_attributes=is_attribute_list and _can_attribute_action('approve'),
        can_approve_attribute_list=is_attribute_list and can_approve_attribute_list,
        list_is_approved=list_is_approved,
        can_manage_list=_can_rider_list_action(list_row['list_type'], 'update') and not list_is_approved,
        can_start_list=_can_create_stats() or _can_attribute_action('update'),
        can_create_stats=_can_rider_list_action(list_row['list_type'], 'create') and not list_is_approved,
        is_submitted_for_approval=is_submitted_for_approval,
        rows=rows,
        list_search_rider=list_search_rider,
        list_search_team=list_search_team,
        list_search_results=list_search_results,
        list_item_rider_ids=list_item_rider_ids,
        selected_namespace=selected_namespace,
        scope_delta_status=scope_delta_status,
        stat_conflicts=stat_conflicts,
        stat_fields=STAT_DELTA_FIELDS,
        can_approve_stats=not is_attribute_list and _can_approve_stats() and not list_is_approved,
        can_update_stats=not is_attribute_list and _can_update_stats() and not list_is_approved,
    )


@web_bp.route('/stats-editor/lists/<int:list_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_list_stat_deltas(list_id):
    if not _can_update_stats():
        abort(403)

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))


    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))

    rider_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    rider_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        saved_count = 0
        skipped_count = 0
        with engine.begin() as conn:
            _lock_editable_stat_list(conn, selected_namespace, list_id, required_type='stat')
            settings = _stat_field_settings(conn, lock=True)
            _require_editable_stat_changes(settings, parsed_changes)
            immediate_ids = []
            snapshot_rider_rows = conn.execute(
                text(
                    """
                    SELECT rider_id
                    FROM rider_list_item
                    WHERE rider_list_id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().all()
            snapshot_cdb_ids = {
                _safe_int(row.get('rider_id'))
                for row in snapshot_rider_rows
            }
            snapshot_cdb_ids.discard(None)
            _ensure_scope_stat_snapshots(conn, stat_list_id=list_id, rider_ids=snapshot_cdb_ids)
            snapshot_rows = conn.execute(
                text(
                    f"""
                    SELECT rider_id, {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE race_result_id IS NULL
                      AND stat_list_id = :list_id
                      AND rider_id = ANY(:rider_ids)
                    """
                ),
                {'list_id': list_id, 'rider_ids': list(rider_ids)},
            ).mappings().all()
            snapshot_map = {
                row['rider_id']: _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                rider_id = _safe_int(item.get('first_cycling_rider_id'))
                stat_name = (item.get('stat_name') or '').strip().lower()
                final_value = _safe_int(item.get('final_value'))
                original_value = _safe_int((snapshot_map.get(rider_id) or {}).get(stat_name))
                is_confilict_override = bool(item.get('is_confilict_override'))

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

                _require_unapplied_stat(conn, 'race_result_id IS NULL AND stat_list_id = :list_id',
                                        {'list_id': list_id}, rider_id, stat_name)
                delta_value = final_value - original_value
                if delta_value == 0 and not is_confilict_override:
                    conn.execute(
                        text(
                            """
                            DELETE FROM stat_changes
                            WHERE race_result_id IS NULL
                              AND stat_list_id = :list_id
                              AND rider_id = :rider_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'list_id': list_id,
                            'rider_id': rider_id,
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
                          AND rider_id = :rider_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                    },
                )

                change_id = conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            race_result_id,
                            stat_list_id,
                            rider_id,
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
                            :rider_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            :is_confilict_override,
                            FALSE
                        )
                        RETURNING id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                        'is_confilict_override': is_confilict_override,
                    },
                ).scalar_one()
                if _stat_applies_immediately(settings, stat_name):
                    immediate_ids.append(change_id)
                saved_count += 1
            _apply_immediate_stats(conn, immediate_ids)
            refresh_pending_flags(conn, rider_ids)

        if saved_count > 0:
            flash(f'Saved {saved_count} stat change(s).', 'success')
        else:
            flash('No stat changes were saved.', 'info')
        if skipped_count > 0:
            flash(f'Skipped {skipped_count} invalid or unmatched change(s).', 'warning')

    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to save stat edits for list %s', list_id)
        flash('Failed to save stat edits.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))


def _stat_list_is_approved(conn, list_id):
    return bool(conn.execute(text('''
        SELECT EXISTS (SELECT 1 FROM stat_changes
                       WHERE stat_list_id=:id AND is_approved=TRUE
                         AND (to_jsonb(stat_changes)->>'is_applied_immediately') IS DISTINCT FROM 'true')
    '''), {'id': list_id}).scalar_one())


def _lock_editable_stat_list(conn, namespace, list_id, required_type=None):
    """Serialize edits with approval, and reject edits once any batch was approved."""
    _ensure_namespace_database_connection(conn, namespace)
    list_type = conn.execute(text("""
        SELECT COALESCE(to_jsonb(l)->>'list_type', 'stat') FROM rider_list l WHERE id=:id FOR UPDATE
    """), {'id': list_id}).scalar_one_or_none()
    if list_type is None:
        abort(404)
    if required_type is not None and list_type != required_type:
        abort(409, description='This action is unavailable for this list type.')
    if list_type == 'stat' and _stat_list_is_approved(conn, list_id):
        abort(409, description='Approved lists are read-only.')
    return list_type


def _attribute_list_summaries(conn, list_ids):
    if not list_ids:
        return {}
    rows = conn.execute(text("""
        SELECT stat_list_id, COUNT(*) AS total,
               CASE WHEN BOOL_OR(status = 'pending') THEN 'Approval pending'
                    WHEN BOOL_AND(status = 'approved') THEN 'Approved'
                    WHEN BOOL_AND(status = 'rejected') THEN 'Rejected'
                    ELSE 'Reviewed' END AS approval_status
        FROM cdb_changes
        WHERE stat_list_id = ANY(:list_ids) AND source = 'attribute_editor' AND entity_type = 'rider'
        GROUP BY stat_list_id
    """), {'list_ids': list_ids}).mappings()
    return {row['stat_list_id']: (row['approval_status'], row['total']) for row in rows}


def _list_pending_submission_fingerprint(conn, list_id):
    rows = conn.execute(text("""
        SELECT row_to_json(changes)::text
        FROM stat_changes AS changes
        WHERE race_result_id IS NULL AND stat_list_id = :list_id
          AND is_approved = FALSE
        ORDER BY id
    """), {'list_id': list_id}).scalars().all()
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest() if rows else None


def _list_submission_matches(namespace, list_id, fingerprint):
    if not fingerprint or namespace is None:
        return False
    submission = AuditLog.query.filter_by(
        action=AuditAction.STAT_LIST_SUBMITTED,
        resource_type=f'namespace:{namespace.id}:stat_list', resource_id=list_id,
    ).order_by(AuditLog.id.desc()).first()
    return submission is not None and (submission.details or {}).get('fingerprint') == fingerprint


def _list_is_submitted_for_approval(namespace, list_id):
    if namespace is None:
        return False
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            fingerprint = _list_pending_submission_fingerprint(conn, list_id)
        return _list_submission_matches(namespace, list_id, fingerprint)
    except Exception:
        logger.exception('Failed to load submission state for stat list %s', list_id)
        return False
    finally:
        engine.dispose()


@web_bp.route('/stats-editor/lists/<int:list_id>/submit-for-approval', methods=['POST'])
@login_required
def stats_editor_submit_list_for_approval(list_id):
    if not _can_update_stats():
        abort(403)
    target = url_for('web.bulk_editor', tab='editor', list_id=list_id)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before submitting stat changes.', 'warning')
        return redirect(target)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.begin() as conn:
            _lock_editable_stat_list(conn, namespace, list_id, required_type='stat')
            list_name = conn.execute(text('SELECT list_name FROM rider_list WHERE id = :list_id'),
                                     {'list_id': list_id}).scalar_one_or_none()
            if list_name is None:
                abort(404)
            fingerprint = _list_pending_submission_fingerprint(conn, list_id)
            pending_count = conn.execute(text("""
                SELECT COUNT(*) FROM stat_changes
                WHERE race_result_id IS NULL AND stat_list_id = :list_id AND is_approved = FALSE
            """), {'list_id': list_id}).scalar_one()
        if not fingerprint:
            flash('No pending stat edits to submit.', 'info')
            return redirect(target)
        AuditLog.log_action(
            AuditAction.STAT_LIST_SUBMITTED, current_user,
            resource_type=f'namespace:{namespace.id}:stat_list', resource_id=list_id,
            details={'fingerprint': fingerprint},
            ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()
        try:
            notified = notify_discord_stat_list_saved(
                namespace, list_id, list_name, url_for('web.bulk_editor', tab='editor', list_id=list_id, _external=True),
                pending_count, saved_by=current_user.username,
            )
        except Exception:
            logger.exception('Failed to send Discord submission notification for stat list %s', list_id)
            notified = False
        if notified:
            flash('Submitted for approval. Discord approval thread notified.', 'success')
        else:
            flash('Submitted for approval, but the Discord notification was not sent. Check the Discord integration settings.', 'warning')
    finally:
        engine.dispose()
    return redirect(target)


@web_bp.route('/stats-editor/lists/<int:list_id>/approve-attributes', methods=['POST'])
@login_required
def stats_editor_approve_list_attributes(list_id):
    if not _can_access_panel('stat_editor') or not _can_attribute_action('approve'):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.begin() as conn:
            _lock_editable_stat_list(conn, namespace, list_id, required_type='attribute')
            approved_count = attribute_changes.approve_list(
                conn, list_id, current_user.id, authorize=current_user.has_permission,
            )
    except HTTPException:
        raise
    except PermissionError:
        abort(403)
    except (LookupError, ValueError) as error:
        flash(f'The list could not be approved. {error}', 'error')
    except Exception:
        logger.exception('Failed to approve attribute list %s in namespace %s', list_id, namespace.slug)
        flash('The list could not be approved. Try again.', 'error')
    else:
        if approved_count:
            flash(f'Approved and applied {approved_count} attribute change(s) in this list.', 'success')
        else:
            flash('This list has no pending attribute changes to approve.', 'info')
    finally:
        engine.dispose()
    return redirect(url_for('web.bulk_editor', namespace_slug=namespace.slug, tab='editor', list_id=list_id))


@web_bp.route('/stats-editor/lists/<int:list_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_list_stat_deltas(list_id):
    if not _can_approve_stats():
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    approved_count = 0
    applied_count = 0
    try:
        with engine.begin() as conn:
            _lock_editable_stat_list(conn, selected_namespace, list_id, required_type='stat')
            if not _list_submission_matches(
                selected_namespace, list_id, _list_pending_submission_fingerprint(conn, list_id),
            ):
                flash('Submit the current saved changes for approval before approving this list.', 'warning')
                return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))
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
                return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))

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
            refresh_pending_flags(conn, conn.execute(text(
                'SELECT DISTINCT rider_id FROM stat_changes WHERE race_result_id IS NULL AND stat_list_id=:list_id'
            ), {'list_id': list_id}).scalars().all())
            approved_count = result.rowcount or 0
    except HTTPException:
        raise
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

    return redirect(url_for('web.bulk_editor', tab='editor', list_id=list_id))


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
            'individuals_included': 0,
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
def legacy_race_detail(race_id, stage_id=None):
    """Keep bookmarked race URLs pointing at the first-party race pages."""
    if not _can_access_panel('stat_editor'):
        abort(403)
    _get_selected_namespace(_get_accessible_namespaces())
    target = url_for('web.race_detail', race_id=race_id, stage_id=stage_id)
    if request.query_string:
        target += '?' + request.query_string.decode('latin-1')
    return redirect(target, code=301)


@web_bp.route('/races/cdb/<int:race_id>')
@login_required
def cdb_race_detail(race_id):
    if not _can_access_panel('stat_editor'):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    if namespace.database_name == db.engines['main'].url.database:
        abort(400)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            race = conn.execute(text('SELECT * FROM race WHERE id = :id AND NOT is_deleted'),
                                {'id': race_id}).mappings().first()
            if race is None:
                abort(404)
    finally:
        engine.dispose()
    return render_template('cdb_race_detail.html', race=race,
                           **_attribute_editor_context(namespace, 'race', race_id))


@web_bp.route('/races/<int:race_id>')
@web_bp.route('/races/<int:race_id>/<int:stage_id>')
@login_required
def race_detail(race_id, stage_id=None):
    """Race detail page with optional stage-specific view."""
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    if selected_namespace is None or selected_year is None:
        abort(404)
    race = FirstCyclingRace.query.options(
        selectinload(FirstCyclingRace.race_stages),
        selectinload(FirstCyclingRace.race_results),
    ).filter_by(race_id=race_id, season_year=selected_year, gender=selected_gender).first_or_404()

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

    first_cycling_ids = {result.rider_id for result in result_rows if result.rider_id is not None}
    rider_map = {}
    if first_cycling_ids:
        riders = FirstCyclingRider.query.filter(FirstCyclingRider.rider_id.in_(first_cycling_ids)).all()
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

    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, first_cycling_ids)
    field_engine = create_engine(selected_namespace.build_connection_string())
    try:
        with field_engine.connect() as conn:
            _ensure_namespace_database_connection(conn, selected_namespace)
            editable_rider_fields = _editable_stat_fields(_stat_field_settings(conn))
    finally:
        field_engine.dispose()
    rider_link_map = _load_namespace_cyclist_link_map(selected_namespace, first_cycling_ids)
    fc_to_cdb_id = {
        rider_id: (rider_link_map.get(rider_id) or {}).get('cyclist_id')
        for rider_id in first_cycling_ids
    }
    rider_ids = [cdb_id for cdb_id in fc_to_cdb_id.values() if cdb_id is not None]
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
        rider_ids=rider_ids,
    )
    rider_delta_map = _load_namespace_race_result_deltas(
        selected_namespace,
        race_result_map_ids,
        rider_ids,
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
            rider_id = fc_to_cdb_id.get(result.rider_id)
            base_stats = rider_stats_map.get(result.rider_id) or {}
            snapshot_stats = rider_snapshot_map.get(rider_id) or {}
            original_stats = dict(snapshot_stats or base_stats)
            final_stats = dict(original_stats)
            for stat_name, delta_info in (rider_delta_map.get(rider_id) or {}).items():
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
            'locked_stats': {name for name, change in
                             (rider_delta_map.get(fc_to_cdb_id.get(result.rider_id)) or {}).items()
                             if change.get('is_applied_immediately')},
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
        editable_rider_fields=editable_rider_fields,
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


@web_bp.route('/stats-editor/races/<int:race_id>/get-result', methods=['POST'], endpoint='legacy_race_get_result')
@web_bp.route('/races/<int:race_id>/get-result', methods=['POST'])
@login_required
def race_get_result(race_id):
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
            'web.race_detail',
            race_id=race.race_id,
            stage_id=stage_id,
        ))

    return redirect(url_for(
        'web.race_detail',
        race_id=race.race_id,
    ))


@web_bp.route('/stats-editor/races/<int:race_id>/save-stat-deltas', methods=['POST'], endpoint='legacy_race_save_stat_deltas')
@web_bp.route('/races/<int:race_id>/save-stat-deltas', methods=['POST'])
@login_required
def race_save_stat_deltas(race_id):
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
        return redirect(url_for('web.race_detail', **redirect_target))

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.race_detail', **redirect_target))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.race_detail', **redirect_target))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.race_detail', **redirect_target))


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
            return redirect(url_for('web.race_detail', **redirect_target))
        race_result_map_id = race_result_map.race_result_id

        rider_to_cdb_id = {}
        if rider_ids:
            with engine.connect() as conn:
                rider_rows = conn.execute(
                    text(
                        """
                        SELECT id, first_cycling_id
                        FROM rider
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
            _ensure_namespace_database_connection(conn, selected_namespace)
            settings = _stat_field_settings(conn, lock=True)
            _require_editable_stat_changes(settings, parsed_changes)
            immediate_ids = []
            snapshot_rider_ids = {
                rider_to_cdb_id.get(_safe_int(result.rider_id))
                for result in race.race_results
                if result.rider_id is not None
            }
            snapshot_rider_ids.discard(None)
            if not snapshot_rider_ids:
                snapshot_rider_ids = set(rider_to_cdb_id.values())
            _ensure_scope_stat_snapshots(conn, race_result_id=race_result_map_id, rider_ids=snapshot_rider_ids)
            snapshot_rows = conn.execute(
                text(
                    f"""
                    SELECT rider_id, {_snapshot_stat_select_list()}
                    FROM stat_change_snapshot
                    WHERE race_result_id = :race_result_id
                      AND stat_list_id IS NULL
                      AND rider_id = ANY(:rider_ids)
                    """
                ),
                {'race_result_id': race_result_map_id, 'rider_ids': list(rider_to_cdb_id.values())},
            ).mappings().all()
            snapshot_map = {
                row['rider_id']: _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                first_cycling_id = _safe_int(item.get('first_cycling_rider_id'))
                rider_id = rider_to_cdb_id.get(first_cycling_id)
                stat_name = (item.get('stat_name') or '').strip().lower()
                final_value = _safe_int(item.get('final_value'))
                original_value = _safe_int((snapshot_map.get(rider_id) or {}).get(stat_name))
                is_confilict_override = bool(item.get('is_confilict_override'))

                if (
                    first_cycling_id is None
                    or rider_id is None
                    or stat_name not in STAT_DELTA_FIELDS
                    or original_value is None
                    or final_value is None
                    or final_value < STAT_MIN_VALUE
                    or final_value > STAT_MAX_VALUE
                ):
                    skipped_count += 1
                    continue

                _require_unapplied_stat(conn, 'stat_list_id IS NULL AND race_result_id = :race_result_id',
                                        {'race_result_id': race_result_map_id}, rider_id, stat_name)
                delta_value = final_value - original_value
                saved_changes.append({
                    'rider_id': rider_id, 'stat_name': stat_name,
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
                              AND rider_id = :rider_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'race_result_id': race_result_map_id,
                            'rider_id': rider_id,
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
                          AND rider_id = :rider_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'race_result_id': race_result_map_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                    },
                )

                change_id = conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            race_result_id,
                            stat_list_id,
                            rider_id,
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
                            :rider_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            :is_confilict_override,
                            FALSE
                        )
                        RETURNING id
                        """
                    ),
                    {
                        'race_result_id': race_result_map_id,
                        'rider_id': rider_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                        'is_confilict_override': is_confilict_override,
                    },
                ).scalar_one()
                if _stat_applies_immediately(settings, stat_name):
                    immediate_ids.append(change_id)
                saved_count += 1
            _apply_immediate_stats(conn, immediate_ids)
            refresh_pending_flags(conn, rider_to_cdb_id.values())

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

    return redirect(url_for('web.race_detail', **redirect_target))


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


@web_bp.route('/stats-editor/races/<int:race_id>/submit-for-approval', methods=['POST'], endpoint='legacy_race_submit_for_approval')
@web_bp.route('/races/<int:race_id>/submit-for-approval', methods=['POST'])
@login_required
def race_submit_for_approval(race_id):
    """Request review of saved overall and stage edits in the selected namespace."""
    if not _can_update_stats():
        abort(403)
    if 'stage_id' in request.form:
        abort(400)

    race = FirstCyclingRace.query.filter_by(race_id=race_id).first_or_404()
    target = url_for('web.race_detail', race_id=race_id)
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
            url_for('web.race_detail', race_id=race_id, _external=True),
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


@web_bp.route('/stats-editor/races/<int:race_id>/approve-stat-deltas', methods=['POST'], endpoint='legacy_race_approve_stat_deltas')
@web_bp.route('/races/<int:race_id>/approve-stat-deltas', methods=['POST'])
@login_required
def race_approve_stat_deltas(race_id):
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
        return redirect(url_for('web.race_detail', **redirect_target))

    result_state = _build_race_result_state(race)
    if result_state['is_stage_race'] and stage_scope_id is not None:
        flash('Stage races can only be approved from the overall race result.', 'warning')
        return redirect(url_for('web.race_detail', **redirect_target))

    if result_state['is_stage_race'] and (not result_state['is_completed'] or not result_state['has_overall_result']):
        flash('Stage races can only be approved once the race is completed and the overall result is saved.', 'warning')
        return redirect(url_for('web.race_detail', **redirect_target))

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
                return redirect(url_for('web.race_detail', **redirect_target))

            if not _race_submission_matches(
                selected_namespace, race.race_id,
                _race_pending_submission_fingerprint(conn, race_result_ids),
            ):
                flash('Submit the current saved changes for approval before approving this race.', 'warning')
                return redirect(url_for('web.race_detail', **redirect_target))

            conflicts = _load_scope_stat_change_conflicts(conn, approval_where_clause, approval_params)
            if conflicts:
                flash(f'Resolve {len(conflicts)} stat conflict(s) before approving this race.', 'warning')
                return redirect(url_for('web.race_detail', **redirect_target))

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
            refresh_pending_flags(conn, conn.execute(text(
                f"SELECT DISTINCT rider_id FROM stat_changes WHERE {approval_where_clause.replace('stat_changes.', '')}"
            ), approval_params).scalars().all())
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

    return redirect(url_for('web.race_detail', **redirect_target))


# ==================== Admin Panel Routes ====================

@web_bp.route('/settings')
@login_required
def settings():
    """Central directory of administration and configuration options."""
    return render_template(
        'settings.html',
        can_read_identities=current_user.has_permission('identities', 'read'),
        can_manage_users=_can_access_user_management(),
        can_manage_namespaces=_can_access_namespace_management(),
        can_read_audit_logs=current_user.has_permission('audit_logs', 'read'),
        can_access_external_integrations=(
            _can_read_external_call_groups() or _can_access_firstcycling_integrations()
            or _can_run_firstcycling_integrations()
            or current_user.has_permission('integrations-external-calls', 'download')
        ),
        can_access_discord_integrations=_can_access_discord_integrations(),
    )


def _audit_resource_links(entries):
    """Format historical resource references without relying on the selected namespace."""
    namespaces = {ns.id: ns for ns in Namespace.query.all()}
    resources = {}
    name_cache = {}
    for entry in entries:
        kind = entry.resource_type or ''
        namespace = None
        parts = kind.split(':')
        if len(parts) == 3 and parts[0] == 'namespace':
            namespace = namespaces.get(_safe_int(parts[1]))
            kind = parts[2]
        links = []
        if len(parts) == 3 and parts[0] == 'namespace':
            links.append({
                'label': f'ns:{namespace.slug if namespace else parts[1]}',
                'url': url_for('web.admin_namespace_detail', namespace_id=namespace.id) if namespace else None,
            })
        name = entry.resource_name
        resource_id = entry.resource_id
        key = (namespace.id if namespace else None, kind, resource_id)
        if not name and resource_id is not None:
            if key not in name_cache:
                resolved_name = None
                if kind == 'race':
                    race = FirstCyclingRace.query.filter_by(race_id=resource_id).first()
                    resolved_name = race.name if race else None
                elif kind == 'user':
                    user = db.session.get(User, resource_id)
                    resolved_name = user.username if user else None
                elif kind == 'namespace':
                    ns = namespaces.get(resource_id)
                    resolved_name = ns.slug if ns else None
                elif namespace and not namespace.is_deleted and kind in ('release', 'stat_list', 'individual'):
                    try:
                        if kind == 'individual':
                            rider = _load_namespace_roster_rider(namespace, resource_id)
                            resolved_name = rider.name if rider else None
                        elif kind == 'release':
                            release = _load_namespace_release(namespace, resource_id)
                            resolved_name = (release.release_name or release.version) if release else None
                        else:
                            resolved_name = next((row['list_name'] for row in _load_namespace_stat_lists(namespace)
                                                  if row['id'] == resource_id), None)
                    except Exception:
                        logger.exception('Failed to resolve audit resource name for %s', key)
                name_cache[key] = resolved_name
            name = name_cache[key]
        endpoint, values = None, {}
        if resource_id is not None:
            if namespace and not namespace.is_deleted:
                if kind == 'race':
                    endpoint, values = 'web.race_detail', {'race_id': resource_id}
                elif kind == 'release':
                    endpoint, values = 'web.releases', {'tab': 'overview', 'release_id': resource_id}
                elif kind == 'stat_list':
                    endpoint, values = 'web.stats_editor_list_detail', {'list_id': resource_id}
                elif kind == 'individual':
                    endpoint, values = 'web.rider_detail', {'rider_id': resource_id}
                if endpoint:
                    values['namespace_slug'] = namespace.slug
            elif kind == 'user':
                endpoint, values = 'web.admin_user_detail', {'user_id': resource_id}
            elif kind == 'namespace' and resource_id in namespaces:
                endpoint, values = 'web.admin_namespace_detail', {'namespace_id': resource_id}
        label = f'{"ns" if kind == "namespace" else kind}:{name or resource_id}' if kind and (name or resource_id is not None) else (name or kind or '—')
        links.append({'label': label, 'url': url_for(endpoint, **values) if endpoint else None})
        resources[entry.id] = links
    return resources


@web_bp.route('/admin/audit-log')
@login_required
def admin_audit_log():
    """Read shared audit history with action and recorded-username filters."""
    if not current_user.has_permission('audit_logs', 'read'):
        abort(403)

    for namespace in Namespace.query.filter_by(is_deleted=False).all():
        try:
            field_configuration.forward_audits(namespace)
        except Exception:
            logger.exception('Could not project field configuration audit for namespace %s', namespace.slug)

    selected_action = request.args.get('action', '').strip()
    selected_user = request.args.get('user', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    query = AuditLog.query
    if selected_action:
        query = query.filter(AuditLog.action == selected_action)
    if selected_user:
        query = query.filter(AuditLog.username == selected_user)
    entries = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=50, error_out=False,
    )
    return render_template(
        'admin/audit_log.html', entries=entries,
        resource_links=_audit_resource_links(entries.items),
        actions=[row[0] for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)],
        usernames=[row[0] for row in db.session.query(AuditLog.username).distinct().order_by(AuditLog.username)],
        selected_action=selected_action, selected_user=selected_user,
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
        field_slugs=field_configuration.field_slugs(),
        field_grants={(p.resource.name, p.action.name) for p in role.permissions},
        active_section='users',
    )


@web_bp.route('/admin/roles/<int:role_id>/field-permissions', methods=['POST'])
@login_required
def admin_update_field_permission(role_id):
    if not current_user.has_permission('roles', 'update'):
        abort(403)
    slug = request.form.get('slug')
    action = request.form.get('action')
    decision = request.form.get('decision')
    if slug not in field_configuration.field_slugs() or action not in ('update', 'approve') or decision not in ('grant', 'revoke'):
        abort(400)
    role = Role.query.filter_by(id=role_id).with_for_update().first_or_404()
    resource = Resource.query.filter_by(name=slug).first_or_404()
    permission_action = Action.query.filter_by(name=action).first_or_404()
    permission = RolePermission.query.filter_by(role_id=role.id, resource_id=resource.id, action_id=permission_action.id).first()
    grant = decision == 'grant'
    if grant != (permission is not None):
        if grant:
            db.session.add(RolePermission(role_id=role.id, resource_id=resource.id, action_id=permission_action.id))
        else:
            db.session.delete(permission)
        AuditLog.log_action(
            AuditAction.FIELD_PERMISSION_UPDATED, current_user,
            resource_type='role', resource_id=role.id, resource_name=role.display_name,
            details={'permission': f'{slug}:{action}', 'granted': grant}, ip_address=request.remote_addr,
        )
        db.session.commit()
    return redirect(url_for('web.admin_role_detail', role_id=role.id) + '#field-permissions')


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
    """Show external call history, manual sync controls, or scheduler settings."""
    can_view_groups = _can_read_external_call_groups() or _can_access_firstcycling_integrations()
    can_run_fc = _can_run_firstcycling_integrations()
    can_view_fc = _can_access_firstcycling_integrations() or can_run_fc
    can_run_pcs = current_user.has_permission('integrations-external-calls', 'download')
    can_view_scheduler = _can_read_external_call_groups()
    can_view_manual = can_view_fc or can_run_pcs
    if not can_view_groups and not can_view_manual:
        abort(403)

    selected_namespace = _get_selected_namespace()
    tab = request.args.get('tab') or ('groups' if can_view_groups else 'manual')
    if tab not in ('groups', 'manual', 'scheduler'):
        abort(404)
    context = {
        'tab': tab,
        'can_view_groups': can_view_groups,
        'can_view_manual': can_view_manual,
        'can_view_scheduler': can_view_scheduler,
        'sync_type_display': _external_sync_type_display,
        'sync_views': EXTERNAL_SYNC_TYPE_LABELS,
    }
    if tab == 'manual':
        if not can_view_manual:
            abort(403)
        manual_types = {key: f'FC {EXTERNAL_SYNC_TYPE_LABELS[key]}' for key in EXTERNAL_SYNC_RUN_TYPES} if can_view_fc else {}
        if can_run_pcs:
            manual_types[PCS_RIDER_SYNC] = 'PCS Rider'
        configured_seasons = _load_config_seasons()
        sync_view = request.args.get('sync_view')
        if sync_view not in manual_types:
            sync_view = next(iter(manual_types))
        context.update(
            manual_types=manual_types,
            can_run_firstcycling_integrations=can_run_fc,
            configured_seasons=configured_seasons,
            selected_season=_resolve_external_sync_season(
                configured_seasons, request.args.get('year'), request.args.get('gender'),
            ),
            active_sync_view=sync_view,
            selected_start_date=request.args.get('start_date', ''),
            selected_end_date=request.args.get('end_date', ''),
            selected_limit=request.args.get('limit', ''),
            rider_slug=request.args.get('rider_slug', ''),
        )
    elif tab == 'scheduler':
        if not can_view_scheduler:
            abort(403)
        namespaces = _get_accessible_namespaces()
        jobs = _visible_scheduled_jobs(namespaces)
        job_id = request.args.get('job_id', type=int)
        editing = jobs.filter_by(id=job_id).first_or_404() if job_id else None
        job_type = editing.job_type if editing else request.args.get('job_type', 'race_results')
        if job_type not in SCHEDULED_JOB_TYPES:
            abort(400)
        namespace_id = editing.namespace_id if editing else request.args.get('namespace_id', type=int)
        namespace = next((ns for ns in namespaces if ns.id == namespace_id), None)
        params = editing.params if editing else {}
        search = request.args.get('rider_search', '').strip()[:120]
        riders = []
        if job_type == ACTIVITY_CHECK and namespace and (editing or request.args.get('new')):
            try:
                riders = scheduled_rider_choices(namespace, search, params.get('rider_id'))
            except (ValueError, SQLAlchemyError):
                logger.warning('Could not load linked riders for namespace %s', namespace.id)
                flash('Could not load riders for this namespace.', 'warning')
        seasons = _load_config_seasons()
        editable_job_types = {kind for kind in SCHEDULED_JOB_TYPES if _can_configure_scheduled_job(kind)}
        context.update(
            jobs=jobs.order_by(ScheduledJob.name, ScheduledJob.id).all(),
            job_types=SCHEDULED_JOB_TYPES, editing=editing, job_type=job_type,
            show_job_form=bool(editing or request.args.get('new')), job_params=params,
            job_namespace=namespace, namespaces=namespaces, rider_choices=riders, rider_search=search,
            configured_seasons=seasons, selected_season=_resolve_external_sync_season(
                seasons, params.get('year'), params.get('gender'),
            ),
            scheduler_timezone=current_app.config['WORKER_TIMEZONE'],
            can_update_schedule=job_type in editable_job_types,
            editable_job_types=editable_job_types,
        )
    else:
        if not can_view_groups:
            abort(403)
        groups = _visible_external_call_groups(selected_namespace)
        filters = {
            name: request.args.get(name, '').strip()
            for name in ('provider', 'status', 'origin', 'priority', 'sync_view', 'year', 'gender')
        }
        for name, column in (
            ('status', ExternalCallTaskGroup.status),
            ('origin', ExternalCallTaskGroup.origin),
            ('priority', ExternalCallTaskGroup.priority),
            ('gender', ExternalCallTaskGroup.gender),
        ):
            if filters[name]:
                groups = groups.filter(column == filters[name])
        if filters['sync_view'] and filters['sync_view'] != 'all':
            groups = groups.filter_by(sync_type=filters['sync_view'])
        if filters['year']:
            year = request.args.get('year', type=int)
            if year is None or not 1 <= year <= 9999:
                abort(400)
            groups = groups.filter_by(season_year=year)
        if filters['provider']:
            provider = filters['provider']
            groups = groups.filter(or_(
                ExternalCallTaskGroup.provider == provider,
                ExternalCallTask.query.filter(
                    ExternalCallTask.task_group_id == ExternalCallTaskGroup.id,
                    ExternalCallTask.provider == provider,
                ).exists(),
            ))

        page = groups.order_by(
            ExternalCallTaskGroup.timestamp.desc(), ExternalCallTaskGroup.id.desc(),
        ).paginate(per_page=50)
        providers_by_group = {group.id: set() for group in page.items}
        if providers_by_group:
            for group_id, provider in db.session.query(
                ExternalCallTask.task_group_id, ExternalCallTask.provider,
            ).filter(ExternalCallTask.task_group_id.in_(providers_by_group)).distinct():
                providers_by_group[group_id].add(provider)
        for group in page.items:
            if group.provider:
                providers_by_group[group.id].add(group.provider)
        context.update(
            page=page,
            filters=filters,
            providers_by_group=providers_by_group,
            selected_namespace=selected_namespace,
        )

    response = make_response(render_template('admin/external_data_integrations.html', **context))
    response.cache_control.no_store = True
    return response


def _visible_scheduled_jobs(namespaces):
    return ScheduledJob.query.filter(or_(
        ScheduledJob.namespace_id.is_(None), ScheduledJob.namespace_id.in_([ns.id for ns in namespaces]),
    ))


def _can_configure_scheduled_job(job_type):
    return (
        current_user.has_permission('integrations-external-calls', 'update')
        and _can_run_firstcycling_integrations()
        and (job_type not in RIDER_CHECK_JOBS
             or current_user.has_permission('integrations-external-calls', 'download'))
    )


@web_bp.route('/admin/integrations/scheduler', methods=['POST'])
@login_required
def save_external_sync_schedule():
    if not _can_read_external_call_groups():
        abort(403)
    namespaces = _get_accessible_namespaces()
    _get_selected_namespace(namespaces)
    action = request.form.get('action', 'save')
    job_type = request.form.get('job_type')
    if action not in ('save', 'run_now', 'disable') or job_type not in SCHEDULED_JOB_TYPES:
        abort(400)
    if not _can_configure_scheduled_job(job_type):
        abort(403)

    job_id = request.form.get('job_id', type=int)
    job = _visible_scheduled_jobs(namespaces).filter_by(id=job_id).with_for_update().first_or_404() if job_id else None
    namespace_id = request.form.get('namespace_id', type=int) if job_type in RIDER_CHECK_JOBS else None
    if job and (job.job_type != job_type or job.namespace_id != namespace_id):
        abort(400, description='The job type and namespace are fixed after creation.')
    return_url = url_for('web.admin_integrations_index', tab='scheduler')
    if action == 'disable':
        if job is None:
            abort(400)
        job.enabled = False
        db.session.commit()
        flash('Scheduled job disabled.', 'success')
        return redirect(return_url)

    name = request.form.get('name', '').strip()
    if not name or len(name) > 120:
        abort(400, description='Enter a job name of up to 120 characters.')
    enabled = request.form.get('enabled') == '1'
    try:
        run_at = datetime.strptime(request.form.get('run_at', ''), '%H:%M').time()
    except ValueError:
        abort(400, description='Enter a valid daily run time.')

    if job_type == 'race_results':
        season = _find_external_sync_season(_load_config_seasons(), request.form.get('year'), request.form.get('gender'))
        start_days_ago = request.form.get('start_days_ago', type=int)
        end_days_ago = request.form.get('end_days_ago', type=int)
        if season is None:
            abort(400, description='Select a configured FirstCycling season and gender.')
        if start_days_ago is None or end_days_ago is None or not 0 <= end_days_ago <= start_days_ago <= 30:
            abort(400, description='Choose a relative start date on or before the end date.')
        params = dict(year=season['year'], gender=season['gender'],
                      start_days_ago=start_days_ago, end_days_ago=end_days_ago)
    else:
        namespace = next((ns for ns in namespaces if ns.id == namespace_id), None)
        if namespace is None:
            abort(400, description='Select a namespace.')
        if job_type == IDENTITY_CHECK:
            riders_per_run = request.form.get('riders_per_run', type=int)
            if riders_per_run is None or not 1 <= riders_per_run <= 1000:
                abort(400, description='Choose between 1 and 1000 riders per run.')
            params = {'riders_per_run': riders_per_run}
        else:
            rider_id = request.form.get('rider_id', type=int)
            if rider_id is None or not 1 <= rider_id <= 2147483647:
                abort(400, description='Select a linked rider.')
            try:
                rider = scheduled_rider(namespace, rider_id)
            except (ValueError, SQLAlchemyError):
                abort(400, description='Could not load the selected namespace rider.')
            if rider is None:
                abort(400, description='Select a rider with valid PCS and FirstCycling links in this namespace.')
            params = {'rider_id': rider_id}
        fields = ('min_days_since_check', 'recent_activity_days') if job_type == ACTIVITY_CHECK else ('min_days_since_check',)
        for field in fields:
            days = request.form.get(field, 0, type=int)
            if not 0 <= days <= 365:
                abort(400, description='Enter a lookback from 0 to 365 days.')
            params[field] = days

    if action == 'run_now':
        # Release the edit lock before notifying the worker. Run Now never saves the form.
        db.session.rollback()
        if job_type == 'race_results':
            today = datetime.now(ZoneInfo(current_app.config['WORKER_TIMEZONE'])).date()
            _queue_manual_fc_sync(
                'fc_race_results', params,
                today - timedelta(days=params['start_days_ago']),
                today - timedelta(days=params['end_days_ago']), scheduled_job_id=job_id,
            )
        else:
            try:
                run_rider_check_now.delay(job_type, namespace_id, current_user.id, params, job_id=job_id)
            except Exception:
                logger.exception('Could not queue one-off rider check')
                flash('Could not queue the rider check.', 'error')
            else:
                flash('Rider check requested. Check History for the outcome.', 'info')
        return redirect(return_url)

    if job is None:
        job = ScheduledJob(job_type=job_type, namespace_id=namespace_id)
        db.session.add(job)
    job.name, job.params, job.run_at, job.enabled = name, params, run_at, enabled
    db.session.commit()
    flash('Scheduled job saved.', 'success')
    return redirect(return_url)


def _visible_external_call_groups(namespace):
    global_groups = ExternalCallTaskGroup.namespace_id.is_(None)
    if _can_read_external_call_groups():
        if namespace is None:
            return ExternalCallTaskGroup.query.filter(global_groups)
        return ExternalCallTaskGroup.query.filter(or_(
            global_groups, ExternalCallTaskGroup.namespace_id == namespace.id,
        ))
    return ExternalCallTaskGroup.query.filter(global_groups, ExternalCallTaskGroup.provider == 'firstcycling')


@web_bp.route('/admin/integrations/task-groups/<int:group_id>')
@login_required
def external_call_group_detail(group_id):
    if not (_can_read_external_call_groups() or _can_access_firstcycling_integrations()):
        abort(403)

    namespace = _get_selected_namespace()
    group = _visible_external_call_groups(namespace).filter_by(id=group_id).first_or_404()
    tasks = ExternalCallTask.query.filter_by(task_group_id=group.id).order_by(ExternalCallTask.id).all()
    try:
        details = json.loads(group.details_json or '{}')
    except ValueError:
        details = {}

    response = make_response(render_template(
        'admin/external_call_group.html',
        group=group,
        tasks=tasks,
        sync_type_label=_external_sync_type_display(group.sync_type),
        details=details if isinstance(details, dict) else {},
    ))
    response.cache_control.no_store = True
    return response


@web_bp.route('/admin/integrations/task-groups/<int:group_id>/tasks/<int:task_id>')
@login_required
def external_call_task_detail(group_id, task_id):
    if not (_can_read_external_call_groups() or _can_access_firstcycling_integrations()):
        abort(403)
    namespace = _get_selected_namespace()
    group = _visible_external_call_groups(namespace).filter_by(id=group_id).first_or_404()
    task = ExternalCallTask.query.filter_by(id=task_id, task_group_id=group.id).first_or_404()

    related_record_url = related_record_name = None
    if (task.related_to_table == 'rider' and task.related_to_id is not None
            and namespace is not None and group.namespace_id == namespace.id
            and _can_access_panel('team_manager')):
        related_record_url = url_for('web.rider_detail', namespace_slug=namespace.slug, rider_id=task.related_to_id)
        rider = _load_namespace_roster_rider(namespace, task.related_to_id)
        if rider is not None:
            first_name = (rider.first_name or '').strip()
            last_name = (rider.last_name or '').strip()
            related_record_name = f'{first_name} {last_name}'.strip() or rider.name

    def formatted_json(raw):
        if not raw:
            return None
        try:
            return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return raw

    response = make_response(render_template(
        'admin/external_call_task.html', group=group, task=task, related_record_url=related_record_url,
        related_record_name=related_record_name,
        request_params=formatted_json(task.request_params_json),
        result=formatted_json(task.result_json),
    ))
    response.cache_control.no_store = True
    return response


@web_bp.route('/admin/integrations/<provider>', methods=['GET', 'POST'])
@login_required
def admin_integrations(provider='firstcycling'):
    """Handle Discord settings and redirect legacy FirstCycling links."""
    requested_provider = (request.args.get('provider') or '').strip().lower()
    if requested_provider:
        provider = requested_provider

    provider = (provider or 'firstcycling').strip().lower()
    if provider not in ('firstcycling', 'discord'):
        abort(404)
    if provider == 'firstcycling':
        if not (_can_read_external_call_groups() or _can_access_firstcycling_integrations()
                or _can_run_firstcycling_integrations()):
            abort(403)
        _get_selected_namespace()
        filters = {key: request.args.get(key) for key in ('sync_view', 'year', 'gender', 'start_date', 'end_date')}
        if _can_read_external_call_groups() or _can_access_firstcycling_integrations():
            return redirect(url_for('web.admin_integrations_index', provider=provider, **filters))
        return redirect(url_for('web.admin_integrations_index', tab='manual', **filters))
    if not _can_access_discord_integrations():
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    discord_settings = (
        DiscordSettings.query.filter_by(namespace_id=selected_namespace.id).order_by(DiscordSettings.id.asc()).all()
        if selected_namespace is not None
        else []
    )

    if request.method == 'POST':
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

    response = make_response(render_template(
        'admin/integrations.html',
        selected_namespace=selected_namespace,
        discord_settings=discord_settings,
        can_update_discord_integrations=_can_update_discord_integrations(),
        discord_workflow_choices=DISCORD_WORKFLOW_CHOICES,
        discord_bot_configured=bool(current_app.config.get('DISCORD_BOT_TOKEN')),
    ))
    response.cache_control.no_store = True
    return response


@web_bp.route('/admin/external-sync')
@web_bp.route('/admin/external-sync/<provider>')
@login_required
def admin_external_sync_redirect(provider='firstcycling'):
    """Redirect legacy external sync URLs to integrations URLs."""
    _get_selected_namespace()
    query_params = request.args.to_dict()
    query_params['provider'] = provider
    return redirect(url_for('web.admin_integrations', **query_params))


@web_bp.route('/admin/integrations/run', methods=['POST'])
@web_bp.route('/admin/external-sync/run', methods=['POST'])
@login_required
def admin_integrations_run():
    """Queue a supported integration from its manual form."""
    provider = (request.form.get('provider') or 'firstcycling').strip().lower()
    sync_type = request.form.get('sync_type', '').strip()
    if provider == 'pcs' and sync_type == PCS_RIDER_SYNC:
        if not current_user.has_permission('integrations-external-calls', 'download'):
            abort(403)
        namespace = _get_selected_namespace()
        slug = request.form.get('rider_slug', '').strip()
        return_url = url_for('web.admin_integrations_index', tab='manual', sync_view=PCS_RIDER_SYNC)
        if not _valid_pcs_rider_slug(slug):
            flash('Enter the PCS rider slug, such as jane-doe.', 'error')
            return redirect(return_url)
        create_manual_external_group(
            [{'provider': 'pcs', 'endpoint': PROFILE_ENDPOINT, 'params': {'rider_slug': slug}}],
            namespace_id=namespace.id if namespace else None, executed_by_id=current_user.id,
            message=f'PCS rider profile for {slug}'[:255], provider='pcs',
        )
        flash('PCS rider check queued. Progress will appear in History.', 'info')
        return redirect(return_url)
    if provider != 'firstcycling':
        abort(400)
    if not _can_run_firstcycling_integrations():
        abort(403)

    _get_selected_namespace()
    if sync_type not in EXTERNAL_SYNC_RUN_TYPES:
        abort(400)
    fields = {
        name: (request.form.get(name) or '').strip()
        for name in ('year', 'gender', 'start_date', 'end_date', 'limit')
    }
    return_url = url_for('web.admin_integrations_index', tab='manual', sync_view=sync_type, **fields)
    season = _find_external_sync_season(_load_config_seasons(), fields['year'], fields['gender'])
    if season is None:
        flash('Select a configured FirstCycling season and gender.', 'error')
        return redirect(return_url)

    start_date = end_date = None
    limit = None
    if sync_type == 'fc_race_results':
        try:
            start_date = date.fromisoformat(fields['start_date'])
            end_date = date.fromisoformat(fields['end_date'])
            if start_date > end_date:
                raise ValueError
        except ValueError:
            flash('Enter valid race result dates with the start on or before the end.', 'error')
            return redirect(return_url)
    elif sync_type in ('fc_teams', 'fc_riders') and fields['limit']:
        limit = request.form.get('limit', type=int)
        if limit is None or not 1 <= limit <= 2147483647:
            flash('Enter a positive record limit or leave it blank.', 'error')
            return redirect(return_url)

    _queue_manual_fc_sync(sync_type, season, start_date, end_date, limit)
    return redirect(return_url)


def _queue_manual_fc_sync(sync_type, season, start_date=None, end_date=None, limit=None, scheduled_job_id=None):
    api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
    label = _external_sync_type_display(sync_type)
    message = f'Queued {label} for {season["year"]} [{season["gender"]}]'
    if start_date is not None:
        message += f' ({start_date} to {end_date})'
    if limit is not None:
        message += f' (limit {limit})'
    if not api_path:
        message = 'FIRST_CYCLING_API_PATH is not configured'
    group = _record_external_sync_log(
        provider='firstcycling', sync_type=sync_type, status='queued' if api_path else 'failed',
        executed_by_id=current_user.id, season_year=season['year'], gender=season['gender'],
        message=message, scheduled_job_id=scheduled_job_id,
    )
    db.session.commit()
    if not api_path:
        flash(message, 'error')
        return

    group_id = group.id
    try:
        run_external_sync_task.delay(
            log_id=group_id, sync_type=sync_type, season_year=season['year'], gender=season['gender'],
            api_path=api_path, start_date=start_date.isoformat() if start_date else None,
            end_date=end_date.isoformat() if end_date else None, limit=limit,
        )
    except Exception:
        logger.exception('Could not queue external sync group %s', group_id)
        failed_group = db.session.get(ExternalCallTaskGroup, group_id, with_for_update=True)
        if failed_group.status == 'queued':
            failed_group.status = 'failed'
            failed_group.message = 'Could not send the sync to the worker.'
            failed_group.failed_count = 1
        db.session.commit()
        flash(f'Could not confirm that {label} was queued. Check History for its status.', 'error')
    else:
        flash(f'{label} queued. Progress will appear in History.', 'info')


@web_bp.route('/admin/namespaces/<int:namespace_id>')
@login_required
@admin_required
def admin_namespace_detail(namespace_id):
    """Admin panel - View and manage namespace details"""
    namespace = Namespace.query.get_or_404(namespace_id)
    metadata_form = NamespaceMetadataForm(obj=namespace)
    metadata_form.pcm_version.choices = current_app.config['PCM_VERSIONS']
    
    return render_template('admin/namespace_detail.html',
                         namespace=namespace,
                         metadata_form=metadata_form,
                         active_section='namespaces')


@web_bp.route('/admin/namespaces/<int:namespace_id>/fields', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_namespace_fields(namespace_id):
    namespace = Namespace.query.get_or_404(namespace_id)
    can_update = _can_update_namespaces()
    if request.method == 'POST' and not can_update:
        abort(403)
    object_name = request.values.get('object', 'rider')
    if object_name not in field_configuration.OBJECTS:
        abort(404)
    engine = create_engine(namespace.build_connection_string())
    try:
        if namespace.database_name == (db.engines.get('main') or db.engine).url.database:
            abort(400)
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            fields = field_configuration.stored_fields(conn, object_name)
            if request.method == 'POST':
                name = request.form.get('name')
                table = field_configuration.TABLE
                if name == 'id':
                    abort(400, description='The CDB identity mapping is fixed.')
                if (object_name, name) not in fields or name not in field_configuration.configurable_columns(object_name):
                    abort(404)
                key = (table.c.object == object_name, table.c.name == name)
                before = conn.execute(table.select().where(*key).with_for_update()).mappings().one()
                values = field_configuration.form_values(object_name, name, request.form)
                changed = {key: value for key, value in values.items() if before[key] != value}
                if changed:
                    conn.execute(table.update().where(*key).values(**changed))
                    conn.execute(FieldConfigurationAudit.__table__.insert().values(
                        actor_id=current_user.id, username=current_user.username,
                        details={'object': object_name, 'name': name, 'slug': before['slug'],
                                 'before': {key: before[key] for key in changed}, 'after': changed},
                        ip_address=request.remote_addr,
                    ))
    except ValueError as error:
        flash(str(error), 'error')
    finally:
        engine.dispose()
    try:
        field_configuration.forward_audits(namespace)
    except Exception:
        logger.exception('Could not project field configuration audit for namespace %s', namespace.slug)
        flash('Field configuration audit history is retained in the namespace. Delivery to the Audit Log will be retried.', 'warning')
    if request.method == 'POST':
        return redirect(url_for('web.admin_namespace_fields', namespace_id=namespace.id, object=object_name))
    return render_template(
        'admin/namespace_fields.html', namespace=namespace, active_section='namespaces',
        object_name=object_name, objects=field_configuration.OBJECTS, can_update=can_update,
        fields=sorted(fields.values(), key=lambda field: field['name']),
        import_policies=field_configuration.IMPORT_POLICIES, null_policies=field_configuration.NULL_POLICIES,
        can_edit_column=field_configuration.can_edit_column,
    )


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
    clone_sources = []
    if (current_user.has_permission('namespaces', 'read')
            and current_user.has_permission('releases', 'read')
            and current_user.has_permission('releases', 'download')):
        clone_sources = [source for source in Namespace.query.filter_by(
            is_deleted=False, is_template=False).order_by(Namespace.name).all()
            if current_user.has_namespace_access(source.id)]
    form.source_namespace.choices = [('', '-- Select Namespace --')] + [
        (str(source.id), source.name) for source in clone_sources]

    if request.method == 'POST' and request.form.get('creation_method') == 'clone':
        if not (current_user.has_permission('namespaces', 'read')
                and current_user.has_permission('releases', 'read')
                and current_user.has_permission('releases', 'download')):
            abort(403)
        if request.form.get('source_namespace') not in {str(source.id) for source in clone_sources}:
            abort(403)
    
    if request.method == 'POST':
        form.slug.data = _build_namespace_slug(form.name.data, form.gender.data)

    if form.validate_on_submit():
        normalized_slug = form.slug.data
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

        if form.creation_method.data == 'clone':
            from webapp.namespace.clone import CATEGORIES, clone_namespace, load_clone_plan

            source = next(item for item in clone_sources if str(item.id) == form.source_namespace.data)
            if form.cdb_file.data:
                flash('Choose either a source namespace or a CDB upload.', 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            if form.pcm_version.data != source.pcm_version:
                flash('Select the source namespace’s PCM version when cloning.', 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            categories = [kind for kind in CATEGORIES if getattr(form, f'copy_{kind}').data]
            values = {key: getattr(form, key).data for key in
                      ('name', 'slug', 'description', 'pcm_version', 'database_year', 'gender', 'status')}
            if request.form.get('confirm_clone') != 'yes':
                try:
                    plan = load_clone_plan(source, categories)
                except Exception:
                    logger.exception('Could not preview namespace clone from %s', source.slug)
                    flash('Could not preview the clone. Verify that the source has a complete published release and compatible schema.', 'error')
                    return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
                session['namespace_clone_review'] = dict(
                    user_id=current_user.id, source_id=source.id, values=values, categories=categories,
                    fingerprint=plan['fingerprint'],
                    expires=(datetime.utcnow() + timedelta(hours=1)).timestamp(),
                )
                return render_template('admin/namespace_form.html', form=form, clone_plan=plan,
                                       active_section='namespaces')
            review = session.get('namespace_clone_review', {})
            if (review.get('user_id') != current_user.id or review.get('source_id') != source.id
                    or review.get('values') != values or review.get('categories') != categories
                    or review.get('expires', 0) < datetime.utcnow().timestamp()):
                flash('The clone preview expired or settings changed. Preview again.', 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            namespace = Namespace(**values, database_name=database_name, created_by_id=current_user.id)
            try:
                with clone_namespace(source, namespace, categories, review['fingerprint']) as details:
                    db.session.add(namespace)
                    db.session.flush()
                    AuditLog.log_action(
                        action=AuditAction.NAMESPACE_CREATED, user=current_user,
                        resource_type='namespace', resource_id=namespace.id, resource_name=namespace.name,
                        details={'method': 'clone', **details}, ip_address=request.remote_addr,
                    )
                    db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception('Namespace clone failed for %s', normalized_slug)
                session.pop('namespace_clone_review', None)
                flash('Cloning failed. Preview again; the source may have changed or the destination may already exist.', 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            session.pop('namespace_clone_review', None)
            flash('Namespace cloned from the latest published release without pending approvals.', 'success')
            return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))
        
        # Handle CDB file upload
        sqlite_file_path = None
        creation_values = {key: getattr(form, key).data for key in
                           ('name', 'slug', 'description', 'pcm_version', 'database_year', 'gender', 'status')}
        confirming_import = request.form.get('confirm_import') == 'yes' and not form.cdb_file.data
        initial_fingerprint = None
        if request.form.get('analyze_import') == 'yes' and not form.cdb_file.data:
            flash('Select a CDB file to analyze again.', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
        if confirming_import:
            from webapp.integrations.cdb import cdb_import
            from webapp.config import get_cdb_import_path
            review = session.get('namespace_creation_review', {})
            if (review.get('user_id') != current_user.id
                    or review.get('values') != creation_values
                    or review.get('expires', 0) < datetime.utcnow().timestamp()):
                flash('The import review expired or namespace settings changed. Select the file and analyze again.', 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            try:
                analysis = cdb_import.analyze_initial(review['sqlite_path'], form.pcm_version.data)
                if analysis['errors'] or analysis['fingerprint'] != review['fingerprint']:
                    raise ValueError('The import plan changed. Select the file and analyze again.')
            except (ValueError, OSError, sqlite3.Error) as error:
                flash(str(error), 'error')
                return render_template('admin/namespace_form.html', form=form, active_section='namespaces'), 400
            sqlite_file_path = f"cdb/import/{os.path.basename(review['sqlite_path'])}"
            initial_fingerprint = review['fingerprint']
        elif form.cdb_file.data:
            from webapp.integrations.cdb import cdb_import
            from werkzeug.utils import secure_filename
            from webapp.config import ensure_namespace_directories, get_cdb_import_path
            from webapp.integrations.cdb.converter import (
                convert_cdb_to_sqlite,
                validate_sqlite_cdb,
            )
            
            # Create namespace directories
            ensure_namespace_directories(normalized_slug)
            
            # Get the upload file
            file = form.cdb_file.data
            filename = secure_filename(file.filename)
            
            # Generate a unique filename with timestamp if needed
            base_name, ext = os.path.splitext(filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
            unique_filename = f"{base_name}_{timestamp}{ext}"
            
            # Preserve the uploaded file. Packed .cdb files are converted to a
            # precise-types SQLite copy before any namespace database is made.
            import_path = get_cdb_import_path(normalized_slug)
            source_path = os.path.join(import_path, unique_filename)
            sqlite_path = source_path
            paths_to_clean = [source_path]

            try:
                file.save(source_path)
                if ext.lower() == '.cdb':
                    sqlite_filename = f"{base_name}_{timestamp}.sqlite"
                    sqlite_path = os.path.join(import_path, sqlite_filename)
                    paths_to_clean.append(sqlite_path)
                    convert_cdb_to_sqlite(source_path, sqlite_path)
                else:
                    validate_sqlite_cdb(sqlite_path)
                analysis = cdb_import.analyze_initial(sqlite_path, form.pcm_version.data)
            except Exception as exc:
                for path in paths_to_clean:
                    if os.path.exists(path):
                        os.remove(path)
                flash(f'Could not prepare the CDB file: {exc}', 'error')
                return render_template(
                    'admin/namespace_form.html',
                    form=form,
                    active_section='namespaces',
                ), 400

            session['namespace_creation_review'] = dict(
                user_id=current_user.id, values=creation_values, sqlite_path=sqlite_path,
                fingerprint=analysis['fingerprint'],
                expires=(datetime.utcnow() + timedelta(hours=1)).timestamp(),
            )
            return render_template('admin/namespace_form.html', form=form,
                                   initial_analysis=analysis, active_section='namespaces')
        
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

        # Automatically provision the namespace database
        from webapp.namespace.setup import NamespaceProvisioner, CDBImporter

        try:
            # Provision database
            provisioner = NamespaceProvisioner()
            result = provisioner.provision_namespace(namespace.database_name, namespace.pcm_version)
            
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

            # Import CDB data if file was uploaded
            if sqlite_file_path:
                # Build full path to CDB file
                cdb_filename = os.path.basename(sqlite_file_path)
                cdb_path = os.path.join(get_cdb_import_path(namespace.slug), cdb_filename)
                
                if os.path.exists(cdb_path):
                    
                    with CDBImporter(cdb_path, namespace.build_connection_string(), namespace.pcm_version, expected_database=namespace.database_name) as importer:
                        stats = importer.import_all_tables(
                            expected_fingerprint=initial_fingerprint, seed_initial_identities=True,
                        )
                    session.pop('namespace_creation_review', None)

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
                else:
                    error_msg = f'CDB file not found at {cdb_path}'
                    logger.error(error_msg)
                    flash(error_msg, 'error')
            
        except Exception as e:
            error_msg = f'Error during provisioning/import: {str(e)}'
            logger.error(f"Namespace provisioning/import failed for {namespace.slug}: {error_msg}", exc_info=True)
            flash(error_msg, 'error')
            db.session.rollback()
        
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))
    
    return render_template('admin/namespace_form.html', form=form, active_section='namespaces')


@web_bp.route('/admin/namespaces/<int:namespace_id>/cdb', methods=['POST'])
@login_required
@admin_required
def admin_namespace_update_cdb(namespace_id):
    """Analyze an upload and apply the confirmed namespace upsert."""
    if not _can_update_namespaces():
        abort(403)

    namespace = Namespace.query.get_or_404(namespace_id)
    confirming = request.form.get('confirm') == 'yes'
    review = session.get('cdb_review', {})
    main_engine = db.engines.get('main') or db.engine
    if namespace.database_name == main_engine.url.database:
        abort(400)
    uploaded_file = request.files.get('cdb_file')
    if not confirming and (uploaded_file is None or not uploaded_file.filename):
        flash('Choose a .cdb or .sqlite file to update this namespace.', 'error')
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    from werkzeug.utils import secure_filename
    from webapp.config import ensure_namespace_directories, get_cdb_import_path
    from webapp.integrations.cdb.converter import convert_cdb_to_sqlite, validate_sqlite_cdb
    from webapp.namespace.setup import CDBImporter

    if confirming:
        if (review.get('namespace_id') != namespace.id
                or review.get('user_id') != current_user.id
                or review.get('pcm_version') != namespace.pcm_version
                or review.get('expires', 0) < datetime.utcnow().timestamp()):
            abort(400)
        original_name = review['filename']
    else:
        original_name = secure_filename(uploaded_file.filename)
    base_name, extension = os.path.splitext(original_name)
    extension = extension.lower()
    if extension not in {'.cdb', '.sqlite'}:
        flash('Only .cdb and .sqlite files can update a namespace.', 'error')
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    ensure_namespace_directories(namespace.slug)
    import_path = get_cdb_import_path(namespace.slug)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
    source_path = os.path.join(import_path, f'{base_name}_{timestamp}{extension}')
    if confirming:
        source_path = review['source_path']
    sqlite_path = review['sqlite_path'] if confirming else source_path
    paths_to_clean = list({source_path, sqlite_path})
    cdb_import_completed = False
    try:
        if not confirming:
            uploaded_file.save(source_path)
        if not confirming and extension == '.cdb':
            sqlite_path = os.path.join(import_path, f'{base_name}_{timestamp}.sqlite')
            paths_to_clean.append(sqlite_path)
            convert_cdb_to_sqlite(source_path, sqlite_path)
        else:
            validate_sqlite_cdb(sqlite_path)

        if not confirming:
            with CDBImporter(sqlite_path, namespace.build_connection_string(), namespace.pcm_version, expected_database=namespace.database_name) as importer:
                analysis = importer.analyze()
            session['cdb_review'] = dict(
                namespace_id=namespace.id, user_id=current_user.id,
                pcm_version=namespace.pcm_version, filename=original_name,
                source_path=source_path, sqlite_path=sqlite_path,
                fingerprint=analysis['fingerprint'],
                expires=(datetime.utcnow() + timedelta(hours=1)).timestamp(),
            )
            metadata_form = NamespaceMetadataForm(obj=namespace)
            metadata_form.pcm_version.choices = current_app.config['PCM_VERSIONS']
            return render_template('admin/namespace_detail.html', namespace=namespace,
                                   metadata_form=metadata_form, analysis=analysis,
                                   active_section='namespaces')
        # Preserve existing Activity Monitor archive synchronization.
        try:
            from webapp.tools.activity_monitor_global_history import archive_namespace_history

            namespace_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
            try:
                archive_namespace_history(
                    namespace_engine,
                    db.engines.get('main') or db.engine,
                    namespace.slug,
                    apply=True,
                )
            finally:
                namespace_engine.dispose()
        except Exception:
            logger.exception(
                'Could not synchronize the shared Activity Monitor archive before CDB update: %s',
                namespace.slug,
            )

        with CDBImporter(
            sqlite_path,
            namespace.build_connection_string(),
            namespace.pcm_version,
            expected_database=namespace.database_name,
        ) as importer:
            stats = importer.import_all_tables(expected_fingerprint=review['fingerprint'])
        try:
            from webapp.tools.activity_monitor_global_history import (
                restore_worlddb_history,
                sync_activity_monitor_roster,
            )

            destination_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
            try:
                worlddb_ids = sync_activity_monitor_roster(destination_engine)
                if worlddb_ids:
                    restore_worlddb_history(
                        db.engines.get('main') or db.engine,
                        destination_engine,
                        worlddb_ids,
                    )
            finally:
                destination_engine.dispose()
        except Exception:
            logger.exception('Could not restore archived Activity Monitor history for namespace %s', namespace.slug)
        cdb_import_completed = True
        session.pop('cdb_review', None)

        namespace.sqlite_file_path = f'cdb/import/{os.path.basename(sqlite_path)}'
        db.session.commit()
        AuditLog.log_action(
            action=AuditAction.NAMESPACE_CDB_IMPORTED,
            user=current_user,
            resource_type='namespace',
            resource_id=namespace.id,
            resource_name=namespace.name,
            details={
                'import_stats': stats,
                'cdb_file': os.path.basename(source_path),
                'operation': 'upsert_mapped_cdb_data',
                'activity_monitor_history_preserved': True,
            },
            ip_address=request.remote_addr,
        )
        db.session.commit()
    except Exception as error:
        session.pop('cdb_review', None)
        db.session.rollback()
        if isinstance(error, ValueError):
            flash(str(error), 'error')
        logger.exception('CDB update failed for namespace %s', namespace.slug)
        if not cdb_import_completed:
            for path in paths_to_clean:
                if os.path.exists(path):
                    os.remove(path)
            flash(
                'The new CDB could not be imported. The previous mapped CDB data and namespace history were kept.',
                'error',
            )
        else:
            flash(
                'The mapped CDB data was updated, but namespace metadata or its audit entry could not be saved. Check the namespace details before retrying.',
                'error',
            )
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))


@web_bp.route('/admin/namespaces/<int:namespace_id>/metadata', methods=['POST'])
@login_required
@admin_required
def admin_namespace_update_metadata(namespace_id):
    """Update the current season/version without renaming databases or files."""
    if not _can_update_namespaces():
        abort(403)
    namespace = Namespace.query.get_or_404(namespace_id)
    form = NamespaceMetadataForm()
    form.pcm_version.choices = current_app.config['PCM_VERSIONS']
    if not form.validate_on_submit():
        return {'errors': form.errors}, 400
    previous = {'database_year': namespace.database_year, 'pcm_version': namespace.pcm_version}
    namespace.database_year = form.database_year.data
    namespace.pcm_version = form.pcm_version.data
    AuditLog.log_action(
        action=AuditAction.NAMESPACE_UPDATED,
        user=current_user,
        resource_type='namespace',
        resource_id=namespace.id,
        resource_name=namespace.name,
        details={'previous': previous, 'current': {
            'database_year': namespace.database_year, 'pcm_version': namespace.pcm_version,
        }},
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash('Namespace season and PCM version updated.', 'success')
    return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))


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
        ('/riders', 'riders', teams, ['GET', 'POST']),
        ('/riders/snapshot', 'rider_snapshot', rider_snapshot, ['GET']),
        ('/teams/review', 'teams_review', object_review, ['GET']),
        ('/teams/change-log', 'teams_change_log', object_change_log, ['GET']),
        ('/riders/change-log', 'riders_change_log', object_change_log, ['GET']),
        ('/races/change-log', 'races_change_log', object_change_log, ['GET']),
        ('/riders/review', 'riders_review', object_review, ['GET']),
        ('/races/review', 'races_review', object_review, ['GET']),
        ('/races', 'races', stats_editor, ['GET', 'POST']),
        ('/races/cdb/<int:race_id>', 'cdb_race_detail', cdb_race_detail, ['GET']),
        ('/riders/bulk-editor', 'bulk_editor', stats_editor, ['GET', 'POST']),
        ('/riders/bulk-editor/new', 'bulk_editor_new', bulk_editor_new, ['GET']),
        ('/riders/bulk-tasks', 'rider_bulk_tasks', rider_bulk_tasks, ['GET', 'POST']),
        ('/stats-editor/lists/<int:list_id>/rename', 'stats_editor_rename_list', stats_editor_rename_list, ['POST']),
        ('/riders/stat-edit-log', 'stat_edit_log', stats_editor, ['GET', 'POST']),
        ('/riders/<int:rider_id>', 'rider_detail', rider_detail, ['GET']),
        ('/riders/<int:rider_id>/check-identity', 'check_rider_identity', check_rider_identity, ['POST']),
        ('/attributes/<entity_type>/<int:entity_id>/propose', 'propose_attribute', propose_attribute, ['POST']),
        ('/attributes/<entity_type>/review/<int:change_id>/<action>', 'review_attribute', review_attribute, ['POST']),
        ('/riders/<int:rider_id>/stats/<action>', 'rider_stats_action', rider_stats_action, ['POST']),
        ('/stats-editor', 'stats_editor', stats_editor, ['GET', 'POST']),
        ('/stats-editor/lists/create', 'stats_editor_create_list', stats_editor_create_list, ['POST']),
        ('/stats-editor/lists/<int:list_id>/add-rider', 'stats_editor_add_list_rider', stats_editor_add_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>/toggle-rider', 'stats_editor_toggle_list_rider', stats_editor_toggle_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>', 'stats_editor_list_detail', stats_editor_list_detail, ['GET']),
        ('/stats-editor/lists/<int:list_id>/save-stat-deltas', 'stats_editor_save_list_stat_deltas', stats_editor_save_list_stat_deltas, ['POST']),
        ('/stats-editor/lists/<int:list_id>/submit-for-approval', 'stats_editor_submit_list_for_approval', stats_editor_submit_list_for_approval, ['POST']),
        ('/stats-editor/lists/<int:list_id>/approve-stat-deltas', 'stats_editor_approve_list_stat_deltas', stats_editor_approve_list_stat_deltas, ['POST']),
        ('/stats-editor/lists/<int:list_id>/approve-attributes', 'stats_editor_approve_list_attributes', stats_editor_approve_list_attributes, ['POST']),
        ('/stats-editor/races/<int:race_id>', 'legacy_race_detail', legacy_race_detail, ['GET']),
        ('/stats-editor/races/<int:race_id>/<int:stage_id>', 'legacy_race_detail', legacy_race_detail, ['GET']),
        ('/stats-editor/races/<int:race_id>/get-result', 'legacy_race_get_result', race_get_result, ['POST']),
        ('/stats-editor/races/<int:race_id>/save-stat-deltas', 'legacy_race_save_stat_deltas', race_save_stat_deltas, ['POST']),
        ('/stats-editor/races/<int:race_id>/submit-for-approval', 'legacy_race_submit_for_approval', race_submit_for_approval, ['POST']),
        ('/stats-editor/races/<int:race_id>/approve-stat-deltas', 'legacy_race_approve_stat_deltas', race_approve_stat_deltas, ['POST']),
        ('/races/<int:race_id>', 'race_detail', race_detail, ['GET']),
        ('/races/<int:race_id>/<int:stage_id>', 'race_detail', race_detail, ['GET']),
        ('/races/<int:race_id>/get-result', 'race_get_result', race_get_result, ['POST']),
        ('/races/<int:race_id>/save-stat-deltas', 'race_save_stat_deltas', race_save_stat_deltas, ['POST']),
        ('/races/<int:race_id>/submit-for-approval', 'race_submit_for_approval', race_submit_for_approval, ['POST']),
        ('/races/<int:race_id>/approve-stat-deltas', 'race_approve_stat_deltas', race_approve_stat_deltas, ['POST']),
        ('/releases', 'releases', releases, ['GET', 'POST']),
        ('/releases/<int:release_id>/files/<int:file_id>/download', 'release_file_download', release_file_download, ['GET']),
        ('/namespace/select', 'select_namespace', select_namespace, ['POST']),
        ('/admin/integrations', 'admin_integrations_index', admin_integrations_index, ['GET']),
        ('/admin/integrations/<provider>', 'admin_integrations', admin_integrations, ['GET', 'POST']),
        ('/admin/integrations/task-groups/<int:group_id>', 'external_call_group_detail', external_call_group_detail, ['GET']),
        ('/admin/integrations/task-groups/<int:group_id>/tasks/<int:task_id>', 'external_call_task_detail', external_call_task_detail, ['GET']),
        ('/admin/integrations/scheduler', 'save_external_sync_schedule', save_external_sync_schedule, ['POST']),
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
