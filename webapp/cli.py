"""Flask CLI commands for database initialization and seeding"""
import os
import time
import sqlite3
from contextlib import contextmanager
import click
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError
from flask import current_app
from flask.cli import with_appcontext
from webapp.extensions import db, bcrypt
from webapp.models import User, Role, Resource, Action, RolePermission, Namespace
from webapp.config import (
    get_base_data_path,
    ensure_base_directories,
    ensure_namespace_directories,
    get_cdb_import_path
)
from webapp.namespace.setup import NamespaceProvisioner, CDBImporter


@contextmanager
def _startup_init_lock(log=None):
    """Use a PostgreSQL advisory lock to prevent concurrent startup initialization."""
    if log is None:
        log = lambda _: None

    provisioner_url = os.getenv('PROVISIONER_DATABASE_URL')
    engine = None
    owns_engine = False

    if provisioner_url:
        engine = create_engine(provisioner_url, isolation_level='AUTOCOMMIT')
        owns_engine = True
    else:
        engine = db.engines.get('main') or db.engine

    if engine.dialect.name != 'postgresql':
        # Non-Postgres environments (e.g. tests) do not support advisory locks.
        yield
        return

    lock_key = 792211001
    conn = engine.connect()
    lock_acquired = False
    try:
        log('   Waiting for startup initialization lock...')
        conn.execute(text('SELECT pg_advisory_lock(:lock_key)'), {'lock_key': lock_key})
        lock_acquired = True
        log('   ✓ Startup initialization lock acquired')
        yield
    finally:
        try:
            if lock_acquired:
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn.execute(text('SELECT pg_advisory_unlock(:lock_key)'), {'lock_key': lock_key})
        finally:
            conn.close()
            if owns_engine:
                engine.dispose()


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Initialize database, seed roles, and create admin user (all in one)"""
    initialize_database(log=click.echo)


def initialize_database(log=None):
    """Initialize database schema, seed data, and optional admin user."""
    if log is None:
        log = lambda _: None

    log('=== WorldDB Database Initialization ===')

    with _startup_init_lock(log=log):
        # Step 0: Provision database and required extension
        log('\n0. Ensuring database and extensions are provisioned...')
        _provision_main_database(log=log)
        log('   ✓ Database provisioning complete')

        # Step 1: Create tables
        log('\n1. Creating database tables...')
        db.create_all(bind_key='main')
        with db.engine.begin() as conn:
            conn.execute(text("""
                ALTER TABLE namespaces
                ADD COLUMN IF NOT EXISTS gender VARCHAR(20) NOT NULL DEFAULT 'Men'
            """))
            conn.execute(text("""
                ALTER TABLE firstcycling_teams
                ADD COLUMN IF NOT EXISTS rider_ids JSONB
            """))
            conn.execute(text("""
                ALTER TABLE users
                ALTER COLUMN password_hash DROP NOT NULL
            """))
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0
            """))
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS login_locked_until TIMESTAMP
            """))
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS discord_user_id VARCHAR(32)
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS ix_users_discord_user_id
                ON users (discord_user_id)
            """))

        # Create association tables using raw SQL (to avoid FK resolution issues)
        from webapp.models.main.user import create_association_tables
        create_association_tables()

        # Configure User relationships AFTER all tables exist
        from webapp.models.main.user import configure_user_relationships
        configure_user_relationships()

        log('   ✓ Database tables created')

        # Step 2: Seed roles
        log('\n2. Seeding roles, resources, actions, and permissions...')
        _seed_roles(log=log)
        log('   ✓ Roles seeded')

        # Step 3: Create admin user (if credentials provided via env vars)
        admin_username = os.getenv('ADMIN_USERNAME')
        admin_password = os.getenv('ADMIN_PASSWORD')

        if admin_username and admin_password:
            log(f'\n3. Creating admin user: {admin_username}...')
            admin_role = Role.query.filter_by(name='admin').first()

            # Check if user already exists
            existing_user = User.query.filter_by(username=admin_username).first()
            if existing_user:
                # Idempotent self-heal: ensure role assignment exists on reruns.
                if admin_role and not existing_user.has_role('admin'):
                    existing_user.roles.append(admin_role)
                    db.session.commit()
                    log('   ✓ Existing admin user found; admin role reattached')
                else:
                    log('   ⚠ Admin user already exists, skipping')
            else:
                admin_user = User(
                    username=admin_username,
                    is_active=True,
                    is_superadmin=True
                )
                admin_user.set_password(admin_password)

                # Assign admin role
                if admin_role:
                    admin_user.roles.append(admin_role)

                db.session.add(admin_user)
                db.session.commit()
                log(f'   ✓ Admin user created: {admin_username}')
        else:
            log('\n3. Skipping admin user creation (no credentials provided)')
            log('   Use "flask create-admin" to create an admin user manually')

    log('\n=== Initialization Complete ===\n')


def _provision_main_database(log=None):
    """Ensure main database exists and required extensions are enabled."""
    if log is None:
        log = lambda _: None

    database_url = os.getenv('DATABASE_URL')
    provisioner_url = os.getenv('PROVISIONER_DATABASE_URL')

    if not database_url:
        raise click.ClickException('DATABASE_URL is not configured')

    if not provisioner_url:
        raise click.ClickException('PROVISIONER_DATABASE_URL is not configured')

    main_db_name = make_url(database_url).database
    if not main_db_name:
        raise click.ClickException('Could not determine database name from DATABASE_URL')

    retries = 10
    delay_seconds = 2
    last_error = None

    # Create the target application database if it does not exist.
    for attempt in range(1, retries + 1):
        try:
            admin_engine = create_engine(provisioner_url, isolation_level='AUTOCOMMIT')
            with admin_engine.connect() as conn:
                exists = conn.execute(
                    text('SELECT 1 FROM pg_database WHERE datname = :database_name'),
                    {'database_name': main_db_name}
                ).scalar()

                if not exists:
                    safe_name = main_db_name.replace('"', '""')
                    conn.execute(text(f'CREATE DATABASE "{safe_name}"'))
                    log(f'   Created database: {main_db_name}')
                else:
                    log(f'   Database already exists: {main_db_name}')
            admin_engine.dispose()
            break
        except OperationalError as exc:
            last_error = exc
            if attempt == retries:
                raise click.ClickException(
                    f'Unable to connect to provisioner database after {retries} attempts: {exc}'
                )
            log(f'   Provisioning database not ready (attempt {attempt}/{retries}), retrying...')
            time.sleep(delay_seconds)

    if last_error:
        raise click.ClickException(str(last_error))


def _seed_roles(log=None):
    """Internal function to seed roles, resources, actions, and permissions"""
    if log is None:
        log = lambda _: None

    # Create roles
    roles_data = [
        {'name': 'admin', 'display_name': 'Administrator', 
         'description': 'Full system access', 'is_system_role': True},
        {'name': 'release_manager', 'display_name': 'Releases',
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
            log(f'  Created role: {role.name}')
        else:
            updated_fields = []
            for field_name in ('display_name', 'description', 'is_system_role'):
                new_value = role_data[field_name]
                if getattr(role, field_name) != new_value:
                    setattr(role, field_name, new_value)
                    updated_fields.append(field_name)

            if updated_fields:
                log(f"  Updated role: {role.name} ({', '.join(updated_fields)})")
        roles[role.name] = role
    
    # Create resources
    resources_data = [
        {'name': 'users', 'app_panel': 'admin', 'description': 'User management'},
        {'name': 'roles', 'app_panel': 'admin', 'description': 'Role management'},
        {'name': 'namespaces', 'app_panel': 'admin', 'description': 'Namespace management'},
        {'name': 'riders', 'app_panel': 'admin', 'description': 'Rider data'},
        {'name': 'teams', 'app_panel': 'team_manager', 'description': 'Manage teams and rosters'},
        {'name': 'races', 'app_panel': 'admin', 'description': 'Race data'},
        {'name': 'stats', 'app_panel': 'stat_manager', 'description': 'Maintain rider stats from race results'},
        {'name': 'photos', 'app_panel': 'photo_manager', 'description': 'Maintain rider photos'},
        {'name': 'palmares', 'app_panel': 'palmares_manager', 'description': 'Maintain rider historical race results'},
        {'name': 'releases', 'app_panel': 'release_manager', 'description': 'Manage database releases'},
    ]
    
    resources = {}
    for resource_data in resources_data:
        resource = Resource.query.filter_by(name=resource_data['name']).first()
        if not resource:
            resource = Resource(**resource_data)
            db.session.add(resource)
            log(f'  Created resource: {resource.name}')
        else:
            updated_fields = []
            for field_name in ('app_panel', 'description'):
                new_value = resource_data[field_name]
                if getattr(resource, field_name) != new_value:
                    setattr(resource, field_name, new_value)
                    updated_fields.append(field_name)

            if updated_fields:
                log(f"  Updated resource: {resource.name} ({', '.join(updated_fields)})")
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
            log(f'  Created action: {action.name}')
        else:
            if action.description != action_data['description']:
                action.description = action_data['description']
                log(f'  Updated action: {action.name} (description)')
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
    log('Roles, resources, actions, and permissions seeded successfully!')


@click.command('seed-roles')
@with_appcontext
def seed_roles_command():
    """Seed initial roles, resources, actions, and permissions"""
    click.echo('Seeding roles, resources, actions, and permissions...')
    _seed_roles()



@click.group()
def namespace():
    """Namespace database management commands"""
    pass

@namespace.command('drop')
@click.argument('namespace_slug')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
@with_appcontext
def drop_namespace(namespace_slug, yes):
    """Drop a namespace database (DESTRUCTIVE!)
    
    Permanently deletes the namespace database and all its data.
    
    Usage: flask namespace drop <namespace-slug> [--yes]
    """
    # Find the namespace
    ns = Namespace.query.filter_by(slug=namespace_slug).first()
    if not ns:
        click.echo(f"Error: Namespace '{namespace_slug}' not found", err=True)
        return
    
    if not ns.connection_string:
        click.echo("Namespace database not provisioned - nothing to drop")
        return
    
    # Confirm deletion
    if not yes:
        click.echo(f"WARNING: This will permanently delete the database '{ns.database_name}'")
        click.echo("All data will be lost!")
        if not click.confirm('Are you sure you want to continue?'):
            click.echo("Aborted")
            return
    
    try:
        provisioner = NamespaceProvisioner()
        provisioner.drop_namespace_database(ns.database_name)
        
        # Clear connection string
        ns.connection_string = ''
        db.session.commit()
        
        click.echo(f"✓ Database '{ns.database_name}' dropped successfully")
        
    except Exception as e:
        click.echo(f"Error dropping namespace database: {e}", err=True)


@namespace.command('list')
@with_appcontext
def list_namespaces():
    """List all namespaces and their status"""
    namespaces = Namespace.query.order_by(Namespace.created_at.desc()).all()
    
    if not namespaces:
        click.echo("No namespaces found")
        return
    
    click.echo("\nNamespaces:")
    click.echo("="*80)
    
    for ns in namespaces:
        provisioned = "✓" if ns.connection_string else "✗"
        has_cdb = "✓" if ns.sqlite_file_path else "✗"
        
        click.echo(f"{ns.slug:20s} | {ns.name:30s} | Provisioned: {provisioned} | CDB: {has_cdb}")
    
    click.echo("="*80)


@click.group()
def cdb():
    """CDB analysis and inspection commands"""
    pass


@cdb.command('analyze')
@click.argument('sqlite_path')
def analyze_cdb(sqlite_path):
    """Analyze SQLite CDB file and display table structure
    
    Connects to a PCM CDB (SQLite) file and displays all tables with their columns.
    
    Usage: flask cdb analyze <path_to_sqlite_file>
    """
    if not os.path.exists(sqlite_path):
        click.echo(f"Error: File not found: {sqlite_path}", err=True)
        return
    
    conn = sqlite3.connect(sqlite_path)
    cursor = conn.cursor()
    
    # Get all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    
    click.echo(f"\n{'='*80}")
    click.echo(f"CDB Analysis: {os.path.basename(sqlite_path)}")
    click.echo(f"{'='*80}\n")
    click.echo(f"Found {len(tables)} tables\n")
    
    # Analyze each table
    for table_name in tables:
        # Get column information
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        
        # Get row count
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        
        click.echo(f"Table: {table_name} ({row_count} rows)")
        click.echo(f"{'-'*80}")
        for col in columns:
            col_id, col_name, col_type, not_null, default_val, is_pk = col
            pk_marker = " [PK]" if is_pk else ""
            null_marker = " NOT NULL" if not_null else ""
            click.echo(f"  {col_name:<40} {col_type:<15}{pk_marker}{null_marker}")
        click.echo()
    
    conn.close()


@cdb.command('inspect')
@click.argument('sqlite_path')
@click.argument('table_name')
def inspect_table(sqlite_path, table_name):
    """Inspect a specific table in a SQLite CDB file
    
    Shows column names, types, and generates YAML mapping template.
    
    Usage: flask cdb inspect <sqlite_path> <table_name>
    """
    if not os.path.exists(sqlite_path):
        click.echo(f"Error: File not found: {sqlite_path}", err=True)
        return
    
    try:
        conn = sqlite3.connect(sqlite_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        if not cursor.fetchone():
            click.echo(f"Error: Table '{table_name}' not found in database", err=True)
            click.echo("\nAvailable tables:")
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            for row in cursor.fetchall():
                click.echo(f"  - {row[0]}")
            conn.close()
            return
        
        # Get column information
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()
        
        # Get row count
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row_count = cursor.fetchone()[0]
        
        # Print header
        click.echo(f"\n{'='*80}")
        click.echo(f"Table: {table_name}")
        click.echo(f"Rows: {row_count:,}")
        click.echo(f"{'='*80}\n")
        
        # Print column information
        click.echo(f"{'Column Name':<40} {'Type':<15} {'Flags'}")
        click.echo(f"{'-'*80}")
        
        for col in columns:
            col_id, col_name, col_type, not_null, default_val, is_pk = col
            
            flags = []
            if is_pk:
                flags.append("PRIMARY KEY")
            if not_null:
                flags.append("NOT NULL")
            if default_val is not None:
                flags.append(f"DEFAULT {default_val}")
            
            flags_str = ", ".join(flags) if flags else ""
            click.echo(f"{col_name:<40} {col_type:<15} {flags_str}")
        
        click.echo()
        
        # Print column names as comma-separated list
        click.echo("Column names (comma-separated):")
        click.echo(", ".join([col[1] for col in columns]))
        click.echo()
        
        # Print YAML mapping template
        click.echo("\nYAML field mapping template:")
        for col in columns:
            col_name = col[1]
            # Suggest internal field name
            internal_name = col_name.lower().replace('gene_sz_', '').replace('gene_i_', '').replace('gene_f_', '').replace('gene_b_', '').replace('fkid', '').replace('value_', '')
            click.echo(f"      # {internal_name}: {col_name}")
        
        conn.close()
        
    except sqlite3.Error as e:
        click.echo(f"SQLite error: {e}", err=True)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
