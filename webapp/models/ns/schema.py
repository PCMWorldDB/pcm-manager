"""Namespace database schema models

These models define the structure of tables in each namespace database.
Each namespace gets its own PostgreSQL database with this schema.
"""
import os
import yaml
from sqlalchemy import Column, Integer, String, Text, Date, Boolean, ForeignKey, Float
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


# List of all tables to create (in order)
NAMESPACE_TABLES = [
    CDBCyclist,
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
