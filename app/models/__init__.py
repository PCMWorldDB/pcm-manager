"""Models package - exports all models"""
from app.models.base import MainDBModel, NamespaceModel, TimestampMixin, SoftDeleteMixin
from app.models.role import Role, Resource, Action, RolePermission
from app.models.namespace import Namespace
from app.models.user import User
from app.models.release import Release

# Association tables and relationship configuration are handled in CLI after db.create_all()


__all__ = [
    'MainDBModel',
    'NamespaceModel',
    'TimestampMixin',
    'SoftDeleteMixin',
    'Role',
    'Resource',
    'Action',
    'RolePermission',
    'Namespace',
    'User',
    'Release',
]
