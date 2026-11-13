"""Apply namespace race mappings and queue their FC/PCS pairs for main."""
from pathlib import Path

from webapp.models.ns.cdb import Race
from webapp.seeds.identity import seed_namespace

DATA_PATH = Path(__file__).with_suffix('.yaml')


def seed(connection):
    return seed_namespace(connection, Race.__table__, DATA_PATH, 'race')
