"""Main database models - User management"""
from app.extensions import db, bcrypt, login_manager
from app.models.base import MainDBModel
from flask_login import UserMixin


class User(UserMixin, MainDBModel):
    """User model - stored in main database"""
    __tablename__ = 'users'
    
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_superadmin = db.Column(db.Boolean, default=False, nullable=False)
    last_login = db.Column(db.DateTime)
    
    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    
    def check_password(self, password):
        """Check if provided password matches hash"""
        return bcrypt.check_password_hash(self.password_hash, password)
    
    def has_role(self, role_name):
        """Check if user has specific role"""
        return self.roles.filter_by(name=role_name).first() is not None
    
    def has_namespace_access(self, namespace_id):
        """Check if user can access specific namespace"""
        if self.is_superadmin:
            return True
        return self.accessible_namespaces.filter_by(id=namespace_id).first() is not None
    
    def has_permission(self, resource, action):
        """Check if user has permission for resource and action"""
        if self.is_superadmin:
            return True
        
        for role in self.roles:
            for permission in role.permissions:
                if permission.resource.name == resource and permission.action.name == action:
                    return True
        return False
    
    def __repr__(self):
        return f'<User {self.username}>'


# Define association tables AFTER the User class
# These will be created by raw SQL to avoid SQLAlchemy foreign key resolution issues

def create_association_tables():
    """Create association tables using raw SQL to avoid FK resolution issues"""
    from app.extensions import db
    
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
    
    # Create user_namespace_mtm table
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS user_namespace_mtm (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            namespace_id INTEGER NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            granted_by INTEGER,
            UNIQUE (user_id, namespace_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (namespace_id) REFERENCES namespaces(id) ON DELETE CASCADE,
            FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL
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
    from app.models.role import Role
    from app.models.namespace import Namespace
    
    # Check if the MTM tables exist before trying to reflect them
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()
    
    if 'user_role_mtm' not in existing_tables or 'user_namespace_mtm' not in existing_tables:
        # Tables don't exist yet - skip configuration
        # This is expected on first app startup before init-db is run
        return
    
    # Reflect the association tables from the database
    metadata = MetaData()
    metadata.bind = db.engine
    
    user_role_mtm_table = Table('user_role_mtm', metadata, autoload_with=db.engine)
    user_namespace_mtm_table = Table('user_namespace_mtm', metadata, autoload_with=db.engine)
    
    # Configure relationships with explicit join conditions
    User.roles = db.relationship(
        'Role',
        secondary=user_role_mtm_table,
        primaryjoin=(User.id == user_role_mtm_table.c.user_id),
        secondaryjoin=(Role.id == user_role_mtm_table.c.role_id),
        backref='users',
        lazy='dynamic'
    )
    
    User.accessible_namespaces = db.relationship(
        'Namespace',
        secondary=user_namespace_mtm_table,
        primaryjoin=(User.id == user_namespace_mtm_table.c.user_id),
        secondaryjoin=(Namespace.id == user_namespace_mtm_table.c.namespace_id),
        backref='authorized_users',
        lazy='dynamic'
    )


@login_manager.user_loader
def load_user(id):
    """Load user by ID for Flask-Login"""
    return User.query.get(int(id))
