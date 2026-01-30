"""Main database model - CDB Namespace registry"""
from app.extensions import db
from app.models.base import MainDBModel, SoftDeleteMixin
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
    
    # Encrypted connection string
    connection_string = db.Column(
        EncryptedType(db.String, 
                     os.getenv('SECRET_KEY', os.getenv('SECRET_KEY', 'dev-secret-key-change-this')),
                     AesEngine,
                     'pkcs5'),
        nullable=False
    )
    
    # SQLite file info
    sqlite_file_path = db.Column(db.String(500))  # Path to current working SQLite file
    
    # Metadata
    pcm_version = db.Column(db.String(20))  # e.g., 'PCM2024', 'PCM2023'
    database_year = db.Column(db.Integer)  # e.g., 2024
    
    # Status
    status = db.Column(db.String(20), default='active', nullable=False)  # active, maintenance, archived
    is_template = db.Column(db.Boolean, default=False, nullable=False)
    
    # Audit
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Relationships
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    releases = db.relationship('Release', back_populates='namespace', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Namespace {self.name}>'
    
    @property
    def is_active(self):
        """Check if namespace is active"""
        return self.status == 'active' and not self.is_deleted
    
    def get_database_uri(self):
        """Get the decrypted database connection string"""
        return self.connection_string
