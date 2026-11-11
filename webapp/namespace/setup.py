"""Data importer from SQLite CDB to PostgreSQL namespace database

Handles importing data from PCM CDB (SQLite) files into namespace databases.
"""
import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy_utils import database_exists, create_database
from webapp.models.ns.schema import NamespaceBase
from webapp.integrations.cdb import cdb_import
from webapp.integrations.cdb.field_configuration import seed_fields
from webapp.config import get_setting
from datetime import datetime

logger = logging.getLogger(__name__)


class NamespaceProvisioner:
    """Handles provisioning of namespace databases"""
    
    def __init__(self, admin_db_url=None):
        """Initialize provisioner with admin database URL"""
        self.admin_db_url = admin_db_url or get_setting('PROVISIONER_DATABASE_URL')
        if not self.admin_db_url:
            raise ValueError('Admin database URL not provided')
    
    def create_namespace_database(self, database_name):
        """Create a new namespace database
        
        Args:
            database_name: Name for the database
            
        Returns:
            Created database name
        """
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
                return full_db_name
            
            # Create the database
            safe_db_name = full_db_name.replace('"', '""')
            conn.execute(text(f'CREATE DATABASE "{safe_db_name}"'))
        
        admin_engine.dispose()
        return full_db_name
    
    def create_namespace_schema(self, connection_string, pcm_version='PCM2025'):
        """Create tables in namespace database
        
        Args:
            connection_string: Full connection string to namespace database
        """
        engine = create_engine(connection_string)
        
        try:
            with engine.begin() as conn:
                NamespaceBase.metadata.create_all(conn)
                seed_fields(conn, pcm_version)
        finally:
            engine.dispose()
    
    
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
    
    def provision_namespace(self, database_name, pcm_version='PCM2025'):
        """Full provisioning: create database and schema
        
        Args:
            database_name: Name for the namespace database
            
        Returns:
            dict with database_name and connection_string
        """
        # Create the database
        full_db_name = self.create_namespace_database(database_name)
        
        # Build connection string
        connection_string = self.build_connection_string(full_db_name)
        
        # Create schema
        self.create_namespace_schema(connection_string, pcm_version)
        
        return {
            'database_name': full_db_name,
            'connection_string': connection_string
        }
    
    def drop_namespace_database(self, database_name):
        """Drop a namespace database (use with caution!)
        
        Args:
            database_name: Name of database to drop
        """
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
            safe_db_name = full_db_name.replace('"', '""')
            conn.execute(text(f'DROP DATABASE IF EXISTS "{safe_db_name}"'))
            logger.info(f"Dropped database: {full_db_name}")
        
        admin_engine.dispose()


# Compatibility for existing provisioning callers.
CDBImporter = cdb_import.CDBImporter
