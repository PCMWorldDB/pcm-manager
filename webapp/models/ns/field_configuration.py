"""Per-namespace import and editing settings for object columns."""
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from webapp.models.ns.base import NamespaceBase


class FieldLevelConfiguration(NamespaceBase):
    __tablename__ = 'field_level_configuration'

    object = Column(String(50), primary_key=True)
    name = Column(String(100), primary_key=True)
    slug = Column(String(50), nullable=False)
    is_mapped_to_cdb = Column(Boolean, nullable=False)
    cdb_source = Column(String(255))
    cdb_source_import_policy = Column(String(20))
    cdb_source_import_null_handling = Column(String(20))
    is_editable = Column(Boolean, nullable=False)
    is_require_approval = Column(Boolean, nullable=False)

    __table_args__ = (
        CheckConstraint("""
            (is_mapped_to_cdb AND cdb_source IS NOT NULL
                AND cdb_source_import_policy IS NOT NULL
                AND cdb_source_import_policy IN ('identity', 'overwrite', 'preserve', 'reject')
                AND cdb_source_import_null_handling IS NOT NULL
                AND cdb_source_import_null_handling IN ('allow', 'ignore', 'reject'))
            OR (NOT is_mapped_to_cdb AND cdb_source IS NULL
                AND cdb_source_import_policy IS NULL AND cdb_source_import_null_handling IS NULL)
        """, name='ck_field_level_cdb_mapping'),
    )


class FieldConfigurationAudit(NamespaceBase):
    """Durable audit events committed together with namespace configuration.

    The main audit log is a projection. Undelivered events are retried when
    administrators open field settings or the audit log.
    """

    __tablename__ = 'field_configuration_audit'

    id = Column(BigInteger, primary_key=True)
    actor_id = Column(Integer)
    username = Column(String(100), nullable=False)
    details = Column(JSONB, nullable=False)
    ip_address = Column(String(45))
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    forwarded = Column(Boolean, nullable=False, server_default='false')
