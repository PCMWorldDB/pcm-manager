"""Web routes for UI"""
import logging
import json
from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, abort, current_app, session
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from webapp.web import web_bp
from webapp.web.forms import LoginForm, NamespaceForm, UserCreateForm, PasswordChangeForm, PasswordResetForm
from webapp.models import User, Resource, Namespace, Role, Release, AuditLog, AuditAction, FirstCyclingRace
from webapp.integrations.firstcycling import FirstCyclingClient, FirstCyclingAPIError
from webapp.models.main.firstcycling import FirstCyclingRider, FirstCyclingTeam
from webapp.extensions import db
from functools import wraps

# Get logger for this module
logger = logging.getLogger(__name__)

STAT_DELTA_FIELDS = ('mo', 'mm', 'hil', 'tt', 'spr', 'acc', 'end', 'res', 'rec')


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
        'stat_manager': 'web.stat_manager',
        'release_manager': 'web.release_manager',
    }

    endpoint = panel_routes.get(app_panel)
    if not endpoint:
        return None
    return url_for(endpoint)


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


def _load_namespace_cyclist_stats(namespace, rider_ids):
    if namespace is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        _ensure_race_result_delta_table(engine)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_id,
                        stat_mo,
                        stat_mm,
                        stat_hil,
                        stat_tt,
                        stat_spr,
                        stat_acc,
                        stat_end,
                        stat_res,
                        stat_rec
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
            'hil': row['stat_hil'],
            'tt': row['stat_tt'],
            'spr': row['stat_spr'],
            'acc': row['stat_acc'],
            'end': row['stat_end'],
            'res': row['stat_res'],
            'rec': row['stat_rec'],
        }

    return stats_map


def _load_namespace_race_result_deltas(namespace, first_cycling_race_id, first_cycling_stage_id, rider_ids):
    """Load saved stat delta rows keyed by rider and stat name for one race."""
    if namespace is None or first_cycling_race_id is None or first_cycling_stage_id is None or not rider_ids:
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
                    FROM race_result_delta
                    WHERE first_cycling_race_id = :race_id
                                            AND first_cycling_stage_id = :stage_id
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


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_race_result_delta_table(engine):
    """Create delta table/indexes if they do not already exist in namespace DB."""
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS race_result_delta (
                id SERIAL PRIMARY KEY,
                first_cycling_race_id INTEGER NOT NULL,
                first_cycling_stage_id INTEGER NOT NULL DEFAULT 0,
                cyclist_pcm_id INTEGER NOT NULL,
                first_cycling_rider_id INTEGER NOT NULL,
                stat_name VARCHAR(20) NOT NULL,
                original_value INTEGER NOT NULL,
                final_value INTEGER NOT NULL,
                delta_value INTEGER NOT NULL,
                UNIQUE (first_cycling_race_id, first_cycling_stage_id, cyclist_pcm_id, first_cycling_rider_id, stat_name)
            )
            """
        ))
        conn.execute(text(
            """
            ALTER TABLE race_result_delta
            ADD COLUMN IF NOT EXISTS first_cycling_stage_id INTEGER NOT NULL DEFAULT 0
            """
        ))
        conn.execute(text(
            """
            DROP INDEX IF EXISTS idx_race_result_delta_race_rider
            """
        ))
        conn.execute(text(
            """
            DROP INDEX IF EXISTS idx_race_result_delta_race_stage_rider
            """
        ))
        conn.execute(text(
            """
            CREATE INDEX IF NOT EXISTS idx_race_result_delta_race_stage_rider
            ON race_result_delta (first_cycling_race_id, first_cycling_stage_id, first_cycling_rider_id)
            """
        ))
        conn.execute(text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_race_result_delta_scope
            ON race_result_delta (
                first_cycling_race_id,
                first_cycling_stage_id,
                cyclist_pcm_id,
                first_cycling_rider_id,
                stat_name
            )
            """
        ))


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
    is_result_saved = all_stages_saved if is_stage_race else has_race_scope_result
    is_future_race = race.start_date is not None and race.start_date > datetime.utcnow().date()

    return {
        'is_stage_race': is_stage_race,
        'is_result_saved': is_result_saved,
        'is_future_race': is_future_race,
    }


@web_bp.app_context_processor
def inject_namespace_selector_context():
    if not current_user.is_authenticated:
        return {
            'available_namespaces': [],
            'selected_namespace': None,
        }

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    return {
        'available_namespaces': available_namespaces,
        'selected_namespace': selected_namespace,
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
            
        if panel not in app_panels:
            app_panels[panel] = {
                'name': panel,
                'display_name': panel.replace('_', ' ').title(),
                'description': f'Manage and view {panel.replace("_", " ")}',
                'icon': get_app_icon(panel),
                'url': _get_panel_url(panel),
                'resources': []
            }
        app_panels[panel]['resources'].append(resource.name)
    
    # Convert to list for template
    apps = list(app_panels.values())
    
    return render_template('dashboard.html', apps=apps)


def get_app_icon(app_panel):
    """Get icon emoji for app panel"""
    icons = {
        'admin': '⚙️',
        'stat_manager': '📊',
        'photo_manager': '📷',
        'palmares_manager': '🏆',
        'release_manager': '📦',
    }
    return icons.get(app_panel, '📁')


@web_bp.route('/stat-manager')
@login_required
def stat_manager():
    """Stats manager panel: select season and filter races/stages."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    seasons = _load_config_seasons()
    selected_year = request.args.get('year', type=int)
    selected_gender = request.args.get('gender', type=str)

    if selected_gender is not None:
        selected_gender = selected_gender.strip()

    if selected_year is None or not selected_gender:
        default_season = _resolve_default_season(seasons)
        if default_season:
            selected_year = default_season['year']
            selected_gender = default_season['gender']

    season_selected = (
        selected_year is not None
        and isinstance(selected_gender, str)
        and selected_gender != ''
        and any(item['year'] == selected_year and item['gender'] == selected_gender for item in seasons)
    )

    races = []
    filter_choices = {
        'categories': [],
        'classifications': [],
    }

    selected_filters = {
        'race_name': request.args.get('race_name', type=str) or '',
        'category': request.args.get('category', type=str) or '',
        'classification': request.args.get('classification', type=str) or '',
        'start_date_from': request.args.get('start_date_from', type=str) or '',
        'start_date_to': request.args.get('start_date_to', type=str) or '',
        'is_stage_race': request.args.get('is_stage_race', type=str) or '',
    }

    if season_selected:
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

        query = base_query.options(selectinload(FirstCyclingRace.race_stages))

        if selected_filters['race_name']:
            query = query.filter(FirstCyclingRace.name.ilike(f"%{selected_filters['race_name']}%"))
        if selected_filters['category']:
            query = query.filter(FirstCyclingRace.category == selected_filters['category'])
        if selected_filters['classification']:
            query = query.filter(FirstCyclingRace.classification_label == selected_filters['classification'])

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

    return render_template(
        'stat_manager.html',
        seasons=seasons,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        races=races,
        filter_choices=filter_choices,
        selected_filters=selected_filters,
    )


@web_bp.route('/release-manager', methods=['GET', 'POST'])
@login_required
def release_manager():
    """Release manager panel with tabbed sections and overview actions."""
    if not _can_access_panel('release_manager'):
        abort(403)

    valid_tabs = ('overview', 'stats', 'palmares', 'photos')
    active_tab = (request.args.get('tab', type=str) or 'overview').strip().lower()
    if active_tab not in valid_tabs:
        return redirect(url_for('web.release_manager', tab='overview'))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)

    if request.method == 'POST' and active_tab == 'overview':
        if selected_namespace is None:
            flash('Select a namespace before creating a release.', 'warning')
            return redirect(url_for('web.release_manager', tab='overview'))

        version = (request.form.get('version', type=str) or '').strip()
        release_name = (request.form.get('release_name', type=str) or '').strip() or None
        release_type = (request.form.get('release_type', type=str) or 'minor').strip().lower()
        release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
        sqlite_file_path = (request.form.get('sqlite_file_path', type=str) or '').strip()

        if not version:
            flash('Release version is required.', 'error')
            return redirect(url_for('web.release_manager', tab='overview'))

        if release_type not in ('major', 'minor', 'patch', 'beta'):
            release_type = 'minor'

        if not sqlite_file_path:
            sqlite_file_path = selected_namespace.sqlite_file_path or ''

        if not sqlite_file_path:
            flash('SQLite file path is required to create a release.', 'error')
            return redirect(url_for('web.release_manager', tab='overview'))

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
            flash(f'Release {version} created as draft.', 'success')
        except IntegrityError:
            db.session.rollback()
            flash('Release version already exists for this namespace.', 'error')
        except Exception:
            db.session.rollback()
            logger.exception('Failed to create release')
            flash('Failed to create release.', 'error')

        return redirect(url_for('web.release_manager', tab='overview'))

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

    return render_template(
        'release_manager.html',
        active_tab=active_tab,
        selected_namespace=selected_namespace,
        releases=releases,
        summary=summary,
    )


@web_bp.route('/stat-manager/<int:race_id>')
@web_bp.route('/stat-manager/<int:race_id>/<int:stage_id>')
@login_required
def stat_manager_race_detail(race_id, stage_id=None):
    """Race detail page with optional stage-specific view."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.options(
        selectinload(FirstCyclingRace.race_stages),
        selectinload(FirstCyclingRace.race_results),
    ).filter_by(id=race_id).first_or_404()

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
    rider_stats_map = _load_namespace_cyclist_stats(selected_namespace, rider_ids)
    stage_scope_id = selected_stage.id if selected_stage is not None else 0
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

        base_stats = rider_stats_map.get(result.rider_id) or {}
        original_stats = dict(base_stats)
        final_stats = dict(base_stats)
        for stat_name, delta_info in (rider_delta_map.get(result.rider_id) or {}).items():
            final_value = delta_info.get('final_value')
            if final_value is None:
                continue
            final_stats[stat_name] = final_value

        display_rows.append({
            'result': result,
            'rider_name': rider_name,
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
        selected_namespace=selected_namespace,
        back_params={
            'year': request.args.get('year', type=int),
            'gender': request.args.get('gender', type=str),
            'race_name': request.args.get('race_name', type=str) or '',
            'category': request.args.get('category', type=str) or '',
            'classification': request.args.get('classification', type=str) or '',
            'start_date_from': request.args.get('start_date_from', type=str) or '',
            'start_date_to': request.args.get('start_date_to', type=str) or '',
            'is_stage_race': request.args.get('is_stage_race', type=str) or '',
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


@web_bp.route('/stat-manager/races/<int:race_pk>/get-result', methods=['POST'])
@login_required
def stat_manager_get_result(race_pk):
    """Fetch and persist race results for one race from FirstCycling."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.get_or_404(race_pk)

    selected_year = request.form.get('year', type=int)
    selected_gender = request.form.get('gender', type=str)
    category = request.form.get('category', type=str) or ''
    race_name = request.form.get('race_name', type=str) or ''
    classification = request.form.get('classification', type=str) or ''
    start_date_from = request.form.get('start_date_from', type=str) or ''
    start_date_to = request.form.get('start_date_to', type=str) or ''
    is_stage_race_filter = request.form.get('is_stage_race', type=str) or ''
    stage_id = request.form.get('stage_id', type=int)

    if selected_gender is not None:
        selected_gender = selected_gender.strip()

    if selected_year is None:
        selected_year = race.season_year
    if not selected_gender:
        selected_gender = race.gender

    if selected_year != race.season_year or selected_gender != race.gender:
        flash('Race season/gender mismatch for selected filters.', 'error')
        return redirect(url_for('web.stat_manager_race_detail', race_id=race.id))

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
            'web.stat_manager_race_detail',
            race_id=race.id,
            stage_id=stage_id,
            year=selected_year,
            gender=selected_gender,
            race_name=race_name,
            category=category,
            classification=classification,
            start_date_from=start_date_from,
            start_date_to=start_date_to,
            is_stage_race=is_stage_race_filter,
        ))

    return redirect(url_for(
        'web.stat_manager_race_detail',
        race_id=race.id,
        year=selected_year,
        gender=selected_gender,
        race_name=race_name,
        category=category,
        classification=classification,
        start_date_from=start_date_from,
        start_date_to=start_date_to,
        is_stage_race=is_stage_race_filter,
    ))


@web_bp.route('/stat-manager/races/<int:race_pk>/save-stat-deltas', methods=['POST'])
@login_required
def stat_manager_save_stat_deltas(race_pk):
    """Persist edited stat deltas for this race into the selected namespace database."""
    if not _can_access_panel('stat_manager'):
        abort(403)

    race = FirstCyclingRace.query.get_or_404(race_pk)

    selected_year = request.form.get('year', type=int)
    selected_gender = request.form.get('gender', type=str)
    category = request.form.get('category', type=str) or ''
    race_name = request.form.get('race_name', type=str) or ''
    classification = request.form.get('classification', type=str) or ''
    start_date_from = request.form.get('start_date_from', type=str) or ''
    start_date_to = request.form.get('start_date_to', type=str) or ''
    is_stage_race_filter = request.form.get('is_stage_race', type=str) or ''
    stage_id = request.form.get('stage_id', type=int)
    stage_scope_id = stage_id if stage_id is not None else 0

    if selected_gender is not None:
        selected_gender = selected_gender.strip()
    if selected_year is None:
        selected_year = race.season_year
    if not selected_gender:
        selected_gender = race.gender

    redirect_target = {
        'race_id': race.id,
        'year': selected_year,
        'gender': selected_gender,
        'race_name': race_name,
        'category': category,
        'classification': classification,
        'start_date_from': start_date_from,
        'start_date_to': start_date_to,
        'is_stage_race': is_stage_race_filter,
    }
    if stage_id is not None:
        redirect_target['stage_id'] = stage_id

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return redirect(url_for('web.stat_manager_race_detail', **redirect_target))

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.stat_manager_race_detail', **redirect_target))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.stat_manager_race_detail', **redirect_target))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.stat_manager_race_detail', **redirect_target))

    rider_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    rider_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)

    try:
        _ensure_race_result_delta_table(engine)

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
                            DELETE FROM race_result_delta
                            WHERE first_cycling_race_id = :race_id
                                                            AND first_cycling_stage_id = :stage_id
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
                        INSERT INTO race_result_delta (
                            first_cycling_race_id,
                            first_cycling_stage_id,
                            cyclist_pcm_id,
                            first_cycling_rider_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value
                        )
                        VALUES (
                            :race_id,
                            :stage_id,
                            :cyclist_pcm_id,
                            :rider_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value
                        )
                        ON CONFLICT (first_cycling_race_id, first_cycling_stage_id, cyclist_pcm_id, first_cycling_rider_id, stat_name)
                        DO UPDATE SET
                            original_value = EXCLUDED.original_value,
                            final_value = EXCLUDED.final_value,
                            delta_value = EXCLUDED.delta_value
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
            flash(f'Saved {saved_count} stat delta change(s).', 'success')
        else:
            flash('No stat delta changes were saved.', 'info')

        if skipped_count > 0:
            flash(f'Skipped {skipped_count} invalid or unmatched change(s).', 'warning')

    except Exception:
        logger.exception('Failed to save stat deltas for race %s', race.race_id)
        flash('Failed to save stat delta changes.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stat_manager_race_detail', **redirect_target))


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
    """Admin panel - Users management"""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users, active_section='users')


@web_bp.route('/admin/namespaces')
@login_required
@admin_required
def admin_namespaces():
    """Admin panel - Namespaces management"""
    namespaces = Namespace.query.order_by(Namespace.created_at.desc()).all()
    return render_template('admin/namespaces.html', namespaces=namespaces, active_section='namespaces')


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
        # Check if namespace with same name or slug exists
        existing = Namespace.query.filter(
            (Namespace.name == form.name.data) | (Namespace.slug == form.slug.data)
        ).first()
        
        if existing:
            flash('A namespace with this name or slug already exists', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces')
        
        # Generate database name from slug
        database_name = f"ns_{form.slug.data.replace('-', '_')}"
        
        # Handle CDB file upload
        sqlite_file_path = None
        if form.cdb_file.data:
            from werkzeug.utils import secure_filename
            from webapp.config import ensure_namespace_directories, get_cdb_import_path
            import os
            
            # Create namespace directories
            ensure_namespace_directories(form.slug.data)
            
            # Get the upload file
            file = form.cdb_file.data
            filename = secure_filename(file.filename)
            
            # Generate a unique filename with timestamp if needed
            base_name, ext = os.path.splitext(filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{base_name}_{timestamp}{ext}"
            
            # Save to namespace's import directory
            import_path = get_cdb_import_path(form.slug.data)
            file_path = os.path.join(import_path, unique_filename)
            
            file.save(file_path)
            
            # Store relative path from namespace root
            sqlite_file_path = f"cdb/import/{unique_filename}"
            
            flash(f'CDB database file "{filename}" uploaded successfully', 'success')
        
        # Create namespace
        namespace = Namespace(
            name=form.name.data,
            slug=form.slug.data,
            description=form.description.data,
            database_name=database_name,
            sqlite_file_path=sqlite_file_path,  # Store path to uploaded CDB file
            pcm_version=form.pcm_version.data,
            database_year=form.database_year.data,
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

