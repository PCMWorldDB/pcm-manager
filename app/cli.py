"""Flask CLI commands for database initialization and seeding"""
import os
import click
from flask.cli import with_appcontext
from app.extensions import db, bcrypt
from app.models import User, Role, Resource, Action, RolePermission
from app.config import (
    get_base_data_path,
    ensure_base_directories,
    ensure_namespace_directories
)


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Initialize database, seed roles, and create admin user (all in one)"""
    click.echo('=== PCM Manager Database Initialization ===')
    
    # Step 1: Create tables
    click.echo('\n1. Creating database tables...')
    db.create_all(bind_key='main')
    
    # Create association tables using raw SQL (to avoid FK resolution issues)
    from app.models.user import create_association_tables
    create_association_tables()
    
    # Configure User relationships AFTER all tables exist
    from app.models.user import configure_user_relationships
    configure_user_relationships()
    
    click.echo('   ✓ Database tables created')
    
    # Step 2: Seed roles
    click.echo('\n2. Seeding roles, resources, actions, and permissions...')
    _seed_roles()
    click.echo('   ✓ Roles seeded')
    
    # Step 3: Create admin user (if credentials provided via env vars)
    admin_username = os.getenv('ADMIN_USERNAME')
    admin_password = os.getenv('ADMIN_PASSWORD')
    
    if admin_username and admin_password:
        click.echo(f'\n3. Creating admin user: {admin_username}...')
        
        # Check if user already exists
        existing_user = User.query.filter_by(username=admin_username).first()
        if existing_user:
            click.echo(f'   ⚠ Admin user already exists, skipping')
        else:
            admin_user = User(
                username=admin_username,
                is_active=True,
                is_superadmin=True
            )
            admin_user.set_password(admin_password)
            
            # Assign admin role
            admin_role = Role.query.filter_by(name='admin').first()
            if admin_role:
                admin_user.roles.append(admin_role)
            
            db.session.add(admin_user)
            db.session.commit()
            click.echo(f'   ✓ Admin user created: {admin_username}')
    else:
        click.echo('\n3. Skipping admin user creation (no credentials provided)')
        click.echo('   Use "flask create-admin" to create an admin user manually')
    
    click.echo('\n=== Initialization Complete ===\n')


def _seed_roles():
    """Internal function to seed roles, resources, actions, and permissions"""
    # Create roles
    roles_data = [
        {'name': 'admin', 'display_name': 'Administrator', 
         'description': 'Full system access', 'is_system_role': True},
        {'name': 'release_manager', 'display_name': 'Release Manager',
         'description': 'Can create namespaces and publish releases', 'is_system_role': True},
        {'name': 'stat_editor', 'display_name': 'Stat Editor',
         'description': 'Can edit rider/team/race statistics', 'is_system_role': True},
        {'name': 'viewer', 'display_name': 'Viewer',
         'description': 'Read-only access to data', 'is_system_role': True},
    ]
    
    roles = {}
    for role_data in roles_data:
        role = Role.query.filter_by(name=role_data['name']).first()
        if not role:
            role = Role(**role_data)
            db.session.add(role)
            click.echo(f'  Created role: {role.name}')
        roles[role.name] = role
    
    # Create resources
    resources_data = [
        {'name': 'users', 'app_panel': 'admin', 'description': 'User management'},
        {'name': 'roles', 'app_panel': 'admin', 'description': 'Role management'},
        {'name': 'namespaces', 'app_panel': 'admin', 'description': 'Namespace management'},
        {'name': 'riders', 'app_panel': 'stat_manager', 'description': 'Rider data'},
        {'name': 'teams', 'app_panel': 'stat_manager', 'description': 'Team data'},
        {'name': 'races', 'app_panel': 'stat_manager', 'description': 'Race data'},
        {'name': 'stats', 'app_panel': 'stat_manager', 'description': 'Statistics'},
        {'name': 'photos', 'app_panel': 'photo_manager', 'description': 'Photo management'},
        {'name': 'palmares', 'app_panel': 'palmares_manager', 'description': 'Career achievements'},
        {'name': 'releases', 'app_panel': 'release_manager', 'description': 'Release management'},
    ]
    
    resources = {}
    for resource_data in resources_data:
        resource = Resource.query.filter_by(name=resource_data['name']).first()
        if not resource:
            resource = Resource(**resource_data)
            db.session.add(resource)
            click.echo(f'  Created resource: {resource.name}')
        resources[resource.name] = resource
    
    # Create actions
    actions_data = [
        {'name': 'create', 'description': 'Create new records'},
        {'name': 'read', 'description': 'View/read records'},
        {'name': 'update', 'description': 'Edit existing records'},
        {'name': 'delete', 'description': 'Delete records'},
        {'name': 'publish', 'description': 'Publish releases'},
        {'name': 'download', 'description': 'Download files'},
        {'name': 'import', 'description': 'Import data from external sources'},
        {'name': 'export', 'description': 'Export data'},
    ]
    
    actions = {}
    for action_data in actions_data:
        action = Action.query.filter_by(name=action_data['name']).first()
        if not action:
            action = Action(**action_data)
            db.session.add(action)
            click.echo(f'  Created action: {action.name}')
        actions[action.name] = action
    
    db.session.commit()
    
    # Create permissions matrix
    permissions_matrix = {
        'admin': {
            # Admin has all permissions on all resources
            'users': ['create', 'read', 'update', 'delete'],
            'roles': ['create', 'read', 'update', 'delete'],
            'namespaces': ['create', 'read', 'update', 'delete'],
            'riders': ['create', 'read', 'update', 'delete'],
            'teams': ['create', 'read', 'update', 'delete'],
            'races': ['create', 'read', 'update', 'delete'],
            'stats': ['create', 'read', 'update', 'delete'],
            'photos': ['create', 'read', 'update', 'delete'],
            'palmares': ['create', 'read', 'update', 'delete', 'import'],
            'releases': ['create', 'read', 'update', 'delete', 'publish', 'download', 'export'],
        },
        'release_manager': {
            'namespaces': ['create', 'read'],
            'riders': ['read'],
            'teams': ['read'],
            'races': ['read'],
            'stats': ['read'],
            'photos': ['read', 'create', 'update', 'delete'],
            'palmares': ['read'],
            'releases': ['create', 'read', 'update', 'publish', 'download', 'export'],
        },
        'stat_editor': {
            'riders': ['read', 'update'],
            'teams': ['read', 'update'],
            'races': ['read', 'update'],
            'stats': ['create', 'read', 'update'],
            'photos': ['read', 'create', 'update'],
            'palmares': ['create', 'read', 'update', 'import'],
            'releases': ['read', 'download'],
        },
        'viewer': {
            'riders': ['read'],
            'teams': ['read'],
            'races': ['read'],
            'stats': ['read'],
            'photos': ['read'],
            'palmares': ['read'],
            'releases': ['read', 'download'],
        },
    }
    
    for role_name, resource_actions in permissions_matrix.items():
        role = roles[role_name]
        for resource_name, action_names in resource_actions.items():
            resource = resources[resource_name]
            for action_name in action_names:
                action = actions[action_name]
                
                # Check if permission already exists
                existing = RolePermission.query.filter_by(
                    role_id=role.id,
                    resource_id=resource.id,
                    action_id=action.id
                ).first()
                
                if not existing:
                    permission = RolePermission(
                        role_id=role.id,
                        resource_id=resource.id,
                        action_id=action.id
                    )
                    db.session.add(permission)
    
    db.session.commit()
    click.echo('Roles, resources, actions, and permissions seeded successfully!')


@click.command('seed-roles')
@with_appcontext
def seed_roles_command():
    """Seed initial roles, resources, actions, and permissions"""
    click.echo('Seeding roles, resources, actions, and permissions...')
    _seed_roles()


@click.command('create-admin')
@click.option('--username', prompt=True, help='Admin username')
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True, help='Admin password')
@with_appcontext
def create_admin_command(username, password):
    """Create an admin user"""
    
    # Check if user already exists
    if User.query.filter_by(username=username).first():
        click.echo(f'Error: User with username {username} already exists')
        return
    
    # Create admin user
    admin_user = User(
        username=username,
        is_active=True,
        is_superadmin=True
    )
    admin_user.set_password(password)
    
    # Assign admin role
    admin_role = Role.query.filter_by(name='admin').first()
    if admin_role:
        admin_user.roles.append(admin_role)
    
    db.session.add(admin_user)
    db.session.commit()
    
    click.echo(f'Admin user created successfully: {username}')


@click.command('init-ns-data-dirs')
@click.option('--namespace', help='Specific namespace to create directories for')
@with_appcontext
def init_data_dirs_command(namespace):
    """Create data directory structure for CDB files"""
    from app.models import Namespace
    
    # Ensure base directories exist first
    base_dirs = ensure_base_directories()
    base_path = get_base_data_path()
    click.echo(f'Ensuring base directories exist: {get_base_data_path()}')
    
    if namespace:
        # Create for specific namespace
        namespaces_to_create = [namespace]
    else:
        # Create for all namespaces in database
        namespaces_to_create = [ns.name for ns in Namespace.query.all()]
        
        if not namespaces_to_create:
            click.echo('No namespaces found in database. Provide --namespace option or create namespaces first.')
            return
    
    click.echo(f'Creating data directory structure in: {base_path}')
    
    for ns_name in namespaces_to_create:
        # Create namespace directories using commons functions
        created_dirs = ensure_namespace_directories(ns_name)
        
        click.echo(f'  ✓ Created: data/{ns_name}/cdb/import')
        click.echo(f'  ✓ Created: data/{ns_name}/cdb/releases')
        click.echo(f'  ✓ Created: data/{ns_name}/photos')
    
    click.echo('\nData directory structure created successfully!')
