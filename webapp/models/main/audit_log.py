"""Audit log model for tracking user actions"""
from webapp.extensions import db
from webapp.models.main.base import MainDBModel
from sqlalchemy import Index


class AuditLog(MainDBModel):
    """Audit log for tracking user actions and system events"""
    __tablename__ = 'audit_logs'
    
    # Who performed the action
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    username = db.Column(db.String(80), nullable=False)  # Denormalized for historical record
    
    # What action was performed
    action = db.Column(db.String(100), nullable=False, index=True)
    
    # Which resource was affected (optional)
    resource_type = db.Column(db.String(50))  # 'namespace', 'user', 'role', etc.
    resource_id = db.Column(db.Integer)
    resource_name = db.Column(db.String(200))  # Denormalized name for easy reference
    
    # Additional details (JSON)
    details = db.Column(db.JSON)
    
    # Request metadata
    ip_address = db.Column(db.String(45))  # IPv6 compatible
    user_agent = db.Column(db.String(500))
    
    # Relationships
    user = db.relationship('User', foreign_keys=[user_id])
    
    # Indexes for common queries
    __table_args__ = (
        Index('ix_audit_logs_user_action', 'user_id', 'action'),
        Index('ix_audit_logs_resource', 'resource_type', 'resource_id'),
        Index('ix_audit_logs_created', 'created_at'),
    )
    
    def __repr__(self):
        return f'<AuditLog {self.username}: {self.action}>'
    
    @classmethod
    def log_action(cls, action, user, resource_type=None, resource_id=None, 
                   resource_name=None, details=None, ip_address=None, user_agent=None):
        """Helper method to create audit log entries
        
        Args:
            action: Action being performed (e.g., 'user_login', 'namespace_created')
            user: User object performing the action
            resource_type: Type of resource affected (optional)
            resource_id: ID of resource affected (optional)
            resource_name: Name of resource affected (optional)
            details: Additional details as dict (optional)
            ip_address: IP address of request (optional)
            user_agent: User agent string (optional)
        """
        log_entry = cls(
            user_id=user.id if user else None,
            username=user.username if user else 'system',
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            details=details or {},
            ip_address=ip_address,
            user_agent=user_agent
        )
        db.session.add(log_entry)
        return log_entry
    
    @classmethod
    def get_user_activity(cls, user_id, limit=50):
        """Get recent activity for a specific user"""
        return cls.query.filter_by(user_id=user_id).order_by(cls.created_at.desc()).limit(limit).all()
    
    @classmethod
    def get_resource_history(cls, resource_type, resource_id, limit=50):
        """Get audit history for a specific resource"""
        return cls.query.filter_by(
            resource_type=resource_type,
            resource_id=resource_id
        ).order_by(cls.created_at.desc()).limit(limit).all()


# Action constants for consistency
class AuditAction:
    """Constants for audit log actions"""
    # Authentication
    USER_LOGIN = 'user_login'
    USER_LOGOUT = 'user_logout'
    USER_LOGIN_FAILED = 'user_login_failed'
    
    # User management
    USER_CREATED = 'user_created'
    USER_ACTIVATED = 'user_activated'
    USER_DEACTIVATED = 'user_deactivated'
    USER_PASSWORD_CHANGED = 'user_password_changed'
    USER_PASSWORD_RESET = 'user_password_reset'
    
    # Role and permission management
    USER_ROLE_ADDED = 'user_role_added'
    USER_ROLE_REMOVED = 'user_role_removed'
    USER_NAMESPACE_ACCESS_GRANTED = 'user_namespace_access_granted'
    USER_NAMESPACE_ACCESS_REVOKED = 'user_namespace_access_revoked'
    
    # Namespace management
    NAMESPACE_CREATED = 'namespace_created'
    NAMESPACE_UPDATED = 'namespace_updated'
    NAMESPACE_DELETED = 'namespace_deleted'
    NAMESPACE_PROVISIONED = 'namespace_provisioned'
    NAMESPACE_CDB_IMPORTED = 'namespace_cdb_imported'
    NAMESPACE_STATUS_CHANGED = 'namespace_status_changed'
