"""Transactional contributions to main's provider-only identity maps."""
from pathlib import Path

from sqlalchemy import Boolean, Column, DateTime, DDL, Integer, String, Text, UniqueConstraint, CheckConstraint, event, func

from webapp.models.ns.base import NamespaceBase


class IdentitySyncOutbox(NamespaceBase):
    __tablename__ = 'identity_sync_outbox'

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(10), nullable=False)
    firstcycling_id = Column(Integer, nullable=False)
    pcs_slug = Column(String(500), nullable=False)
    is_seed = Column(Boolean, nullable=False, server_default='false')
    status = Column(String(12), nullable=False, server_default='pending', index=True)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('entity_type', 'firstcycling_id', 'pcs_slug', name='uq_identity_sync_pair'),
        CheckConstraint("entity_type IN ('race', 'rider')", name='ck_identity_sync_entity'),
        CheckConstraint("status IN ('pending', 'published', 'conflict', 'superseded')", name='ck_identity_sync_status'),
        CheckConstraint('firstcycling_id > 0', name='ck_identity_sync_positive'),
    )


def register_identity_trigger(table):
    sql = (Path(__file__).resolve().parents[2] / 'namespace' / 'identity_sync_function.sql').read_text(encoding='utf-8')
    event.listen(table, 'after_create', DDL(sql.replace('%', '%%')).execute_if(dialect='postgresql'))
    sql = (f'CREATE TRIGGER queue_external_identity_pair AFTER INSERT OR UPDATE OF '
           f'first_cycling_id, pcs_slug, is_deleted' +
           (', pcs_slug_is_assumed' if table.name == 'rider' else '') +
           f' ON {table.name} FOR EACH ROW EXECUTE FUNCTION queue_external_identity_pair()')
    event.listen(table, 'after_create', DDL(sql).execute_if(dialect='postgresql'))
