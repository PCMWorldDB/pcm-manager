"""Web routes for UI"""
import logging
from datetime import datetime

from flask import render_template, redirect, url_for, flash, request, abort, current_app
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.orm import selectinload

from webapp.web import web_bp
from webapp.web.forms import LoginForm, NamespaceForm, UserCreateForm, PasswordChangeForm, PasswordResetForm
from webapp.models import User, Resource, Namespace, Role, AuditLog, AuditAction, FirstCyclingRace
from webapp.extensions import db
from functools import wraps

# Get logger for this module
logger = logging.getLogger(__name__)


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
    }

    endpoint = panel_routes.get(app_panel)
    if not endpoint:
        return None
    return url_for(endpoint)


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
        seasons.append({'year': year, 'gender': gender})

    unique = {(entry['year'], entry['gender']) for entry in seasons}
    return [
        {'year': year, 'gender': gender}
        for year, gender in sorted(unique, key=lambda value: (value[0], value[1]))
    ]

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

    season_selected = (
        selected_year is not None
        and isinstance(selected_gender, str)
        and selected_gender != ''
        and any(item['year'] == selected_year and item['gender'] == selected_gender for item in seasons)
    )

    races = []
    filter_choices = {
        'categories': [],
        'nation_names': [],
        'classifications': [],
    }

    selected_filters = {
        'category': request.args.get('category', type=str) or '',
        'nation_name': request.args.get('nation_name', type=str) or '',
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
        filter_choices['nation_names'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.nation_name).distinct().order_by(FirstCyclingRace.nation_name).all()
            if value
        ]
        filter_choices['classifications'] = [
            value for (value,) in base_query.with_entities(FirstCyclingRace.classification).distinct().order_by(FirstCyclingRace.classification).all()
            if value
        ]

        query = base_query.options(selectinload(FirstCyclingRace.race_stages))

        if selected_filters['category']:
            query = query.filter(FirstCyclingRace.category == selected_filters['category'])
        if selected_filters['nation_name']:
            query = query.filter(FirstCyclingRace.nation_name == selected_filters['nation_name'])
        if selected_filters['classification']:
            query = query.filter(FirstCyclingRace.classification == selected_filters['classification'])

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

