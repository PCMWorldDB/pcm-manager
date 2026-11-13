"""Provider-only identity maps, independent of every namespace's CDB IDs."""
from webapp.extensions import db
from webapp.models.main.base import MainDBModel


def _constraints(kind):
    return (
        db.CheckConstraint('firstcycling_id IS NOT NULL OR pcs_slug IS NOT NULL',
                           name=f'ck_{kind}_identity_present'),
        db.CheckConstraint('firstcycling_id > 0', name=f'ck_{kind}_identity_positive'),
        db.CheckConstraint("pcs_slug IS NULL OR pcs_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
                           name=f'ck_{kind}_identity_slug'),
    )


class RaceIdentity(MainDBModel):
    __tablename__ = 'race_identity'

    firstcycling_id = db.Column(db.Integer, unique=True)
    pcs_slug = db.Column(db.String(500), unique=True)
    __table_args__ = _constraints('race')


class RiderIdentity(MainDBModel):
    __tablename__ = 'rider_identity'

    firstcycling_id = db.Column(db.Integer, unique=True)
    pcs_slug = db.Column(db.String(500), unique=True)
    __table_args__ = _constraints('rider')
