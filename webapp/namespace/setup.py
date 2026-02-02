"""Data importer from SQLite CDB to PostgreSQL namespace database

Handles importing data from PCM CDB (SQLite) files into namespace databases.
"""
import sqlite3
import os
import logging
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, text
from sqlalchemy_utils import database_exists, create_database
from webapp.models.ns.schema import (
    NamespaceBase, NAMESPACE_TABLES, CDBCyclist,
    get_field_mapping, get_cdb_table_name
)
from datetime import datetime

logger = logging.getLogger(__name__)


class NamespaceProvisioner:
    """Handles provisioning of namespace databases"""
    
    def __init__(self, admin_db_url=None):
        """Initialize provisioner with admin database URL"""
        self.admin_db_url = admin_db_url or os.getenv('PROVISIONER_DATABASE_URL')
        if not self.admin_db_url:
            raise ValueError('Admin database URL not provided')
    
    def create_namespace_database(self, database_name):
        """Create a new namespace database
        
        Args:
            database_name: Name for the database (will be prefixed with ns_)
            
        Returns:
            Full database name (with ns_ prefix)
        """
        # Add ns_ prefix if not already present
        if not database_name.startswith('ns_'):
            full_db_name = f'ns_{database_name}'
        else:
            full_db_name = database_name
        
        # Connect to admin database to create new database
        admin_engine = create_engine(self.admin_db_url)
        
        # Check if database already exists
        with admin_engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            result = conn.execute(text(
                "SELECT 1 FROM pg_database WHERE datname = :dbname"
            ), {"dbname": full_db_name})
            
            if result.fetchone():
                logger.info(f"Database {full_db_name} already exists")
                return full_db_name
            
            # Create the database
            conn.execute(text(f'CREATE DATABASE {full_db_name}'))
            logger.info(f"Created database: {full_db_name}")
        
        admin_engine.dispose()
        return full_db_name
    
    def create_namespace_schema(self, connection_string):
        """Create tables in namespace database
        
        Args:
            connection_string: Full connection string to namespace database
        """
        engine = create_engine(connection_string)
        
        # Create all tables
        NamespaceBase.metadata.create_all(engine)
        logger.info(f"Created schema with {len(NAMESPACE_TABLES)} tables")
    
    
    def build_connection_string(self, database_name, base_url=None):
        """Build connection string for namespace database
        
        Args:
            database_name: Name of the namespace database
            base_url: Base connection URL (defaults to admin URL)
            
        Returns:
            Full connection string for the namespace database
        """
        base_url = base_url or self.admin_db_url
        
        # Parse the base URL and replace database name
        # Format: postgresql://user:pass@host:port/dbname
        if '://' in base_url:
            protocol_part, rest = base_url.split('://', 1)
            if '@' in rest:
                credentials, host_db = rest.rsplit('@', 1)
                host_part = host_db.split('/')[0]
                return f"{protocol_part}://{credentials}@{host_part}/{database_name}"
        
        raise ValueError(f"Invalid database URL format: {base_url}")
    
    def provision_namespace(self, database_name):
        """Full provisioning: create database and schema
        
        Args:
            database_name: Base name for the database (ns_ prefix added automatically)
            
        Returns:
            dict with database_name and connection_string
        """
        # Create the database
        full_db_name = self.create_namespace_database(database_name)
        
        # Build connection string
        connection_string = self.build_connection_string(full_db_name)
        
        # Create schema
        self.create_namespace_schema(connection_string)
        
        return {
            'database_name': full_db_name,
            'connection_string': connection_string
        }
    
    def drop_namespace_database(self, database_name):
        """Drop a namespace database (use with caution!)
        
        Args:
            database_name: Name of database to drop
        """
        # Add ns_ prefix if not already present
        if not database_name.startswith('ns_'):
            full_db_name = f'ns_{database_name}'
        else:
            full_db_name = database_name
        
        admin_engine = create_engine(self.admin_db_url)
        
        with admin_engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            # Terminate existing connections
            conn.execute(text(f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = :dbname
                AND pid <> pg_backend_pid()
            """), {"dbname": full_db_name})
            
            # Drop the database
            conn.execute(text(f'DROP DATABASE IF EXISTS {full_db_name}'))
            logger.info(f"Dropped database: {full_db_name}")
        
        admin_engine.dispose()


class CDBImporter:
    """Import data from SQLite CDB files to PostgreSQL namespace databases"""
    
    def __init__(self, sqlite_path, postgres_connection_string, pcm_version='PCM2025'):
        """Initialize importer
        
        Args:
            sqlite_path: Path to SQLite CDB file
            postgres_connection_string: Connection string to namespace PostgreSQL database
            pcm_version: PCM version for field mapping (default: PCM2025)
        """
        self.sqlite_path = sqlite_path
        self.postgres_connection_string = postgres_connection_string
        self.pcm_version = pcm_version
        self.sqlite_conn = None
        self.pg_engine = None
        self.pg_session = None
    
    def connect(self):
        """Establish connections to both databases"""
        # Connect to SQLite
        self.sqlite_conn = sqlite3.connect(self.sqlite_path)
        self.sqlite_conn.row_factory = sqlite3.Row  # Access columns by name
        
        # Connect to PostgreSQL
        self.pg_engine = create_engine(self.postgres_connection_string)
        Session = sessionmaker(bind=self.pg_engine)
        self.pg_session = Session()
    
    def disconnect(self):
        """Close all connections"""
        if self.sqlite_conn:
            self.sqlite_conn.close()
        if self.pg_session:
            self.pg_session.close()
        if self.pg_engine:
            self.pg_engine.dispose()
    
    def import_cyclists(self, batch_size=1000):
        """Import cyclist table from SQLite to PostgreSQL
        
        Uses PCM version mapping to translate CDB fields to internal model.
        
        Args:
            batch_size: Number of records to process at once
            
        Returns:
            Number of cyclists imported
        """
        # Get field mapping for this PCM version
        mapping = get_field_mapping(self.pcm_version, 'cyclist')
        cdb_table_name = mapping['cdb_table_name']
        field_map = mapping['fields']
        
        # Build SELECT query with CDB field names
        cdb_fields = list(field_map.values())
        query = f"SELECT {', '.join(cdb_fields)} FROM {cdb_table_name}"
        
        logger.info(f"Importing from {cdb_table_name} using {self.pcm_version} mapping")
        
        cursor = self.sqlite_conn.cursor()
        cursor.execute(query)
        
        total_imported = 0
        batch = []
        
        for row in cursor:
            # Map CDB fields to internal model fields
            cyclist_data = {}
            for internal_field, cdb_field in field_map.items():
                value = row[cdb_field]
                # Special handling for boolean fields
                if internal_field == 'is_locked' and value is not None:
                    value = bool(value)
                cyclist_data[internal_field] = value
            
            # Create cyclist object with internal field names
            cyclist = CDBCyclist(**cyclist_data)
            batch.append(cyclist)
            
            # Insert batch when it reaches batch_size
            if len(batch) >= batch_size:
                self.pg_session.bulk_save_objects(batch)
                self.pg_session.commit()
                total_imported += len(batch)
                logger.info(f"Imported {total_imported} cyclists...")
                batch = []
        
        # Insert remaining records
        if batch:
            self.pg_session.bulk_save_objects(batch)
            self.pg_session.commit()
            total_imported += len(batch)
        
        logger.info(f"Total cyclists imported: {total_imported}")
        return total_imported
    
    def import_all_tables(self):
        """Import all supported tables
        
        Returns:
            dict with import statistics for each table
        """
        stats = {}
        
        logger.info(f"Starting data import from CDB ({self.pcm_version})...")
        
        # Import cyclists
        logger.info("[1/1] Importing cyclists...")
        stats['cyclists'] = self.import_cyclists()
        
        logger.info("Import complete!")
        return stats
    
    def __enter__(self):
        """Context manager support"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup"""
        self.disconnect()
