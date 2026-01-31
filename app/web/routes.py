"""Web routes for UI"""
from flask import render_template, redirect, url_for, flash, request, abort, current_app
from flask_login import login_user, logout_user, login_required, current_user
from app.web import web_bp
from app.web.forms import LoginForm, NamespaceForm, UserCreateForm, PasswordChangeForm, PasswordResetForm
from app.models import User, Resource, Namespace, Role
from app.extensions import db
from datetime import datetime
from functools import wraps


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
            flash('Invalid username or password', 'error')
    
    return render_template('login.html', form=form)


@web_bp.route('/logout')
@login_required
def logout():
    """User logout"""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('web.login'))


@web_bp.route('/dashboard')
@login_required
def dashboard():
    """User dashboard showing available app panels"""
    # Get all unique app panels the user has access to
    app_panels = {}
    
    if current_user.is_superadmin:
        # Superadmin gets all app panels (except admin)
        resources = Resource.query.all()
    else:
        # Get resources based on user's role permissions
        resources = []
        for role in current_user.roles:
            for permission in role.permissions:
                if permission.resource not in resources:
                    resources.append(permission.resource)
    
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
        database_name = f"pcm_{form.slug.data.replace('-', '_')}"
        
        # Create namespace (connection string will be set later when provisioning the database)
        namespace = Namespace(
            name=form.name.data,
            slug=form.slug.data,
            description=form.description.data,
            database_name=database_name,
            connection_string='',  # Placeholder - will be set during database provisioning
            pcm_version=form.pcm_version.data,
            database_year=form.database_year.data,
            status=form.status.data,
            created_by_id=current_user.id
        )
        
        db.session.add(namespace)
        db.session.commit()
        
        flash(f'Namespace "{namespace.name}" created successfully', 'success')
        return redirect(url_for('web.admin_namespaces'))
    
    return render_template('admin/namespace_form.html', form=form, active_section='namespaces')


@web_bp.route('/admin/namespaces/<int:namespace_id>/status', methods=['POST'])
@login_required
@admin_required
def admin_namespace_update_status(namespace_id):
    """Admin panel - Update namespace status via AJAX"""
    namespace = Namespace.query.get_or_404(namespace_id)
    new_status = request.form.get('status')
    
    if new_status not in ['active', 'draft', 'archived']:
        return {'error': 'Invalid status'}, 400
    
    namespace.status = new_status
    db.session.commit()
    
    flash(f'Namespace "{namespace.name}" status updated to {new_status}', 'success')
    return redirect(url_for('web.admin_namespaces'))


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
        db.session.commit()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('web.dashboard'))
    
    return render_template('change_password.html', form=form)

