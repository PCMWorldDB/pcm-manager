"""Namespace database schema models

These models define the structure of tables in each namespace database.
Each namespace gets its own PostgreSQL database with this schema.
"""
import os
import yaml
from sqlalchemy import Column, Integer, String, Text, Date, Boolean, ForeignKey, Float, DateTime, BigInteger, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship


# Base for namespace database models
NamespaceBase = declarative_base()


class CDBCyclist(NamespaceBase):
    """Cyclist table - stores rider information
    
    Internal model with clean field names.
    PCM version-specific fields are mapped during import.
    """
    __tablename__ = 'cdb_cyclist'
    
    # IDs
    id = Column(Integer, primary_key=True)
    first_cycling_id = Column(Integer, index=True)
    region_id = Column(Integer)
    team_id = Column(Integer)

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
        return f'<Cyclist {self.first_name} {self.last_name}>'


class CDBTeam(NamespaceBase):
    """Team table - stores team information imported from the CDB.

    This is intentionally minimal until the CDB team schema is finalized.
    """
    __tablename__ = 'cdb_team'

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


class CDBCountry(NamespaceBase):
    """Country lookup table imported from the CDB."""

    __tablename__ = 'cdb_country'

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


class CDBDivision(NamespaceBase):
    """Division lookup table imported from the CDB."""

    __tablename__ = 'cdb_division'

    id = Column(Integer, primary_key=True)
    name_string_id = Column(Integer)
    constant = Column(String(50), index=True)
    race_wanted_ratio = Column(Float)

    def __repr__(self):
        return f'<Division {self.constant or self.id}>'


class CDBRegion(NamespaceBase):
    """Region lookup table imported from the CDB."""

    __tablename__ = 'cdb_region'

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


class StatChange(NamespaceBase):
    """Stat changes captured from race/stage review and ad-hoc list workflows."""

    __tablename__ = 'stat_changes'

    id = Column(Integer, primary_key=True)
    race_result_id = Column(Integer, nullable=True, index=True)
    stat_list_id = Column(Integer, nullable=True, index=True)
    release_id = Column(Integer, nullable=True, index=True)
    cdb_cyclist_id = Column(Integer, nullable=False, index=True)
    stat_name = Column(String(20), nullable=False, index=True)
    original_value = Column(Integer, nullable=False)
    final_value = Column(Integer, nullable=False)
    delta_value = Column(Integer, nullable=False)
    is_confilict_override = Column(Boolean, nullable=False, default=False)
    is_approved = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint(
            'race_result_id',
            'stat_list_id',
            'cdb_cyclist_id',
            'stat_name',
            name='uq_stat_changes_scope',
        ),
        Index(
            'idx_stat_changes_race_result_rider',
            'race_result_id',
            'cdb_cyclist_id',
        ),
        Index('idx_stat_changes_list_rider', 'stat_list_id', 'cdb_cyclist_id'),
        Index('idx_stat_changes_release', 'release_id'),
    )

    def __repr__(self):
        return (
            '<StatChange '
            f'race_result={self.race_result_id} '
            f'list={self.stat_list_id} '
            f'release={self.release_id} '
            f'cdb_cyclist={self.cdb_cyclist_id} '
            f'stat={self.stat_name} '
            f'delta={self.delta_value}>'
        )


class StatChangeSnapshot(NamespaceBase):
    """CDB rider stat snapshot captured before stat changes are approved."""

    __tablename__ = 'stat_change_snapshot'

    id = Column(Integer, primary_key=True)
    race_result_id = Column(Integer, nullable=True, index=True)
    stat_list_id = Column(Integer, nullable=True, index=True)
    cdb_cyclist_id = Column(Integer, nullable=False, index=True)
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
            'cdb_cyclist_id',
            name='uq_stat_change_snapshot_scope_rider',
        ),
        Index(
            'idx_stat_change_snapshot_race_result_rider',
            'race_result_id',
            'cdb_cyclist_id',
        ),
        Index('idx_stat_change_snapshot_list_rider', 'stat_list_id', 'cdb_cyclist_id'),
    )

    def __repr__(self):
        return (
            '<StatChangeSnapshot '
            f'race_result={self.race_result_id} '
            f'list={self.stat_list_id} '
            f'cdb_cyclist={self.cdb_cyclist_id}>'
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


class StatList(NamespaceBase):
    """Ad-hoc stat list metadata."""

    __tablename__ = 'stat_lists'

    id = Column(Integer, primary_key=True)
    list_name = Column(String(120), nullable=False)
    season_year = Column(Integer, nullable=False, index=True)
    gender = Column(String(20), nullable=False, index=True)
    created_by_id = Column(Integer, nullable=True)
    created_at = Column(DateTime)


class StatListItem(NamespaceBase):
    """Riders assigned to a stat list."""

    __tablename__ = 'stat_list_items'

    id = Column(Integer, primary_key=True)
    stat_list_id = Column(Integer, nullable=False, index=True)
    cdb_cyclist_id = Column(Integer, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('stat_list_id', 'cdb_cyclist_id', name='uq_stat_list_items_list_rider'),
    )


# List of all tables to create (in order)
NAMESPACE_TABLES = [
    CDBCountry,
    CDBDivision,
    CDBRegion,
    CDBTeam,
    CDBCyclist,
    Release,
    ReleaseFile,
    StatChange,
    StatChangeSnapshot,
    StatList,
    StatListItem,
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


# Load PCM Version Field Mappings from YAML
# Maps PCM CDB fields to internal model fields
def get_cdb_mappings():
    """Load CDB mappings from YAML file
    
    Returns:
        dict: Complete mapping configuration with PCM_VERSION_MAPPINGS and CDB_TABLE_MAPPINGS
    """
    yaml_path = os.path.join(os.path.dirname(__file__), 'cdb_mappings.yaml')
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


def get_field_mapping(pcm_version, table_type='cyclist'):
    """Get field mapping for a specific PCM version and table type
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        dict: Mapping configuration with 'cdb_table_name' and 'fields'
    """
    cdb_mappings = get_cdb_mappings()
    
    # Get the mapping version for this PCM version and table type
    version_mappings = cdb_mappings.get('PCM_VERSION_MAPPINGS', {})
    pcm_config = version_mappings.get(pcm_version.upper())
    if not pcm_config:
        raise ValueError(f"Unsupported PCM version: {pcm_version}")
    
    mapping_version = pcm_config.get(table_type)
    if not mapping_version:
        raise ValueError(f"Table type '{table_type}' not defined for PCM version {pcm_version}")
    
    # Get the actual table mapping from CDB_TABLE_MAPPINGS
    table_mappings = cdb_mappings.get('CDB_TABLE_MAPPINGS', {})
    version_config = table_mappings.get(mapping_version)
    if not version_config:
        raise ValueError(f"Mapping version '{mapping_version}' not found in CDB_TABLE_MAPPINGS")
    
    table_mapping = version_config.get(table_type)
    if not table_mapping:
        raise ValueError(f"Table type '{table_type}' not defined in mapping version '{mapping_version}'")
    
    return table_mapping


def get_cdb_table_name(pcm_version, table_type='cyclist'):
    """Get the CDB table name for a specific PCM version
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        str: CDB table name
    """
    mapping = get_field_mapping(pcm_version, table_type)
    return mapping['cdb_table_name']
