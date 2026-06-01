"""Main database model for provider race-result mappings."""
from webapp.extensions import db


class RaceResultMap(db.Model):
    """Master mapping row for a provider-specific race result."""

    __bind_key__ = 'main'
    __tablename__ = 'race_result_map'

    race_result_id = db.Column(db.Integer, primary_key=True)
    external_race_id = db.Column(db.String(80), nullable=False, index=True)
    external_stage_id = db.Column(db.String(80), nullable=True, index=True)
    provider = db.Column(db.String(50), nullable=False, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            'provider',
            'external_race_id',
            'external_stage_id',
            name='uq_race_result_map_provider_external',
        ),
    )

    def __repr__(self):
        return (
            f'<RaceResultMap race_result_id={self.race_result_id} '
            f'provider={self.provider!r} external_race_id={self.external_race_id!r}>'
        )
