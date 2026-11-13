"""Fill namespace rider mappings; CDB values win over seed values."""
from pathlib import Path

from webapp.models.ns.cdb import Rider
from webapp.seeds.identity import seed_namespace

DATA_PATH = Path(__file__).with_suffix('.yaml')


def seed(connection, *, assumed_slug_ids=()):
    return seed_namespace(connection, Rider.__table__, DATA_PATH, 'rider',
                          fill_blanks=True, assumed_slug_ids=assumed_slug_ids)
