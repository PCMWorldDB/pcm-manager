"""Main database models for FirstCycling API data"""
from datetime import datetime
from sqlalchemy import Index
from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class FirstCyclingRace(MainDBModel):
    """Stores FirstCycling race data by race ID and season"""
    __tablename__ = 'firstcycling_races'

    race_id = db.Column(db.Integer, nullable=False)
    season_year = db.Column(db.Integer, nullable=False, default=2025)
    edition_id = db.Column(db.Integer, nullable=True, index=True)
    name = db.Column(db.String(255), nullable=True)
    race_type = db.Column(db.String(30), nullable=True, index=True)
    category = db.Column(db.String(20), nullable=True, index=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    nation_name = db.Column(db.String(100), nullable=True, index=True)
    description = db.Column(db.Text, nullable=True)
    classification = db.Column(db.String(20), nullable=True, index=True)
    classification_label = db.Column(db.String(120), nullable=True)
    distance_km = db.Column(db.Float, nullable=True)
    profile = db.Column(db.String(60), nullable=True)
    start_date = db.Column(db.Date, nullable=True, index=True)
    end_date = db.Column(db.Date, nullable=True, index=True)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    start_location = db.Column(db.String(255), nullable=True)
    end_location = db.Column(db.String(255), nullable=True)
    stage_count = db.Column(db.Integer, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    race_stages = db.relationship(
        'FirstCyclingRaceStage',
        back_populates='race',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

    __table_args__ = (
        db.UniqueConstraint('race_id', 'season_year', name='uq_firstcycling_race_id_year'),
        Index('ix_firstcycling_race_id', 'race_id'),
        Index('ix_firstcycling_race_year', 'season_year'),
    )

    def __repr__(self):
        return f'<FirstCyclingRace race_id={self.race_id} year={self.season_year}>'


class FirstCyclingRaceStage(MainDBModel):
    """Stores stage rows for a FirstCycling race"""
    __tablename__ = 'firstcycling_race_stages'

    race_id = db.Column(db.Integer, db.ForeignKey('firstcycling_races.id', ondelete='CASCADE'), nullable=False, index=True)
    stage_id = db.Column(db.Integer, nullable=True, index=True)
    stage_number = db.Column(db.Integer, nullable=True)
    stage_name = db.Column(db.String(255), nullable=True)
    stage_date = db.Column(db.Date, nullable=True, index=True)
    stage_distance_km = db.Column(db.Float, nullable=True)
    stage_profile = db.Column(db.String(80), nullable=True)
    stage_start_location = db.Column(db.String(255), nullable=True)
    stage_end_location = db.Column(db.String(255), nullable=True)
    stage_start_time = db.Column(db.Time, nullable=True)
    stage_end_time = db.Column(db.Time, nullable=True)

    race = db.relationship('FirstCyclingRace', back_populates='race_stages')

    __table_args__ = (
        db.UniqueConstraint('race_id', 'stage_number', name='uq_firstcycling_race_stage_number'),
        Index('ix_firstcycling_stage_race_stageid', 'race_id', 'stage_id'),
    )

    def __repr__(self):
        return f'<FirstCyclingRaceStage race_id={self.race_id} stage={self.stage_number}>'


class FirstCyclingTeam(MainDBModel):
    """Stores FirstCycling team details"""
    __tablename__ = 'firstcycling_teams'

    team_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    team_year = db.Column(db.Integer, nullable=True, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    nation_name = db.Column(db.String(100), nullable=True)
    division = db.Column(db.String(20), nullable=True, index=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    uci_code = db.Column(db.String(20), nullable=True, index=True)
    ranking_title = db.Column(db.String(100), nullable=True)
    ranking_rank = db.Column(db.Integer, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<FirstCyclingTeam team_id={self.team_id}>'


class FirstCyclingRider(MainDBModel):
    """Stores FirstCycling rider details"""
    __tablename__ = 'firstcycling_riders'

    rider_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    name = db.Column(db.String(255), nullable=True)
    first_name = db.Column(db.String(120), nullable=True)
    last_name = db.Column(db.String(120), nullable=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    height = db.Column(db.Integer, nullable=True)
    birth_date = db.Column(db.Date, nullable=True, index=True)
    nation_name = db.Column(db.String(100), nullable=True, index=True)
    team_id = db.Column(db.Integer, nullable=True, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<FirstCyclingRider rider_id={self.rider_id}>'

