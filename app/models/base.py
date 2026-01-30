"""Base model classes for main and namespace databases"""
from datetime import datetime
from app.extensions import db


class MainDBModel(db.Model):
    """Base model for main database tables (users, roles, namespaces, etc.)"""
    __abstract__ = True
    __bind_key__ = 'main'
    
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)


class NamespaceModel(db.Model):
    """Base model for namespace database tables (riders, teams, etc.)"""
    __abstract__ = True
    # __bind_key__ will be set dynamically based on current namespace context
    
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)


class TimestampMixin:
    """Mixin for timestamp fields"""
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)


class SoftDeleteMixin:
    """Mixin for soft delete functionality"""
    deleted_at = db.Column(db.DateTime, nullable=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    
    def soft_delete(self):
        """Mark record as deleted"""
        self.is_deleted = True
        self.deleted_at = datetime.utcnow()
    
    def restore(self):
        """Restore soft-deleted record"""
        self.is_deleted = False
        self.deleted_at = None
