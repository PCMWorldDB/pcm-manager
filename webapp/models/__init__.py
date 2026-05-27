"""Models package - exports all models"""
from webapp.models.main.base import (
    MainDBModel,
    NamespaceModel,
    TimestampMixin,
    SoftDeleteMixin,
    AuditLog,
    AuditAction,
    ExternalSyncLog,
    ExternalSyncAPICallLog,
)
from webapp.models.main.role import Role, Resource, Action, RolePermission
from webapp.models.main.namespace import Namespace
from webapp.models.main.user import User, UserSecurityToken
from webapp.models.main.discord_settings import DiscordSettings, DiscordWorkflowThread, DISCORD_WORKFLOW_CHOICES
from webapp.models.main.firstcycling import (
    FirstCyclingRace,
    FirstCyclingRaceStage,
    FirstCyclingRaceResult,
    FirstCyclingTeam,
    FirstCyclingRider,
)
from webapp.models.main.pcs import PcsRaceResult


# Association tables and relationship configuration are handled in CLI after db.create_all()


__all__ = [
    'MainDBModel',
    'NamespaceModel',
    'TimestampMixin',
    'SoftDeleteMixin',
    'ExternalSyncLog',
    'ExternalSyncAPICallLog',
    'Role',
    'Resource',
    'Action',
    'RolePermission',
    'Namespace',
    'User',
    'UserSecurityToken',
    'DiscordSettings',
    'DiscordWorkflowThread',
    'DISCORD_WORKFLOW_CHOICES',
    'AuditLog',
    'AuditAction',
    'FirstCyclingRace',
    'FirstCyclingRaceStage',
    'FirstCyclingRaceResult',
    'FirstCyclingTeam',
    'FirstCyclingRider',
    'PcsRaceResult',
]
