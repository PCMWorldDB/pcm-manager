"""Compatibility re-exports for audit log symbols.

AuditLog and AuditAction now live in webapp.models.main.base.
"""

from webapp.models.main.base import AuditLog, AuditAction

__all__ = ['AuditLog', 'AuditAction']
