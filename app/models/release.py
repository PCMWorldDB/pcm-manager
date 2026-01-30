"""Main database model - Release tracking"""
from app.extensions import db
from app.models.base import MainDBModel


class Release(MainDBModel):
    """Release model - tracks exported CDB versions"""
    __tablename__ = 'releases'
    
    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id'), nullable=False)
    
    # Release info
    version = db.Column(db.String(50), nullable=False)  # e.g., '1.0.0', '2024.1'
    release_name = db.Column(db.String(200))
    release_type = db.Column(db.String(20), default='minor')  # major, minor, patch, beta
    
    # File info
    sqlite_file_path = db.Column(db.String(500), nullable=False)
    file_size_bytes = db.Column(db.BigInteger)
    md5_checksum = db.Column(db.String(32))
    sha256_checksum = db.Column(db.String(64))
    
    # Status
    status = db.Column(db.String(20), default='draft', nullable=False)  # draft, published, archived
    release_notes = db.Column(db.Text)
    
    # Metrics
    download_count = db.Column(db.Integer, default=0, nullable=False)
    
    # Audit
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    published_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    namespace = db.relationship('Namespace', back_populates='releases')
    created_by = db.relationship('User', foreign_keys=[created_by_id])
    published_by = db.relationship('User', foreign_keys=[published_by_id])
    
    # Unique constraint: one version per namespace
    __table_args__ = (
        db.UniqueConstraint('namespace_id', 'version', name='unique_namespace_version'),
    )
    
    def __repr__(self):
        return f'<Release {self.namespace.name} v{self.version}>'
    
    def increment_download(self):
        """Increment download counter"""
        self.download_count += 1
        db.session.commit()
