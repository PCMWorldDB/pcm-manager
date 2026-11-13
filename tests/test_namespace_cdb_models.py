"""CDB models share provisioning metadata under their unprefixed table names."""
import subprocess
import sys
from pathlib import Path

import pytest

from webapp.integrations.cdb import get_cdb_mappings, get_cdb_table_name, get_field_mapping
from webapp.models.ns.base import (
    NAMESPACE_TABLES, NamespaceBase, RiderList, RiderListItem, get_table_by_name, get_table_names,
)
from webapp.models.ns.cdb import CDB_TABLES, Country, Division, Race, RaceClass, Region, Rider, Stage, Team


@pytest.mark.parametrize('first_module', ['base', 'cdb', 'field_configuration'])
def test_namespace_model_import_order(first_module):
    script = f"""
import importlib
importlib.import_module('webapp.models.ns.{first_module}')
from webapp.models.ns.base import NamespaceBase, NAMESPACE_TABLES, get_table_names
names = get_table_names()
assert names == [model.__tablename__ for model in NAMESPACE_TABLES]
assert set(names) == set(NamespaceBase.metadata.tables)
assert {{'rider', 'field_level_configuration', 'releases', 'stat_changes'}} <= set(names)
assert all(model.metadata is NamespaceBase.metadata for model in NAMESPACE_TABLES)
"""
    subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )


def test_cdb_models_are_registered_with_workflow_metadata():
    models = {'rider': Rider, 'team': Team, 'country': Country,
              'division': Division, 'region': Region, 'race': Race, 'stage': Stage, 'race_class': RaceClass}
    assert set(CDB_TABLES) == set(models.values())
    assert set(CDB_TABLES).issubset(NAMESPACE_TABLES)
    for name, model in models.items():
        assert model.__module__ == 'webapp.models.ns.cdb'
        assert model.__tablename__ == name
        assert model.metadata is NamespaceBase.metadata
        assert get_table_by_name(name) is model
        mapping = get_field_mapping('PCM2026', name)
        assert set(mapping['fields']).issubset(model.__table__.columns.keys())
    assert not any(model.__tablename__.startswith('cdb_') for model in CDB_TABLES)
    assert {name for name in get_table_names() if name.startswith('cdb_')} == {
        'cdb_changes', 'cdb_change_events',
    }
    assert 'stat_changes' in NamespaceBase.metadata.tables
    assert get_table_by_name('rider_list') is RiderList
    assert get_table_by_name('rider_list_item') is RiderListItem
    assert 'rider_list_id' in RiderListItem.__table__.columns
    assert 'stat_list_items' not in NamespaceBase.metadata.tables
    assert 'stat_lists' not in NamespaceBase.metadata.tables
    assert get_table_by_name('missing_table') is None


def test_rider_mapping_defaults_and_pcm_native_names():
    for version in get_cdb_mappings()['PCM_VERSION_MAPPINGS']:
        mapping = get_field_mapping(version, 'rider')
        assert get_field_mapping(version) == mapping
        assert get_cdb_table_name(version) == 'DYN_cyclist'
        assert mapping['fields']['id'] == 'IDcyclist'
        assert mapping['fields']['pcs_slug'] == 'CONSTANT'
        assert mapping['export_fields']['pcs_profile_slug'] == 'CONSTANT'
        assert 'cyclist' not in get_cdb_mappings()['PCM_VERSION_MAPPINGS'][version]


def test_race_definitions_default_to_read_only_import_configuration():
    from webapp.integrations.cdb.field_configuration import can_edit_column, default_rows

    rows = {(row['object'], row['name']): row for row in default_rows('PCM2026')}
    for kind in ('race', 'stage', 'race_class'):
        mapping = get_field_mapping('PCM2026', kind)
        for name, config in mapping['mappings'].items():
            row = rows[(kind, name)]
            assert row['is_mapped_to_cdb'] is True
            assert row['cdb_source'] == config['source']
            assert row['is_editable'] is False
            assert not can_edit_column(kind, name)
