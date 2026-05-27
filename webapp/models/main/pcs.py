"""Main database models for ProCyclingStats integration data"""
from datetime import datetime

from sqlalchemy import Index

from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class PcsRaceResult(MainDBModel):
    """Stores raw PCS race result rows and FirstCycling rider matches."""

    __tablename__ = 'pcs_race_results'

    race_name = db.Column(db.String(255), nullable=False, index=True)
    rider_name = db.Column(db.String(255), nullable=False, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    age = db.Column(db.Integer, nullable=True)
    position = db.Column(db.Integer, nullable=True, index=True)
    points = db.Column(db.Integer, nullable=True)
    time = db.Column(db.String(40), nullable=True)
    matched_firstcycling_rider_id = db.Column(db.Integer, nullable=True, index=True)
    match_method = db.Column(db.String(40), nullable=True, index=True)
    match_score = db.Column(db.Float, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'race_name',
            'position',
            'rider_name',
            name='uq_pcs_race_result_race_position_rider',
        ),
        Index('ix_pcs_result_race_position', 'race_name', 'position'),
    )

    def __repr__(self):
        return (
            f'<PcsRaceResult race_name={self.race_name!r} '
            f'rider_name={self.rider_name!r} position={self.position!r}>'
        )
