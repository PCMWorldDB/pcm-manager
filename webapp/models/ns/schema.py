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
    
    # Primary key
    id = Column(Integer, primary_key=True)
    
    # Basic info
    first_name = Column(String(50))
    last_name = Column(String(50))
    nation = Column(String(3))  # 3-letter country code
    
    # Dates
    birthdate = Column(Integer)  # PCM stores dates as integers
    birth_year = Column(Integer)
    birth_month = Column(Integer)
    birth_day = Column(Integer)
    
    # Physical attributes
    weight = Column(Integer)  # in kg
    height = Column(Integer)  # in cm
    
    # Cyclist type and characteristics
    rider_type = Column(Integer)  # Rider type (climber, sprinter, etc.)
    potential = Column(Integer)  # Potential rating
    
    # Skills (main attributes)
    mountain = Column(Integer)  # Mountain/climbing skill
    hill = Column(Integer)  # Hill climbing skill
    time_trial = Column(Integer)  # Time trial skill
    prologue = Column(Integer)  # Prologue skill
    flat = Column(Integer)  # Flat terrain skill
    sprint = Column(Integer)  # Sprint skill
    acceleration = Column(Integer)  # Acceleration
    endurance = Column(Integer)  # Endurance/stamina
    resistance = Column(Integer)  # Resistance/toughness
    recovery = Column(Integer)  # Recovery ability
    
    # Additional attributes
    cobbles = Column(Integer)  # Cobblestone skill
    attack = Column(Integer)  # Attack skill
    fighter = Column(Integer)  # Fighter/combativeness
    
    # Career info
    level = Column(Integer)  # Overall level/rating
    training_program = Column(String(50))  # Training program
    
    # Contract and team
    team_id = Column(Integer)  # Foreign key to team
    year_pro = Column(Integer)  # Year turned professional
    contract_year = Column(Integer)  # Contract end year
    wage = Column(Integer)  # Wage/salary
    
    # Status flags
    is_locked = Column(Boolean, default=False)  # Locked from editing
    
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
    """Load PCM version mappings from YAML file"""
    yaml_path = os.path.join(os.path.dirname(__file__), 'cdb_mappings.yaml')
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


def get_field_mapping(pcm_version, table_type='cyclist'):
    """Get field mapping for a specific PCM version
    
    Args:
        pcm_version: PCM version string (e.g., 'PCM2025')
        table_type: Type of table (e.g., 'cyclist')
        
    Returns:
        dict: Mapping of internal fields to CDB fields
    """
    cdb_mappings = get_cdb_mappings()
    version_mapping = cdb_mappings.get(pcm_version.upper())
    if not version_mapping:
        raise ValueError(f"Unsupported PCM version: {pcm_version}")
    
    table_mapping = version_mapping.get(table_type)
    if not table_mapping:
        raise ValueError(f"Unsupported table type: {table_type}")
    
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
    return mapping['table_name']
