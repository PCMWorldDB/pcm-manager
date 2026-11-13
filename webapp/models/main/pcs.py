"""Main database models for ProCyclingStats integration data"""
from datetime import datetime

from sqlalchemy import Index

from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class PcsRace(MainDBModel):
    """Shared PCS race edition facts, independent of namespace CDB IDs."""

    __tablename__ = 'pcs_races'
    race_slug = db.Column(db.String(500), nullable=False)
    season_year = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    race_url = db.Column(db.String(1000), nullable=False)
    name = db.Column(db.String(500), nullable=False)
    category = db.Column(db.String(100))
    classification = db.Column(db.String(100))
    nation_name = db.Column(db.String(100))
    description = db.Column(db.Text)
    distance_km = db.Column(db.Float)
    profile = db.Column(db.String(100))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    start_location = db.Column(db.String(255))
    end_location = db.Column(db.String(255))
    stage_count = db.Column(db.Integer)
    source_payload = db.Column(db.JSON)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('race_slug', 'season_year', name='uq_pcs_race_edition'),
        db.CheckConstraint("race_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name='ck_pcs_race_slug'),
        db.CheckConstraint('season_year BETWEEN 1 AND 9999', name='ck_pcs_race_year'),
        db.CheckConstraint("gender IN ('Men', 'Women')", name='ck_pcs_race_gender'),
    )


class PcsRider(MainDBModel):
    """Shared PCS profile facts, independent of CDB and monitor identities."""

    __tablename__ = 'pcs_riders'

    rider_slug = db.Column(db.String(255), nullable=False, unique=True)
    profile_url = db.Column(db.String(1000), nullable=False)
    display_name = db.Column(db.String(255))
    birth_date = db.Column(db.Date, index=True)
    nationality = db.Column(db.String(100), index=True)
    latest_participation_year = db.Column(db.Integer)
    latest_participation_date = db.Column(db.Date)
    latest_participation_race_name = db.Column(db.String(255))
    latest_participation_race_category = db.Column(db.String(100))
    latest_participation_race_type = db.Column(db.String(100))
    latest_participation_race_id = db.Column(db.String(1000))
    latest_participation_race_url = db.Column(db.String(1000))
    latest_participation_stage_number = db.Column(db.Integer)
    latest_participation_stage_name = db.Column(db.String(255))
    latest_participation_row_type = db.Column(db.String(40))
    latest_participation_result_position = db.Column(db.String(40))
    latest_participation_status = db.Column(db.String(40))
    latest_participation_confirmed = db.Column(db.Boolean)
    source_payload = db.Column(db.JSON)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    results = db.relationship(
        'PcsRiderResult', back_populates='rider',
        cascade='all, delete-orphan', passive_deletes=True,
    )


class PcsRiderResult(MainDBModel):
    """A PCS rider-page row, including headings and non-finishing statuses."""

    __tablename__ = 'pcs_rider_results'

    rider_id = db.Column(db.Integer, db.ForeignKey('pcs_riders.id', ondelete='CASCADE'), nullable=False)
    # Caller-defined stable row identity within a rider; never a monitor/FC ID.
    source_result_key = db.Column(db.String(200), nullable=False)
    season = db.Column(db.Integer, index=True)
    result_date = db.Column(db.Date)
    race_id = db.Column(db.String(1000))
    race_url = db.Column(db.String(1000), nullable=False)
    race_name = db.Column(db.String(255))
    race_category = db.Column(db.String(100))
    race_type = db.Column(db.String(100))
    stage_number = db.Column(db.Integer)
    stage_name = db.Column(db.String(255))
    row_type = db.Column(db.String(40), nullable=False)
    result_position = db.Column(db.String(40))
    participation_status = db.Column(db.String(40), nullable=False)
    confirmed_participation = db.Column(db.Boolean, nullable=False)
    source_payload = db.Column(db.JSON)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    rider = db.relationship('PcsRider', back_populates='results')

    __table_args__ = (
        db.UniqueConstraint('rider_id', 'source_result_key', name='uq_pcs_rider_result_key'),
        Index('ix_pcs_rider_result_rider_date', 'rider_id', 'result_date'),
    )


class PcsRaceResult(MainDBModel):
    """Stores raw PCS race result rows and namespace CDB rider matches."""

    __tablename__ = 'pcs_race_results'

    race_result_id = db.Column(db.Integer, db.ForeignKey('race_result_map.race_result_id', ondelete='SET NULL'), nullable=True, index=True)
    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id', ondelete='CASCADE'), nullable=False, index=True)
    race_name = db.Column(db.String(255), nullable=False, index=True)
    rider_name = db.Column(db.String(255), nullable=False, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    age = db.Column(db.Integer, nullable=True)
    position = db.Column(db.Integer, nullable=True, index=True)
    points = db.Column(db.Integer, nullable=True)
    time = db.Column(db.String(40), nullable=True)
    matched_rider_id = db.Column(db.Integer, nullable=True, index=True)
    match_method = db.Column(db.String(40), nullable=True, index=True)
    match_score = db.Column(db.Float, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    namespace = db.relationship('Namespace')
    race_result_map = db.relationship('RaceResultMap')

    __table_args__ = (
        db.UniqueConstraint(
            'namespace_id',
            'race_name',
            'position',
            'rider_name',
            name='uq_pcs_race_result_race_position_rider',
        ),
        Index('ix_pcs_result_namespace_race_position', 'namespace_id', 'race_name', 'position'),
    )

    def __repr__(self):
        return (
            f'<PcsRaceResult race_name={self.race_name!r} '
            f'rider_name={self.rider_name!r} position={self.position!r}>'
        )
