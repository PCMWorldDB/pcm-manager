"""Namespace tables imported one-to-one from mapped PCM CDB tables."""
from sqlalchemy import Boolean, Column, Float, Integer, String, false

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


CDB_TABLES = [Country, Division, Region, Team, Rider]


