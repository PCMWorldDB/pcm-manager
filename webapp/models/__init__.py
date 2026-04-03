"""Models package - exports all models"""
from webapp.models.main.base import MainDBModel, NamespaceModel, TimestampMixin, SoftDeleteMixin
from webapp.models.main.role import Role, Resource, Action, RolePermission
from webapp.models.main.namespace import Namespace
from webapp.models.main.user import User
from webapp.models.main.release import Release
from webapp.models.main.audit_log import AuditLog, AuditAction
from webapp.models.main.firstcycling import (
    FirstCyclingRace,
    FirstCyclingRaceStage,
    FirstCyclingRaceResult,
    FirstCyclingTeam,
    FirstCyclingRider,
)


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
    'AuditLog',
    'AuditAction',
    'FirstCyclingRace',
    'FirstCyclingRaceStage',
    'FirstCyclingRaceResult',
    'FirstCyclingTeam',
    'FirstCyclingRider',
]
