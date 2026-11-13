"""Base model classes for main and namespace databases"""
from datetime import datetime
from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import ARRAY
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
    STAT_LIST_SUBMITTED = 'stat_list_submitted'
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
    FIELD_CONFIGURATION_UPDATED = 'field_configuration_updated'
    FIELD_PERMISSION_UPDATED = 'field_permission_updated'
    NAMESPACE_DELETED = 'namespace_deleted'
    NAMESPACE_PROVISIONED = 'namespace_provisioned'
    NAMESPACE_CDB_IMPORTED = 'namespace_cdb_imported'
    NAMESPACE_STATUS_CHANGED = 'namespace_status_changed'


class ScheduledJob(MainDBModel):
    """Named, independently scheduled external data job."""

    __tablename__ = 'scheduled_jobs'

    name = db.Column(db.String(120), nullable=False)
    job_type = db.Column(db.String(32), nullable=False)
    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id'), index=True)
    namespace = db.relationship('Namespace')
    params = db.Column(db.JSON, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    run_at = db.Column(db.Time, nullable=False)
    last_run_on = db.Column(db.Date)

    __table_args__ = (
        db.CheckConstraint(
            "(job_type = 'race_results' AND namespace_id IS NULL) OR "
            "(job_type IN ('rider_identity_check', 'rider_activity_check') AND namespace_id IS NOT NULL)",
            name='ck_scheduled_job_scope',
        ),
    )


class ExternalCallTaskGroup(MainDBModel):
    """Provider-neutral request group, including legacy synchronization history."""
    __tablename__ = 'external_call_task_group'

    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id', ondelete='SET NULL'),
                             nullable=True, index=True)
    namespace = db.relationship('Namespace', foreign_keys=[namespace_id])
    scheduled_job_id = db.Column(db.Integer, db.ForeignKey('scheduled_jobs.id', ondelete='SET NULL'), index=True)
    scheduled_job = db.relationship('ScheduledJob')

    # Legacy provider metadata; mixed groups have no single provider.
    provider = db.Column(db.String(50), nullable=True, index=True)
    workflow_type = db.Column(db.String(80), nullable=True, index=True)
    sync_types = db.Column(ARRAY(db.String(320)), nullable=False, default=list, server_default='{}')
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    season_year = db.Column(db.Integer, nullable=True, index=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='success', index=True)
    priority = db.Column(db.String(4), nullable=False, default='high', server_default='high')
    origin = db.Column(db.String(9), nullable=False, default='manual', server_default='manual')
    is_worker_queued = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    worker_message_id = db.Column(db.String(36), nullable=True)
    worker_queued_at = db.Column(db.DateTime, nullable=True)
    requested_count = db.Column(db.Integer, nullable=True)
    synced_count = db.Column(db.Integer, nullable=True)
    failed_count = db.Column(db.Integer, nullable=True)
    executed_by_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    message = db.Column(db.String(255), nullable=True)
    details_json = db.Column(db.Text, nullable=True)

    executed_by = db.relationship('User', foreign_keys=[executed_by_id])

    __table_args__ = (
        db.CheckConstraint("priority IN ('high', 'low')", name='ck_external_group_priority'),
        db.CheckConstraint("origin IN ('manual', 'scheduled')", name='ck_external_group_origin'),
        Index('ix_external_group_dispatch', 'status', 'is_worker_queued', 'priority', 'timestamp', 'id'),
        Index('ix_external_group_sync_types', 'sync_types', postgresql_using='gin'),
    )

    def __repr__(self):
        return (
            f'<ExternalCallTaskGroup namespace_id={self.namespace_id} '
            f'status={self.status} timestamp={self.timestamp}>'
        )


class ExternalCallTask(MainDBModel):
    """One provider request, or a preserved historical API call audit row."""

    __tablename__ = 'external_call_task'

    task_group_id = db.Column(
        db.Integer,
        db.ForeignKey('external_call_task_group.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    provider = db.Column(db.String(50), nullable=False, index=True)
    endpoint = db.Column(db.String(255), nullable=False, index=True)
    request_url = db.Column(db.Text, nullable=True)
    request_params_json = db.Column(db.Text, nullable=True)
    # Informational record reference; namespace context comes from task_group.
    # No FK: the associated record may live in a separate namespace database.
    related_to_id = db.Column(db.Integer, nullable=True)
    related_to_table = db.Column(db.String(100), nullable=True)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    max_attempts = db.Column(db.Integer, nullable=False, default=1)
    outcome = db.Column(db.String(40), nullable=True, index=True)
    # Audit rows remain historical; only explicitly queued rows are executable.
    status = db.Column(db.String(20), nullable=False, default='historical', index=True)
    available_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    result_json = db.Column(db.Text)
    http_status = db.Column(db.Integer, nullable=True, index=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    response_bytes = db.Column(db.Integer, nullable=True)
    error_type = db.Column(db.String(120), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    task_group = db.relationship('ExternalCallTaskGroup', foreign_keys=[task_group_id])

    @property
    def sync_type(self):
        return f'{self.provider}.{self.endpoint}'

    __table_args__ = (
        Index('ix_external_call_task_related_record',
              'task_group_id', 'related_to_table', 'related_to_id'),
    )

    def __repr__(self):
        return (
            f'<ExternalCallTask provider={self.provider} endpoint={self.endpoint} '
            f'outcome={self.outcome} http_status={self.http_status}>'
        )
