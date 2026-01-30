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
    
    def __repr__(self):
        return f'<Role {self.name}>'


class Resource(MainDBModel):
    """Resource model - what can be accessed (riders, teams, stats, etc.)"""
    __tablename__ = 'resources'
    
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    app_section = db.Column(db.String(50), nullable=False)  # stat_manager, photo_manager, etc.
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
    __tablename__ = 'role_permissions'
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
    
    def __repr__(self):
        return f'<RolePermission {self.role.name} can {self.action.name} {self.resource.name}>'
