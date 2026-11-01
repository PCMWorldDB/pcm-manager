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
from webapp.models import User, Role, Namespace
from webapp.rbac_seed import sync_rbac_metadata
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
                if admin_role and not existing_user.roles.filter_by(id=admin_role.id).first():
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
    """Internal compatibility wrapper for RBAC metadata synchronization."""
    sync_rbac_metadata(log=log)


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


@namespace.command('install-activity-monitor')
@click.argument('namespace_slug')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt')
@with_appcontext
def install_activity_monitor_schema(namespace_slug, yes):
    """Explicitly create the shared Activity Monitor tables in one namespace.

    This command is intentionally separate from application startup and normal
    namespace provisioning. It creates missing tables only; it imports no
    rider data and does not alter existing tables.
    """
    selected_namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if selected_namespace is None:
        raise click.ClickException(f"Namespace '{namespace_slug}' was not found.")
    if not yes and not click.confirm(
        f"Create the Activity Monitor tables in namespace '{namespace_slug}'?"
    ):
        click.echo('Aborted; no namespace database was changed.')
        return

    from webapp.models.ns.activity_monitor import (
        ACTIVITY_MONITOR_TABLES,
        ActivityMonitorBase,
    )

    try:
        namespace_connection_string = selected_namespace.build_connection_string()
    except ValueError as exc:
        raise click.ClickException(
            f"Namespace '{namespace_slug}' has no usable database connection."
        ) from exc

    engine = create_engine(namespace_connection_string)
    try:
        ActivityMonitorBase.metadata.create_all(engine, checkfirst=True)
    except Exception as exc:
        raise click.ClickException(f'Could not create Activity Monitor tables: {exc}') from exc
    finally:
        engine.dispose()

    click.echo(
        f"Created or confirmed {len(ACTIVITY_MONITOR_TABLES)} Activity Monitor tables "
        f"in namespace '{namespace_slug}'. No rider records were imported."
    )


@namespace.command('install-global-activity-monitor-history')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt')
@with_appcontext
def install_global_activity_monitor_history(yes):
    """Explicitly install the cross-namespace history archive in the main DB."""
    if not yes and not click.confirm(
        'Create the Activity Monitor history archive table in the main database?'
    ):
        click.echo('Aborted; no database was changed.')
        return

    from webapp.tools.activity_monitor_global_history import install_global_history_schema

    engine = db.engines.get('main') or db.engine
    try:
        install_global_history_schema(engine)
    except Exception as exc:
        raise click.ClickException(
            f'Could not create the shared Activity Monitor history table: {exc}'
        ) from exc
    click.echo(
        'The shared Activity Monitor history archive is installed in the main database. '
        'No namespace records were copied.'
    )


@namespace.command('archive-activity-monitor-history')
@click.argument('namespace_slug')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Copy/update archived records. Without this flag, only a preview is performed.')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt for --apply')
@with_appcontext
def archive_activity_monitor_history(namespace_slug, apply_changes, yes):
    """Preview or synchronize Activity Monitor history into the main database."""
    selected_namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if selected_namespace is None:
        raise click.ClickException(f"Namespace '{namespace_slug}' was not found.")
    if apply_changes and not yes and not click.confirm(
        f"Copy Activity Monitor history from '{namespace_slug}' into the main database?"
    ):
        click.echo('Aborted; no history was copied.')
        return

    from webapp.tools.activity_monitor_global_history import archive_namespace_history

    try:
        source_url = selected_namespace.build_connection_string()
    except ValueError as exc:
        raise click.ClickException(
            f"Namespace '{namespace_slug}' has no usable database connection."
        ) from exc
    source_engine = create_engine(source_url, pool_pre_ping=True)
    main_engine = db.engines.get('main') or db.engine
    try:
        summary = archive_namespace_history(
            source_engine,
            main_engine,
            selected_namespace.slug,
            apply=apply_changes,
        )
    except Exception as exc:
        raise click.ClickException(
            f'Activity Monitor history archive stopped safely: {exc}'
        ) from exc
    finally:
        source_engine.dispose()

    click.echo(
        f"Namespace: {summary['namespace']}\n"
        f"History records found: {summary['records']}\n"
        f"Records to synchronize: {summary.get('records_to_sync', 0)}\n"
        f"Records written: {summary['written']}\n"
        f"Mode: {'apply' if apply_changes else 'dry run'}"
    )
    for table_name, count in summary['tables'].items():
        click.echo(f'  {table_name}: {count}')


@namespace.command('import-activity-monitor-history')
@click.argument('namespace_slug')
@click.option('--source', 'source_path', required=True, type=click.Path(exists=True, dir_okay=False, path_type=str),
              help='Path to the local Activity Monitor SQLite database.')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Write the validated history in one transaction. Without this flag, only a dry run is performed.')
@click.option('--actor-user-id', type=int, default=None,
              help='PCM Manager user ID to record as the initiating user.')
@with_appcontext
def import_activity_monitor_history(namespace_slug, source_path, apply_changes, actor_user_id):
    """Validate or import local Activity Monitor history into a namespace."""
    selected_namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if selected_namespace is None:
        raise click.ClickException(f"Namespace '{namespace_slug}' was not found.")
    try:
        namespace_connection_string = selected_namespace.build_connection_string()
    except ValueError as exc:
        raise click.ClickException(
            f"Namespace '{namespace_slug}' has no usable database connection."
        ) from exc

    from webapp.tools.activity_monitor_history_import import run_history_import

    engine = create_engine(namespace_connection_string, pool_pre_ping=True)
    try:
        run_history_import(
            engine,
            source_path,
            apply=apply_changes,
            actor_user_id=actor_user_id,
        )
    except Exception as exc:
        current_app.logger.error(
            'Activity Monitor history import stopped safely (%s).',
            type(exc).__name__,
        )
        raise click.ClickException(
            'Activity Monitor history import stopped safely; no partial records were committed.'
        ) from exc
    finally:
        engine.dispose()


@namespace.command('backfill-activity-monitor-history-tail')
@click.argument('namespace_slug')
@click.option('--source', 'source_path', required=True, type=click.Path(exists=True, dir_okay=False, path_type=str),
              help='The local Activity Monitor SQLite database for the imported rider roster.')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Append missing cycle/schedule history. Without this flag, only a read-only preview is shown.')
@with_appcontext
def backfill_activity_monitor_history_tail(namespace_slug, source_path, apply_changes):
    """Append durable source-cycle and scheduling history missing since import."""
    selected_namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if selected_namespace is None:
        raise click.ClickException(f"Namespace '{namespace_slug}' was not found.")
    try:
        namespace_connection_string = selected_namespace.build_connection_string()
    except ValueError as exc:
        raise click.ClickException(
            f"Namespace '{namespace_slug}' has no usable database connection."
        ) from exc
    from webapp.tools.activity_monitor_history_import import backfill_imported_activity_history
    engine = create_engine(namespace_connection_string, pool_pre_ping=True)
    try:
        backfill_imported_activity_history(engine, source_path, apply=apply_changes)
    except Exception as exc:
        current_app.logger.error('Activity Monitor history backfill stopped safely (%s).', type(exc).__name__)
        raise click.ClickException(
            'Activity Monitor history backfill stopped safely; no partial records were committed.'
        ) from exc
    finally:
        engine.dispose()


@namespace.command('backfill-activity-monitor-pcs-paths')
@click.argument('namespace_slug')
@click.option('--source', 'source_path', required=True, type=click.Path(exists=True, dir_okay=False, path_type=str),
              help='The exact local Activity Monitor SQLite database already imported.')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Fill only missing request paths for matching imported PCS checks.')
@with_appcontext
def backfill_activity_monitor_pcs_paths(namespace_slug, source_path, apply_changes):
    """Restore safe PCS request paths omitted by the original history import."""
    selected_namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if selected_namespace is None:
        raise click.ClickException(f"Namespace '{namespace_slug}' was not found.")
    try:
        namespace_connection_string = selected_namespace.build_connection_string()
    except ValueError as exc:
        raise click.ClickException(
            f"Namespace '{namespace_slug}' has no usable database connection."
        ) from exc
    from webapp.tools.activity_monitor_history_import import backfill_imported_pcs_request_paths
    engine = create_engine(namespace_connection_string, pool_pre_ping=True)
    try:
        backfill_imported_pcs_request_paths(engine, source_path, apply=apply_changes)
    except Exception as exc:
        current_app.logger.error('PCS path backfill stopped safely (%s).', type(exc).__name__)
        raise click.ClickException('PCS path backfill stopped safely; no partial changes were committed.') from exc
    finally:
        engine.dispose()

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
