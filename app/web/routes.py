"""Web routes for UI"""
from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.web import web_bp
from app.web.forms import LoginForm
from app.models import User, Resource
from app.extensions import db
from datetime import datetime


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
    """User dashboard showing available app sections"""
    # Get all unique app sections the user has access to
    app_sections = {}
    
    if current_user.is_superadmin:
        # Superadmin gets all app sections
        resources = Resource.query.all()
    else:
        # Get resources based on user's role permissions
        resources = []
        for role in current_user.roles:
            for permission in role.permissions:
                if permission.resource not in resources:
                    resources.append(permission.resource)
    
    # Group by app_section
    for resource in resources:
        section = resource.app_section
        if section not in app_sections:
            app_sections[section] = {
                'name': section,
                'display_name': section.replace('_', ' ').title(),
                'description': f'Manage and view {section.replace("_", " ")}',
                'icon': get_app_icon(section),
                'resources': []
            }
        app_sections[section]['resources'].append(resource.name)
    
    # Convert to list for template
    apps = list(app_sections.values())
    
    return render_template('dashboard.html', apps=apps)


def get_app_icon(app_section):
    """Get icon emoji for app section"""
    icons = {
        'admin': '⚙️',
        'stat_manager': '📊',
        'photo_manager': '📷',
        'palmares_manager': '🏆',
        'release_manager': '📦',
    }
    return icons.get(app_section, '📁')
