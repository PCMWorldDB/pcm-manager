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
    race_results = db.relationship(
        'FirstCyclingRaceResult',
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
    stage_results = db.relationship(
        'FirstCyclingRaceResult',
        back_populates='stage',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

    __table_args__ = (
        db.UniqueConstraint('race_id', 'stage_number', name='uq_firstcycling_race_stage_number'),
        Index('ix_firstcycling_stage_race_stageid', 'race_id', 'stage_id'),
    )

    def __repr__(self):
        return f'<FirstCyclingRaceStage race_id={self.race_id} stage={self.stage_number}>'


class FirstCyclingRaceResult(MainDBModel):
    """Stores rider finishing positions for race-level or stage-level results"""
    __tablename__ = 'firstcycling_race_results'

    race_id = db.Column(db.Integer, db.ForeignKey('firstcycling_races.id', ondelete='CASCADE'), nullable=False, index=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('firstcycling_race_stages.id', ondelete='CASCADE'), nullable=True, index=True)
    result_scope = db.Column(db.String(20), nullable=False, default='race', index=True)
    rider_id = db.Column(db.Integer, nullable=False, index=True)
    team_id = db.Column(db.Integer, nullable=True, index=True)
    overall_position = db.Column(db.Integer, nullable=True, index=True)
    overall_time = db.Column(db.String(40), nullable=True)
    overall_time_seconds = db.Column(db.Integer, nullable=True)
    stage_position = db.Column(db.Integer, nullable=True, index=True)
    stage_time = db.Column(db.String(40), nullable=True)
    stage_time_seconds = db.Column(db.Integer, nullable=True)
    is_still_in_race = db.Column(db.Boolean, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    race = db.relationship('FirstCyclingRace', back_populates='race_results')
    stage = db.relationship('FirstCyclingRaceStage', back_populates='stage_results')

    __table_args__ = (
        db.UniqueConstraint('race_id', 'result_scope', 'stage_id', 'rider_id', name='uq_firstcycling_race_result_scope_stage_rider'),
        Index('ix_firstcycling_result_race_scope_overall_position', 'race_id', 'result_scope', 'overall_position'),
        Index('ix_firstcycling_result_stage_position', 'race_id', 'stage_id', 'stage_position'),
    )

    def __repr__(self):
        return (
            f'<FirstCyclingRaceResult race_id={self.race_id} stage_id={self.stage_id} '
            f'rider_id={self.rider_id} overall_position={self.overall_position} '
            f'stage_position={self.stage_position}>'
        )


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
    rider_ids = db.Column(db.JSON, nullable=True)
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

