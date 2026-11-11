"""Namespace database schema models

These models define the structure of tables in each namespace database.
Each namespace gets its own PostgreSQL database with this schema.
"""
from sqlalchemy import Column, Integer, String, Text, Date, Boolean, ForeignKey, Float, DateTime, BigInteger, UniqueConstraint, Index, CheckConstraint
from webapp.models.ns.base import NamespaceBase
from webapp.models.ns.field_configuration import FieldConfigurationAudit, FieldLevelConfiguration
from webapp.models.ns.cdb import CDB_HISTORY_TABLES, CDB_TABLES
from sqlalchemy.orm import relationship

from webapp.integrations.cdb import (
    get_cdb_mappings,
    get_cdb_table_name,
    get_field_mapping,
)


class StatChange(NamespaceBase):
    """Stat edits: race result, list, or individual (both source IDs are NULL)."""

    __tablename__ = 'stat_changes'

    id = Column(Integer, primary_key=True)
    race_result_id = Column(Integer, nullable=True, index=True)
    stat_list_id = Column(Integer, nullable=True, index=True)
    release_id = Column(Integer, nullable=True, index=True)
    rider_id = Column(Integer, nullable=False, index=True)
    stat_name = Column(String(20), nullable=False, index=True)
    original_value = Column(Integer, nullable=False)
    final_value = Column(Integer, nullable=False)
    delta_value = Column(Integer, nullable=False)
    is_confilict_override = Column(Boolean, nullable=False, default=False)
    is_approved = Column(Boolean, nullable=False, default=False)
    is_applied_immediately = Column(Boolean, nullable=False, server_default='false')

    __table_args__ = (
        UniqueConstraint(
            'race_result_id',
            'stat_list_id',
            'rider_id',
            'stat_name',
            name='uq_stat_changes_scope',
        ),
        Index(
            'idx_stat_changes_race_result_rider',
            'race_result_id',
            'rider_id',
        ),
        Index('idx_stat_changes_list_rider', 'stat_list_id', 'rider_id'),
        Index('idx_stat_changes_release', 'release_id'),
    )

    def __repr__(self):
        return (
            '<StatChange '
            f'race_result={self.race_result_id} '
            f'list={self.stat_list_id} '
            f'release={self.release_id} '
            f'rider={self.rider_id} '
            f'stat={self.stat_name} '
            f'delta={self.delta_value}>'
        )


class StatChangeSnapshot(NamespaceBase):
    """CDB baseline per source; NULL source IDs identify an individual rider scope."""

    __tablename__ = 'stat_change_snapshot'

    id = Column(Integer, primary_key=True)
    race_result_id = Column(Integer, nullable=True, index=True)
    stat_list_id = Column(Integer, nullable=True, index=True)
    rider_id = Column(Integer, nullable=False, index=True)
    stat_mo = Column(Integer, nullable=False)
    stat_mm = Column(Integer, nullable=False)
    stat_dh = Column(Integer, nullable=False)
    stat_hil = Column(Integer, nullable=False)
    stat_tt = Column(Integer, nullable=False)
    stat_prl = Column(Integer, nullable=False)
    stat_fla = Column(Integer, nullable=False)
    stat_spr = Column(Integer, nullable=False)
    stat_acc = Column(Integer, nullable=False)
    stat_end = Column(Integer, nullable=False)
    stat_res = Column(Integer, nullable=False)
    stat_rec = Column(Integer, nullable=False)
    stat_cob = Column(Integer, nullable=False)
    stat_att = Column(Integer, nullable=False)
    discord_thread_id = Column(String(64), nullable=True)
    discord_thread_url = Column(String(500), nullable=True)
    discord_notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint(
            'race_result_id',
            'stat_list_id',
            'rider_id',
            name='uq_stat_change_snapshot_scope_rider',
        ),
        Index(
            'idx_stat_change_snapshot_race_result_rider',
            'race_result_id',
            'rider_id',
        ),
        Index('idx_stat_change_snapshot_list_rider', 'stat_list_id', 'rider_id'),
    )

    def __repr__(self):
        return (
            '<StatChangeSnapshot '
            f'race_result={self.race_result_id} '
            f'list={self.stat_list_id} '
            f'rider={self.rider_id}>'
        )


class Release(NamespaceBase):
    """Namespace-scoped release record."""

    __tablename__ = 'releases'

    id = Column(Integer, primary_key=True)
    version = Column(String(50), nullable=False, index=True)
    release_name = Column(String(200))
    release_type = Column(String(50), nullable=False, default='stats')
    status = Column(String(20), nullable=False, default='draft')
    release_notes = Column(Text)
    include_stats_editor_changes = Column(Boolean, nullable=False, default=False)
    include_palmares_changes = Column(Boolean, nullable=False, default=False)
    include_photo_changes = Column(Boolean, nullable=False, default=False)
    created_by_id = Column(Integer)
    published_by_id = Column(Integer)
    published_at = Column(DateTime)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint('version', name='uq_releases_version'),
    )


class ReleaseFile(NamespaceBase):
    """Namespace-scoped release file records."""

    __tablename__ = 'release_files'

    id = Column(Integer, primary_key=True)
    release_id = Column(Integer, nullable=False, index=True)
    file_type = Column(String(50), nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    relative_path = Column(String(500), nullable=False)
    content_type = Column(String(120))
    file_size_bytes = Column(BigInteger)
    md5_checksum = Column(String(32))
    sha256_checksum = Column(String(64))
    created_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint('release_id', 'file_type', name='uq_release_files_release_file_type'),
    )


class RiderList(NamespaceBase):
    """Metadata for an ad-hoc rider stat or attribute list."""

    __tablename__ = 'rider_list'

    id = Column(Integer, primary_key=True)
    list_name = Column(String(120), nullable=False)
    list_type = Column(String(20), nullable=False, server_default='stat')
    season_year = Column(Integer, nullable=False, index=True)
    gender = Column(String(20), nullable=False, index=True)
    created_by_id = Column(Integer, nullable=True)
    created_at = Column(DateTime)

    __table_args__ = (CheckConstraint("list_type IN ('stat', 'attribute')", name='ck_stat_lists_list_type'),)


class RiderListItem(NamespaceBase):
    """Riders assigned to a stat or attribute list."""

    __tablename__ = 'rider_list_item'

    id = Column(Integer, primary_key=True)
    rider_list_id = Column(Integer, nullable=False, index=True)
    rider_id = Column(Integer, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('rider_list_id', 'rider_id', name='uq_rider_list_item_list_rider'),
    )


# List of all tables to create (in order)
NAMESPACE_TABLES = [
    *CDB_TABLES,
    FieldLevelConfiguration,
    FieldConfigurationAudit,
    Release,
    *CDB_HISTORY_TABLES,
    ReleaseFile,
    StatChange,
    StatChangeSnapshot,
    RiderList,
    RiderListItem,
]


def get_table_names():
    """Get list of all table names in namespace schema"""
    return [table.__tablename__ for table in NAMESPACE_TABLES]


def get_table_by_name(name):
    """Get table model by name"""
    for table in NAMESPACE_TABLES:
        if table.__tablename__ == name:
            return table
    return None
