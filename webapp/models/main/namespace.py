"""Main database model - CDB Namespace registry"""
from webapp.extensions import db
from webapp.models.main.base import MainDBModel, SoftDeleteMixin
from sqlalchemy_utils import EncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
import os


class Namespace(MainDBModel, SoftDeleteMixin):
    """Namespace model - registry of all Namespace instances"""
    __tablename__ = 'namespaces'
    
    # User-friendly name for the namespace
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    description = db.Column(db.Text)
    
    # Database connection info
    database_name = db.Column(db.String(100), unique=True, nullable=False)
    
    # SQLite file info
    sqlite_file_path = db.Column(db.String(500))  # Path to current working SQLite file
    
    # Metadata
    pcm_version = db.Column(db.String(20))  # e.g., 'PCM2024', 'PCM2023'
    database_year = db.Column(db.Integer)  # e.g., 2024
    gender = db.Column(db.String(20), nullable=False, default='Men')
    
    # Status
    status = db.Column(db.String(20), default='active', nullable=False)  # active, maintenance, archived
    is_template = db.Column(db.Boolean, default=False, nullable=False)
    
    # Audit
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Relationships
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    
    def __repr__(self):
        return f'<Namespace {self.name}>'
    
    @property
    def is_active(self):
        """Check if namespace is active"""
        return self.status == 'active' and not self.is_deleted
    
    def build_connection_string(self):
        """Build connection string dynamically for namespace database"""
        base_url = os.getenv('PROVISIONER_DATABASE_URL')
        if not base_url:
            raise ValueError('PROVISIONER_DATABASE_URL environment variable not set')
        
        # Parse the base URL and replace database name
        # Format: postgresql://user:pass@host:port/dbname
        if '://' in base_url:
            protocol_part, rest = base_url.split('://', 1)
            if '@' in rest:
                credentials, host_db = rest.rsplit('@', 1)
                host_part = host_db.split('/')[0]
                return f"{protocol_part}://{credentials}@{host_part}/{self.database_name}"
        
        raise ValueError(f"Invalid database URL format: {base_url}")
