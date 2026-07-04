"""Main database models - User management"""
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import current_app, g, has_request_context
from webapp.extensions import db, bcrypt, login_manager
from webapp.models.main.base import MainDBModel
from flask_login import UserMixin


TOKEN_INVITE = 'invite'
TOKEN_PASSWORD_RESET = 'password_reset'


class User(UserMixin, MainDBModel):
    """User model - stored in main database"""
    __tablename__ = 'users'
    
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    is_superadmin = db.Column(db.Boolean, default=False, nullable=False)
    is_temporary_password = db.Column(db.Boolean, default=False, nullable=False)
    last_login = db.Column(db.DateTime)
    failed_login_count = db.Column(db.Integer, default=0, nullable=False)
    login_locked_until = db.Column(db.DateTime)
    discord_user_id = db.Column(db.String(32), unique=True, nullable=True, index=True)
    
    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    
    def check_password(self, password):
        """Check if provided password matches hash"""
        if not self.password_hash:
            return False
        return bcrypt.check_password_hash(self.password_hash, password)

    def clear_login_failures(self):
        self.failed_login_count = 0
        self.login_locked_until = None

    def register_login_failure(self, now=None):
        now = now or datetime.utcnow()
        self.failed_login_count += 1
        threshold = current_app.config['AUTH_LOGIN_FAILURE_LIMIT']
        if self.failed_login_count >= threshold:
            lock_steps = max(1, self.failed_login_count - threshold + 1)
            seconds = min(
                current_app.config['AUTH_LOGIN_LOCK_MAX_SECONDS'],
                current_app.config['AUTH_LOGIN_LOCK_SECONDS'] * (2 ** (lock_steps - 1)),
            )
            self.login_locked_until = now + timedelta(seconds=seconds)

    def login_is_locked(self, now=None):
        return bool(self.login_locked_until and self.login_locked_until > (now or datetime.utcnow()))
    
    def _authorization_cache(self):
        """Return request-local role and permission sets for this user."""
        cache_key = f'_authorization_cache_user_{self.id}'
        if has_request_context() and hasattr(g, cache_key):
            return getattr(g, cache_key)

        rows = db.session.execute(
            db.text("""
                SELECT
                    roles.name AS role_name,
                    resources.name AS resource_name,
                    actions.name AS action_name
                FROM user_role_mtm
                JOIN roles ON roles.id = user_role_mtm.role_id
                LEFT JOIN role_permission_mtm
                    ON role_permission_mtm.role_id = roles.id
                LEFT JOIN resources
                    ON resources.id = role_permission_mtm.resource_id
                LEFT JOIN actions
                    ON actions.id = role_permission_mtm.action_id
                WHERE user_role_mtm.user_id = :user_id
            """),
            {'user_id': self.id},
        ).mappings()

        cache = {
            'roles': set(),
            'permissions': set(),
        }
        for row in rows:
            if row['role_name']:
                cache['roles'].add(row['role_name'])
            if row['resource_name'] and row['action_name']:
                cache['permissions'].add((row['resource_name'], row['action_name']))

        if has_request_context():
            setattr(g, cache_key, cache)
        return cache

    def has_permissions(self, *permissions):
        """Check if user has every requested resource/action permission."""
        if self.is_superadmin:
            return True

        permission_set = self._authorization_cache()['permissions']
        normalized_permissions = []
        for permission in permissions:
            if isinstance(permission, str):
                if ':' not in permission:
                    return False
                resource, action = permission.split(':', 1)
                normalized_permissions.append((resource, action))
            else:
                normalized_permissions.append(tuple(permission))

        return all(permission in permission_set for permission in normalized_permissions)
    
    def has_namespace_access(self, namespace_id):
        """Check if user can access a published namespace."""
        if self.is_superadmin:
            return True

        from webapp.models.main.namespace import Namespace

        namespace = Namespace.query.filter_by(
            id=namespace_id,
            status='active',
            is_deleted=False,
            is_template=False,
        ).first()
        return namespace is not None
    
    def has_permission(self, resource, action):
        """Check if user has permission for resource and action"""
        return self.has_permissions((resource, action))
    
    def __repr__(self):
        return f'<User {self.username}>'


class UserSecurityToken(MainDBModel):
    """Single-use user activation and password reset tokens."""

    __tablename__ = 'user_security_tokens'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    purpose = db.Column(db.String(40), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    issued_by_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id])
    issued_by = db.relationship('User', foreign_keys=[issued_by_id])

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @classmethod
    def issue(cls, user, purpose, expires_in, issued_by=None):
        raw_token = secrets.token_urlsafe(32)
        token = cls(
            user=user,
            purpose=purpose,
            token_hash=cls.hash_token(raw_token),
            expires_at=datetime.utcnow() + expires_in,
            issued_by=issued_by,
        )
        db.session.add(token)
        return raw_token, token

    @classmethod
    def find_valid(cls, raw_token, purpose, now=None):
        if not raw_token:
            return None
        now = now or datetime.utcnow()
        return cls.query.filter_by(
            token_hash=cls.hash_token(raw_token),
            purpose=purpose,
            consumed_at=None,
        ).filter(cls.expires_at > now).first()

    def consume(self, now=None):
        self.consumed_at = now or datetime.utcnow()


# Define association tables AFTER the User class
# These will be created by raw SQL to avoid SQLAlchemy foreign key resolution issues

def create_association_tables():
    """Create association tables using raw SQL to avoid FK resolution issues"""
    from webapp.extensions import db
    
    # Create user_role_mtm table
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS user_role_mtm (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            assigned_by INTEGER,
            UNIQUE (user_id, role_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
            FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    
    db.session.commit()


def configure_user_relationships():
    """Configure User relationships after all tables exist - reflect tables from DB
    
    This function is safe to call at app startup - if the MTM tables don't exist yet,
    it will silently skip configuration. The relationships will be configured when
    init-db is run.
    """
    from sqlalchemy import Table, MetaData, inspect
    from webapp.models.main.role import Role
    
    # Check if the MTM tables exist before trying to reflect them
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    
    if 'user_role_mtm' not in existing_tables:
        # Tables don't exist yet - skip configuration
        # This is expected on first app startup before init-db is run
        return
    
    # Reflect the association tables from the database
    metadata = MetaData()
    metadata.bind = db.engine
    
    user_role_mtm_table = Table('user_role_mtm', metadata, autoload_with=db.engine)
    
    # Configure relationships with explicit join conditions
    User.roles = db.relationship(
        'Role',
        secondary=user_role_mtm_table,
        primaryjoin=(User.id == user_role_mtm_table.c.user_id),
        secondaryjoin=(Role.id == user_role_mtm_table.c.role_id),
        backref=db.backref('users', lazy='dynamic'),
        lazy='dynamic'
    )


@login_manager.user_loader
def load_user(id):
    """Load user by ID for Flask-Login"""
    return User.query.get(int(id))
