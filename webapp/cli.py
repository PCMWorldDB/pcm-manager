"""Flask CLI commands for database initialization and seeding"""
import os
import sqlite3
import click
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


def sync_firstcycling_seasons():
    """Ensure FirstCycling data exists for configured seasons/genders."""
    from webapp.integrations.firstcycling import FirstCyclingClient, FirstCyclingAPIError
    from webapp.models.main.firstcycling import FirstCyclingRace, FirstCyclingTeam, FirstCyclingRider

    seasons_config = current_app.config.get('SEASONS', []) or []
    if not seasons_config:
        click.echo('   - No SEASONS config found; skipping FirstCycling season sync check')
        return

    season_entries = []
    if isinstance(seasons_config, list):
        season_entries = list(seasons_config)
    else:
        click.echo('   - Invalid SEASONS config format; expected list of dicts', err=True)
        return

    if not season_entries:
        click.echo('   - SEASONS config is empty; skipping FirstCycling season sync check')
        return

    for season_settings in season_entries:
        if not isinstance(season_settings, dict):
            click.echo(f"   - Skipping invalid season entry '{season_settings}'")
            continue

        season_key = season_settings.get('year')
        try:
            season_year = int(season_key)
        except (TypeError, ValueError):
            click.echo(f"   - Skipping invalid season key '{season_key}'")
            continue

        gender = season_settings.get('gender')
        if gender not in ('Men', 'Women'):
            click.echo(f"   - Skipping invalid gender '{gender}'")
            continue

        client = FirstCyclingClient()

        race_query = FirstCyclingRace.query.filter_by(season_year=season_year, gender=gender)
        team_query = FirstCyclingTeam.query.filter_by(team_year=season_year, gender=gender)

        race_count = race_query.count()
        team_count = team_query.count()

        if race_count > 0 and team_count > 0:
            click.echo(
                f'   - FirstCycling data already present for season {season_year} [{gender}] '
                f'(races={race_count}, teams={team_count}'
            )
            continue

        click.echo(
            f'   - Missing FirstCycling data for season {season_year} [{gender}] '
            f'(races={race_count}, teams={team_count}); syncing...'
        )

        try:
            summary = client.sync_season_roster(
                season_year=season_year,
                gender=gender,
                commit=True,
            )
            click.echo(
                f"     ✓ Sync complete for {season_year} [{gender}] "
                f"(races={summary['races']['synced']}, teams={summary['teams']['fetched']})"
                f"riders={summary['riders']['fetched']})"
            )
        except FirstCyclingAPIError as exc:
            click.echo(f'     ✗ FirstCycling sync failed for {season_year} [{gender}]: {exc}', err=True)
        except Exception as exc:
            click.echo(f'     ✗ Unexpected sync error for {season_year} [{gender}]: {exc}', err=True)


@click.command('init-db')
@with_appcontext
def init_db_command():
    """Initialize database, seed roles, and create admin user (all in one)"""
    click.echo('=== PCM Manager Database Initialization ===')
    
    # Step 1: Create tables
    click.echo('\n1. Creating database tables...')
    db.create_all(bind_key='main')
    
    # Create association tables using raw SQL (to avoid FK resolution issues)
    from webapp.models.main.user import create_association_tables
    create_association_tables()
    
    # Configure User relationships AFTER all tables exist
    from webapp.models.main.user import configure_user_relationships
    configure_user_relationships()
    
    click.echo('   ✓ Database tables created')
    
    # Step 2: Seed roles
    click.echo('\n2. Seeding roles, resources, actions, and permissions...')
    _seed_roles()
    click.echo('   ✓ Roles seeded')
    
    # Step 3: Ensure external FirstCycling season data exists for configured seasons/genders
    click.echo('\n3. Syncing FirstCycling seasons...')
    sync_firstcycling_seasons()
    click.echo('   ✓ FirstCycling season sync completed')

    # Step 4: Create admin user (if credentials provided via env vars)
    admin_username = os.getenv('ADMIN_USERNAME')
    admin_password = os.getenv('ADMIN_PASSWORD')
    
    if admin_username and admin_password:
        click.echo(f'\n4. Creating admin user: {admin_username}...')
        admin_role = Role.query.filter_by(name='admin').first()
        
        # Check if user already exists
        existing_user = User.query.filter_by(username=admin_username).first()
        if existing_user:
            # Idempotent self-heal: ensure role assignment exists on reruns.
            if admin_role and not existing_user.has_role('admin'):
                existing_user.roles.append(admin_role)
                db.session.commit()
                click.echo('   ✓ Existing admin user found; admin role reattached')
            else:
                click.echo('   ⚠ Admin user already exists, skipping')
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
            click.echo(f'   ✓ Admin user created: {admin_username}')
    else:
        click.echo('\n4. Skipping admin user creation (no credentials provided)')
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
        {'name': 'riders', 'app_panel': 'admin', 'description': 'Rider data'},
        {'name': 'teams', 'app_panel': 'admin', 'description': 'Team data'},
        {'name': 'races', 'app_panel': 'admin', 'description': 'Race data'},
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
