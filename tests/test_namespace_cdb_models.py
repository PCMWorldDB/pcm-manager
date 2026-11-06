"""CDB models share provisioning metadata under their unprefixed table names."""
from webapp.integrations.cdb import get_cdb_mappings, get_cdb_table_name, get_field_mapping
from webapp.models.ns.base import NamespaceBase
from webapp.models.ns.cdb import CDB_TABLES, Country, Division, Region, Rider, Team
from webapp.models.ns.schema import NAMESPACE_TABLES, get_table_by_name, get_table_names


def test_cdb_models_are_registered_with_workflow_metadata():
    models = {'rider': Rider, 'team': Team, 'country': Country,
              'division': Division, 'region': Region}
    assert set(CDB_TABLES) == set(models.values())
    assert set(CDB_TABLES).issubset(NAMESPACE_TABLES)
    for name, model in models.items():
        assert model.__module__ == 'webapp.models.ns.cdb'
        assert model.__tablename__ == name
        assert model.metadata is NamespaceBase.metadata
        assert get_table_by_name(name) is model
        mapping = get_field_mapping('PCM2026', name)
        assert set(mapping['fields']).issubset(model.__table__.columns.keys())
    assert not any(name.startswith('cdb_') for name in get_table_names())
    assert 'stat_changes' in NamespaceBase.metadata.tables
    assert get_table_by_name('missing_table') is None


def test_rider_mapping_defaults_and_pcm_native_names():
    for version in get_cdb_mappings()['PCM_VERSION_MAPPINGS']:
        mapping = get_field_mapping(version, 'rider')
        assert get_field_mapping(version) == mapping
        assert get_cdb_table_name(version) == 'DYN_cyclist'
        assert mapping['fields']['id'] == 'IDcyclist'
        assert mapping['export_fields']['pcs_profile_slug'] == 'CONSTANT'
        assert 'cyclist' not in get_cdb_mappings()['PCM_VERSION_MAPPINGS'][version]
