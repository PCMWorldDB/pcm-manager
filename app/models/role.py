"""Main database models - Role-Based Access Control (RBAC)"""
from app.extensions import db
from app.models.base import MainDBModel


class Role(MainDBModel):
    """Role model - defines user roles (admin, release_manager, stat_editor, viewer)"""
    __tablename__ = 'roles'
    
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    is_system_role = db.Column(db.Boolean, default=False, nullable=False)
    
    # Relationships
    permissions = db.relationship('RolePermission', back_populates='role', cascade='all, delete-orphan')
    
    @classmethod
    def get_roles_with_users_view(cls):
        """Get a joined view of roles with their assigned users"""
        from sqlalchemy import Table, MetaData, func
        
        metadata = MetaData()
        metadata.bind = db.engine
        user_role_mtm = Table('user_role_mtm', metadata, autoload_with=db.engine)
        
        # Import User here to avoid circular import
        from app.models.user import User
        
        return db.session.query(
            cls,
            User.username.label('user_username'),
            User.is_active.label('user_is_active'),
            user_role_mtm.c.assigned_at.label('assigned_at')
        ).outerjoin(user_role_mtm, cls.id == user_role_mtm.c.role_id
        ).outerjoin(User, user_role_mtm.c.user_id == User.id)
    
    def get_user_count(self):
        """Get count of users with this role"""
        from sqlalchemy import Table, MetaData, func
        
        metadata = MetaData()
        metadata.bind = db.engine
        user_role_mtm = Table('user_role_mtm', metadata, autoload_with=db.engine)
        
        count = db.session.query(func.count(user_role_mtm.c.user_id)).filter(
            user_role_mtm.c.role_id == self.id
        ).scalar()
        
        return count or 0
    
    def __repr__(self):
        return f'<Role {self.name}>'


class Resource(MainDBModel):
    """Resource model - what can be accessed (riders, teams, stats, etc.)"""
    __tablename__ = 'resources'
    
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    app_panel = db.Column(db.String(50), nullable=False)  # stat_manager, photo_manager, etc.
    description = db.Column(db.Text)
    
    # Relationships
    permissions = db.relationship('RolePermission', back_populates='resource')
    
    def __repr__(self):
        return f'<Resource {self.name}>'


class Action(MainDBModel):
    """Action model - what can be done (create, read, update, delete, etc.)"""
    __tablename__ = 'actions'
    
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    description = db.Column(db.Text)
    
    # Relationships
    permissions = db.relationship('RolePermission', back_populates='action')
    
    def __repr__(self):
        return f'<Action {self.name}>'


class RolePermission(MainDBModel):
    """RolePermission model - maps roles to resource-action combinations"""
    __tablename__ = 'role_permission_mtm'
    __table_args__ = (
        db.UniqueConstraint('role_id', 'resource_id', 'action_id', name='unique_role_resource_action'),
    )
    
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey('resources.id'), nullable=False)
    action_id = db.Column(db.Integer, db.ForeignKey('actions.id'), nullable=False)
    
    # Relationships
    role = db.relationship('Role', back_populates='permissions')
    resource = db.relationship('Resource', back_populates='permissions')
    action = db.relationship('Action', back_populates='permissions')
    
    @classmethod
    def get_role_permissions_view(cls):
        """Get a joined view of role permissions with role, resource, and action details"""
        return db.session.query(
            cls,
            Role.name.label('role_name'),
            Role.display_name.label('role_display_name'),
            Resource.name.label('resource_name'),
            Resource.app_panel.label('app_panel'),
            Action.name.label('action_name')
        ).join(Role, cls.role_id == Role.id
        ).join(Resource, cls.resource_id == Resource.id
        ).join(Action, cls.action_id == Action.id)
    
    @classmethod
    def get_permissions_for_role(cls, role_id):
        """Get all permissions for a specific role with joined details"""
        return cls.get_role_permissions_view().filter(cls.role_id == role_id).all()
    
    def __repr__(self):
        return f'<RolePermission {self.role.name} can {self.action.name} {self.resource.name}>'
