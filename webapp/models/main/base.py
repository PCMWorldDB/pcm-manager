"""Base model classes for main and namespace databases"""
from datetime import datetime
from sqlalchemy import Index
from webapp.extensions import db


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


class AuditLog(MainDBModel):
    """Audit log for tracking user actions and system events."""
    __tablename__ = 'audit_logs'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    username = db.Column(db.String(80), nullable=False)
    action = db.Column(db.String(100), nullable=False, index=True)
    resource_type = db.Column(db.String(50))
    resource_id = db.Column(db.Integer)
    resource_name = db.Column(db.String(200))
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))

    user = db.relationship('User', foreign_keys=[user_id])

    __table_args__ = (
        Index('ix_audit_logs_user_action', 'user_id', 'action'),
        Index('ix_audit_logs_resource', 'resource_type', 'resource_id'),
        Index('ix_audit_logs_created', 'created_at'),
    )

    def __repr__(self):
        return f'<AuditLog {self.username}: {self.action}>'

    @classmethod
    def log_action(
        cls,
        action,
        user,
        resource_type=None,
        resource_id=None,
        resource_name=None,
        details=None,
        ip_address=None,
        user_agent=None,
    ):
        """Helper method to create audit log entries."""
        log_entry = cls(
            user_id=user.id if user else None,
            username=user.username if user else 'system',
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            details=details or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.add(log_entry)
        return log_entry

    @classmethod
    def get_user_activity(cls, user_id, limit=50):
        """Get recent activity for a specific user."""
        return cls.query.filter_by(user_id=user_id).order_by(cls.created_at.desc()).limit(limit).all()

    @classmethod
    def get_resource_history(cls, resource_type, resource_id, limit=50):
        """Get audit history for a specific resource."""
        return cls.query.filter_by(
            resource_type=resource_type,
            resource_id=resource_id,
        ).order_by(cls.created_at.desc()).limit(limit).all()


class AuditAction:
    """Constants for audit log actions."""
    RACE_STATS_SAVED = 'race_stats_saved'
    RACE_STATS_SUBMITTED = 'race_stats_submitted'
    RACE_STATS_APPROVED = 'race_stats_approved'
    RELEASE_CREATED = 'release_created'
    RELEASE_STATUS_CHANGED = 'release_status_changed'
    USER_LOGIN = 'user_login'
    USER_LOGOUT = 'user_logout'
    USER_LOGIN_FAILED = 'user_login_failed'
    USER_LOGIN_LOCKED = 'user_login_locked'
    USER_CREATED = 'user_created'
    USER_ACTIVATED = 'user_activated'
    USER_DEACTIVATED = 'user_deactivated'
    USER_INVITE_CREATED = 'user_invite_created'
    USER_PASSWORD_CHANGED = 'user_password_changed'
    USER_PASSWORD_RESET = 'user_password_reset'
    USER_DISCORD_LINKED = 'user_discord_linked'
    USER_DISCORD_UNLINKED = 'user_discord_unlinked'
    USER_ROLE_ADDED = 'user_role_added'
    USER_ROLE_REMOVED = 'user_role_removed'
    NAMESPACE_CREATED = 'namespace_created'
    NAMESPACE_UPDATED = 'namespace_updated'
    NAMESPACE_DELETED = 'namespace_deleted'
    NAMESPACE_PROVISIONED = 'namespace_provisioned'
    NAMESPACE_CDB_IMPORTED = 'namespace_cdb_imported'
    NAMESPACE_STATUS_CHANGED = 'namespace_status_changed'


class ExternalSyncLog(MainDBModel):
    """Audit log for external provider synchronization jobs."""
    __tablename__ = 'external_sync_log'

    provider = db.Column(db.String(50), nullable=False, index=True)
    sync_type = db.Column(db.String(80), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    season_year = db.Column(db.Integer, nullable=True, index=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='success', index=True)
    requested_count = db.Column(db.Integer, nullable=True)
    synced_count = db.Column(db.Integer, nullable=True)
    failed_count = db.Column(db.Integer, nullable=True)
    executed_by_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    message = db.Column(db.String(255), nullable=True)
    details_json = db.Column(db.Text, nullable=True)

    executed_by = db.relationship('User', foreign_keys=[executed_by_id])

    def __repr__(self):
        return (
            f'<ExternalSyncLog provider={self.provider} sync_type={self.sync_type} '
            f'status={self.status} timestamp={self.timestamp}>'
        )


class ExternalSyncAPICallLog(MainDBModel):
    """Per-request debug log for external provider API calls."""

    __tablename__ = 'external_sync_api_call_log'

    external_sync_log_id = db.Column(
        db.Integer,
        db.ForeignKey('external_sync_log.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    provider = db.Column(db.String(50), nullable=False, index=True)
    endpoint = db.Column(db.String(255), nullable=False, index=True)
    request_url = db.Column(db.Text, nullable=False)
    request_params_json = db.Column(db.Text, nullable=True)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    max_attempts = db.Column(db.Integer, nullable=False, default=1)
    outcome = db.Column(db.String(40), nullable=False, index=True)
    http_status = db.Column(db.Integer, nullable=True, index=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    response_bytes = db.Column(db.Integer, nullable=True)
    error_type = db.Column(db.String(120), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    external_sync_log = db.relationship('ExternalSyncLog', foreign_keys=[external_sync_log_id])

    def __repr__(self):
        return (
            f'<ExternalSyncAPICallLog provider={self.provider} endpoint={self.endpoint} '
            f'outcome={self.outcome} http_status={self.http_status}>'
        )
