"""Namespace CDB objects and their shared change history."""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, false, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from webapp.models.ns.base import NamespaceBase


class CDBRecord:
    is_deleted = Column(Boolean, nullable=False, default=False, server_default=false())


class Rider(CDBRecord, NamespaceBase):
    """Cyclist table - stores rider information
    
    Internal model with clean field names.
    PCM version-specific fields are mapped during import.
    """
    __tablename__ = 'rider'
    
    # IDs
    id = Column(Integer, primary_key=True)
    first_cycling_id = Column(Integer, index=True)
    region_id = Column(Integer)
    team_id = Column(Integer)

    identity_pcs_status = Column(String(20), nullable=False, default='unknown', server_default='unknown')
    has_pending_stat_changes = Column(Boolean, nullable=False, default=False, server_default=false())
    has_pending_attribute_changes = Column(Boolean, nullable=False, default=False, server_default=false())

    __table_args__ = (
        CheckConstraint("identity_pcs_status IN ('unknown', 'verified', 'review', 'failed')",
                        name='ck_rider_identity_pcs_status'),
    )

    # Basic info
    first_name = Column(String(100))
    last_name = Column(String(100))
    first_last_name = Column(String(200))
    pcs_rider_path = Column(String(500), index=True)

    #photo = Column(String(255))
    birth_date = Column(Integer)
    size_cm = Column(Float)  # Height in cm
    weight_kg = Column(Float)  # Weight in kg
    
    # Skills (main attributes)
    stat_mo = Column(Integer)  # Mountain/climbing skill
    stat_mm = Column(Integer)  # Medium mountain skill
    stat_dh = Column(Integer)  # Downhill skill
    stat_hil = Column(Integer)  # Hill climbing skill
    stat_tt = Column(Integer)  # Time trial skill
    stat_prl = Column(Integer)  # Prologue skill
    stat_fla = Column(Integer)  # Flat terrain skill
    stat_spr = Column(Integer)  # Sprint skill
    stat_acc = Column(Integer)  # Acceleration
    stat_end = Column(Integer)  # Endurance/stamina
    stat_res = Column(Integer)  # Resistance/toughness
    stat_rec = Column(Integer)  # Recovery ability
    stat_cob = Column(Integer)  # Cobblestone skill
    stat_att = Column(Integer)  # Attack skill
    
    def __repr__(self):
        return f'<Rider {self.first_name} {self.last_name}>'


class Team(CDBRecord, NamespaceBase):
    """Team table - stores team information imported from the CDB.

    This is intentionally minimal until the CDB team schema is finalized.
    """
    __tablename__ = 'team'

    id = Column(Integer, primary_key=True)
    short_name = Column(String(255))
    team_name = Column(String(255))
    abbreviation = Column(String(20), index=True)
    country_id = Column(Integer, index=True)
    division_id = Column(Integer, index=True)
    email_suffix = Column(String(255))
    manager_general = Column(String(255))
    current_evaluation = Column(Float)
    budget = Column(Integer)
    primary_color = Column(String(20))
    secondary_color = Column(String(20))

    def __repr__(self):
        return f'<Team {self.team_name or self.id}>'


class Country(CDBRecord, NamespaceBase):
    """Country lookup table imported from the CDB."""

    __tablename__ = 'country'

    id = Column(Integer, primary_key=True)
    constant = Column(String(50), index=True)
    continent_id = Column(Integer, index=True)
    territory_id = Column(Integer, index=True)
    flag_name = Column(String(100))
    name_string_id = Column(Integer)
    calendar = Column(Integer)
    cyclist_world_championship_count = Column(Integer)
    cyclist_world_championship_itt_count = Column(Integer)
    cycling_popularity = Column(Integer)
    training_structure = Column(Integer)

    def __repr__(self):
        return f'<Country {self.constant or self.id}>'


class Division(CDBRecord, NamespaceBase):
    """Division lookup table imported from the CDB."""

    __tablename__ = 'division'

    id = Column(Integer, primary_key=True)
    name_string_id = Column(Integer)
    constant = Column(String(50), index=True)
    race_wanted_ratio = Column(Float)

    def __repr__(self):
        return f'<Division {self.constant or self.id}>'


class Region(CDBRecord, NamespaceBase):
    """Region lookup table imported from the CDB."""

    __tablename__ = 'region'

    id = Column(Integer, primary_key=True)
    constant = Column(String(100), index=True)
    name_string_id = Column(Integer)
    country_id = Column(Integer, index=True)
    climat_id = Column(Integer, index=True)
    is_europe = Column(Boolean)
    weight = Column(Integer)
    position_x = Column(Integer)
    position_y = Column(Integer)
    training_structure = Column(Integer)
    cycling_popularity = Column(Integer)

    def __repr__(self):
        return f'<Region {self.constant or self.id}>'


class CDBChange(NamespaceBase):
    """One proposed or applied attribute change; frozen after approval.

    Attribute names use logical mapped field names. Values are serialized as
    strings, with SQL NULL representing null or an absent value. Inserts/deletes
    also use one row per attribute. Actor IDs refer to main
    users without a cross-database foreign key. Status is a current projection;
    lifecycle history belongs in CDBChangeEvent. Services enforce transitions.
    Numeric deltas are derived from after_value minus before_value after typed
    validation. Nullable race/list markers retain stats approval scopes; both
    are NULL for individual edits. batch_id groups a submission within a scope.
    """

    __tablename__ = 'cdb_changes'

    id = Column(BigInteger, primary_key=True)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=False)
    attribute_name = Column(String(100), nullable=False)
    operation = Column(String(20), nullable=False)
    before_value = Column(Text)
    after_value = Column(Text)
    source = Column(String(50), nullable=False)
    created_by_id = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    batch_id = Column(String(64), index=True)
    race_result_id = Column(Integer, index=True)
    stat_list_id = Column(Integer, index=True)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    approved_by_id = Column(Integer)
    approved_at = Column(DateTime(timezone=True))
    applied_at = Column(DateTime(timezone=True))
    context = Column(JSONB, nullable=False, default=dict, server_default='{}')

    __table_args__ = (
        CheckConstraint("operation IN ('insert', 'update', 'delete')", name='ck_cdb_changes_operation'),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name='ck_cdb_changes_status'),
        CheckConstraint('race_result_id IS NULL OR stat_list_id IS NULL', name='ck_cdb_changes_source_scope'),
        Index('idx_cdb_changes_entity_created', 'entity_type', 'entity_id', 'created_at'),
    )


class CDBChangeEvent(NamespaceBase):
    """Append-only lifecycle action referencing a change without copying its payload.

    Event types include created, approved, rejected, approval_revoked, applied,
    conflict_overridden, and released. release_id identifies the release for a release event, allowing
    the same change to appear in multiple releases. No cascade deletes history.

    conflict_overridden context stores reviewed_current_value (string or null)
    and an optional reason; actor_id/occurred_at identify the decision. An applied
    event records actual_before_value in context. Services must recheck the
    reviewed value transactionally before applying; a later value needs a fresh
    override. These context contracts and append-only writes are service rules.
    """

    __tablename__ = 'cdb_change_events'

    id = Column(BigInteger, primary_key=True)
    change_id = Column(BigInteger, ForeignKey('cdb_changes.id', ondelete='RESTRICT'), nullable=False)
    event_type = Column(String(50), nullable=False)
    actor_id = Column(Integer)
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    release_id = Column(Integer, ForeignKey('releases.id', ondelete='RESTRICT'), index=True)
    context = Column(JSONB, nullable=False, default=dict, server_default='{}')

    __table_args__ = (
        Index('idx_cdb_change_events_change_time', 'change_id', 'occurred_at'),
    )


# Only mapped objects belong here: import/export iterates this collection.
CDB_TABLES = [Country, Division, Region, Team, Rider]
CDB_HISTORY_TABLES = [CDBChange, CDBChangeEvent]


