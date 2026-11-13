"""CDB imports upsert mapped records and retain deleted records atomically."""
import os
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.integrations.cdb import get_field_mapping
from webapp.models.ns.base import NamespaceBase
from webapp.namespace.setup import CDBImporter


def _make_cdb(path, rider_id, *, omit_cyclist=False):
    mappings = {
        name: get_field_mapping("PCM2026", name)
        for name in ("country", "division", "region", "team", "race_class", "race", "stage", "rider")
    }
    with sqlite3.connect(path) as connection:
        for kind, mapping in mappings.items():
            if omit_cyclist and kind == "rider":
                continue
            table_name = mapping["cdb_table_name"]
            columns = list(dict.fromkeys(mapping["fields"].values()))
            quoted_columns = ", ".join(f'"{column}" TEXT' for column in columns)
            connection.execute(f'CREATE TABLE "{table_name}" ({quoted_columns})')
            if kind == "rider":
                values = [None] * len(columns)
                values[columns.index(mapping["fields"]["id"])] = rider_id
                values[columns.index(mapping["fields"]["first_name"])] = "Updated"
                values[columns.index(mapping["fields"]["last_name"])] = "Rider"
                placeholders = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{column}"' for column in columns)
                connection.execute(
                    f'INSERT INTO "{table_name}" ({quoted}) VALUES ({placeholders})',
                    values,
                )
            else:
                id_column = mapping['fields']['id']
                connection.execute(
                    f'INSERT INTO "{table_name}" ("{id_column}") VALUES (?)',
                    (rider_id,),
                )


def test_cdb_update_soft_deletes_missing_data_and_rolls_back_invalid_import(app, tmp_path):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    create_database(url)
    engine = create_engine(url)
    try:
        NamespaceBase.metadata.create_all(engine)
        first_cdb = tmp_path / "first.sqlite"
        second_cdb = tmp_path / "second.sqlite"
        invalid_cdb = tmp_path / "invalid.sqlite"
        _make_cdb(first_cdb, 101)
        _make_cdb(second_cdb, 202)
        _make_cdb(invalid_cdb, 303, omit_cyclist=True)

        with CDBImporter(str(first_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
            importer.import_all_tables(replace_existing=True)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO rider_list_item (rider_list_id, rider_id) VALUES (1, 101)"
            ))
        with CDBImporter(str(second_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
            importer.import_all_tables(replace_existing=True)
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, first_name FROM rider ORDER BY id"
            )).all()
            assert rows == [(101, "Updated"), (202, "Updated")]
            assert connection.execute(text('SELECT id FROM rider WHERE is_deleted')).scalars().all() == [101]
            for table in ('team', 'country', 'division', 'region', 'race_class', 'race', 'stage'):
                assert connection.execute(text(f'SELECT id FROM {table} WHERE NOT is_deleted')).scalars().all() == [202]
            assert connection.execute(text(
                'SELECT rider_id FROM rider_list_item'
            )).scalars().all() == [101]

        with pytest.raises(sqlite3.OperationalError):
            with CDBImporter(str(invalid_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
                importer.import_all_tables(replace_existing=True)
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, first_name FROM rider ORDER BY id"
            )).all()
            assert rows == [(101, "Updated"), (202, "Updated")]
            for table in ('team', 'country', 'division', 'region', 'race_class', 'race', 'stage'):
                assert connection.execute(text(f'SELECT id FROM {table} WHERE NOT is_deleted')).scalars().all() == [202]
    finally:
        engine.dispose()
        drop_database(url)


@pytest.fixture
def import_database(app):
    url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database=f'worlddb_test_{uuid4().hex}')
    create_database(url)
    engine = create_engine(url)
    NamespaceBase.metadata.create_all(engine)
    try:
        yield engine, url.render_as_string(hide_password=False)
    finally:
        engine.dispose()
        drop_database(url)


def test_initial_plan_contains_only_counts_and_matches_empty_namespace(import_database, tmp_path):
    from webapp.integrations.cdb import cdb_import
    _, url = import_database
    path = tmp_path / 'initial.sqlite'
    _make_cdb(path, 42)
    analysis = cdb_import.analyze_initial(str(path), 'PCM2026')
    assert analysis['tables'] == {
        'country': 1, 'division': 1, 'region': 1, 'team': 1, 'rider': 1,
        'race_class': 1, 'race': 1, 'stage': 1,
    }
    assert analysis['errors'] == []
    assert 'changes' not in analysis
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        assert importer.analyze()['fingerprint'] == analysis['fingerprint']
        importer.import_all_tables(expected_fingerprint=analysis['fingerprint'])


@pytest.fixture
def identity_datasets(tmp_path, monkeypatch):
    import yaml
    from webapp.seeds import race_identity, rider_identity
    for module, kind in ((rider_identity, 'rider'), (race_identity, 'race')):
        path = tmp_path / f'{kind}.yaml'
        path.write_text(yaml.safe_dump({f'{kind}s': [
            {f'{kind}_id': 42, f'{kind}_name': 'Reference label',
             'firstcycling_id': 123, 'pcs_slug': f'seed-{kind}'},
            {f'{kind}_id': 999, f'{kind}_name': 'Missing record',
             'firstcycling_id': None, 'pcs_slug': 'missing'},
            {f'{kind}_id': None, f'{kind}_name': 'External only',
             'firstcycling_id': 888, 'pcs_slug': 'external-only'},
        ]}), encoding='utf-8')
        monkeypatch.setattr(module, 'DATA_PATH', path)


@pytest.mark.parametrize('seed_initial', [False, True])
def test_identity_seeds_are_opt_in_and_updates_do_not_reseed(
    import_database, tmp_path, identity_datasets, seed_initial,
):
    engine, url = import_database
    path = tmp_path / 'identities.sqlite'
    _make_cdb(path, 42)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET CONSTANT='from-cdb', value_f_current_ability=55")
    original = path.read_bytes()
    with CDBImporter(str(path), url, 'PCM2026', expected_database=engine.url.database) as importer:
        importer.import_all_tables(seed_initial_identities=seed_initial)
    with engine.begin() as conn:
        for kind in ('rider', 'race'):
            row = conn.execute(text(f'SELECT first_cycling_id, pcs_slug FROM {kind}')).one()
            if seed_initial and kind == 'race':
                assert row == (123, f'seed-{kind}')
            elif kind == 'rider':
                assert row == (55, 'from-cdb')
            else:
                assert row[0] is None
                assert row[1] != f'seed-{kind}'
            assert conn.scalar(text(f'SELECT count(*) FROM {kind}')) == 1
            conn.execute(text(f"UPDATE {kind} SET first_cycling_id=456, pcs_slug='custom'"))
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables()
        with pytest.raises(ValueError, match='new, empty namespace'):
            importer.import_all_tables(seed_initial_identities=True)
    with engine.connect() as conn:
        for kind in ('rider', 'race'):
            # Later uploads retain the existing CONSTANT overwrite policy.
            expected_slug = 'from-cdb' if kind == 'rider' else 'custom'
            assert conn.execute(text(f'SELECT first_cycling_id, pcs_slug FROM {kind}')).one() == (456, expected_slug)
    assert path.read_bytes() == original


@pytest.mark.parametrize('fc_id,slug,expected', [
    (55, 'from-cdb', (55, 'from-cdb')),
    (None, 'from-cdb', (123, 'from-cdb')),
    (55, None, (55, 'seed-rider')),
    (None, None, (123, 'seed-rider')),
    (0, '0', (123, 'seed-rider')),
    (0, '   ', (123, 'seed-rider')),
    (None, '', (123, 'seed-rider')),
    (55, 'updated-rider', (55, 'updated-rider')),
])
def test_rider_identity_seeds_preserve_cdb_values_per_field(
    import_database, tmp_path, identity_datasets, fc_id, slug, expected,
):
    engine, url = import_database
    path = tmp_path / 'cdb-wins.sqlite'
    _make_cdb(path, 42)
    with sqlite3.connect(path) as source:
        source.execute('UPDATE DYN_cyclist SET value_f_current_ability=?, CONSTANT=?', (fc_id, slug))
    original = path.read_bytes()
    with CDBImporter(str(path), url, 'PCM2026', expected_database=engine.url.database) as importer:
        review = importer.analyze()
        importer.import_all_tables(expected_fingerprint=review['fingerprint'], seed_initial_identities=True)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT first_cycling_id, pcs_slug FROM rider WHERE id=42')).one() == expected
    assert path.read_bytes() == original


def test_null_rider_seeds_keep_generated_fallback(import_database, tmp_path, identity_datasets):
    from webapp.seeds import rider_identity
    engine, url = import_database
    path = tmp_path / 'fallback.sqlite'
    _make_cdb(path, 42)
    rider_identity.DATA_PATH.write_text(
        'riders: [{rider_id: 42, rider_name: Example, firstcycling_id: null, pcs_slug: null}]',
        encoding='utf-8',
    )
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables(seed_initial_identities=True)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT first_cycling_id, pcs_slug FROM rider WHERE id=42')).one() == (
            None, 'updated-rider',
        )


def test_identity_projection_failure_rolls_back_import(import_database, tmp_path, identity_datasets, monkeypatch):
    from webapp.seeds import race_identity
    engine, url = import_database
    path = tmp_path / 'rollback.sqlite'
    _make_cdb(path, 42)
    def invalid_catalog(connection):
        raise ValueError('Invalid FirstCycling ID')
    monkeypatch.setattr(race_identity, 'seed', invalid_catalog)
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        with pytest.raises(ValueError, match='Invalid FirstCycling ID'):
            importer.import_all_tables(seed_initial_identities=True)
    with engine.connect() as conn:
        for kind in ('rider', 'race', 'team', 'country', 'stage'):
            assert conn.scalar(text(f'SELECT count(*) FROM {kind}')) == 0


def test_identity_seeds_reject_wrong_namespace(import_database, tmp_path, identity_datasets):
    engine, url = import_database
    path = tmp_path / 'wrong-namespace.sqlite'
    _make_cdb(path, 42)
    with pytest.raises(ValueError, match='selected namespace'):
        with CDBImporter(str(path), url, 'PCM2026', expected_database='another_namespace') as importer:
            importer.import_all_tables(seed_initial_identities=True)
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM rider')) == 0


def test_identity_seeds_isolate_overlapping_ids(import_database, tmp_path, identity_datasets):
    engine, url = import_database
    other_url = engine.url.set(database=f'worlddb_test_{uuid4().hex}')
    create_database(other_url)
    other_engine = create_engine(other_url)
    try:
        NamespaceBase.metadata.create_all(other_engine)
        with other_engine.begin() as conn:
            for kind in ('rider', 'race'):
                conn.execute(text(f"INSERT INTO {kind} (id, first_cycling_id, pcs_slug) "
                                  "VALUES (42, 789, 'other-namespace')"))
        path = tmp_path / 'isolated.sqlite'
        _make_cdb(path, 42)
        with CDBImporter(str(path), url, 'PCM2026', expected_database=engine.url.database) as importer:
            importer.import_all_tables(seed_initial_identities=True)
        with engine.connect() as conn, other_engine.connect() as other:
            for kind in ('rider', 'race'):
                query = text(f'SELECT first_cycling_id, pcs_slug FROM {kind} WHERE id=42')
                assert conn.execute(query).one() == (123, f'seed-{kind}')
                assert other.execute(query).one() == (789, 'other-namespace')
    finally:
        other_engine.dispose()
        drop_database(other_url)


def test_shipped_identity_datasets():
    import yaml
    from webapp.seeds import race_identity, rider_identity
    for module, kind in ((rider_identity, 'rider'), (race_identity, 'race')):
        with module.DATA_PATH.open(encoding='utf-8') as source:
            entries = yaml.safe_load(source)[f'{kind}s']
        assert isinstance(entries, list)
        assert len(entries) == len({entry[f'{kind}_id'] for entry in entries})
        for entry in entries:
            assert set(entry) == {f'{kind}_id', f'{kind}_name', 'firstcycling_id', 'pcs_slug'}
            assert type(entry[f'{kind}_id']) is int and entry[f'{kind}_id'] > 0
            assert isinstance(entry[f'{kind}_name'], str)
            fc_id = entry['firstcycling_id']
            assert fc_id is None or (type(fc_id) is int and fc_id > 0)
            slug = entry['pcs_slug']
            assert slug is None or (isinstance(slug, str) and slug.strip() and len(slug) <= 500)


@pytest.mark.parametrize('version', ['PCM2024', 'PCM2025', 'PCM2026'])
def test_race_definition_import_preserves_types_lists_and_source(import_database, tmp_path, version):
    engine, url = import_database
    path = tmp_path / 'races.sqlite'
    _make_cdb(path, 1)
    with sqlite3.connect(path) as source:
        source.execute("""UPDATE STA_race SET gene_sz_race_name='Example Tour',
            fkIDrace_class=1, fkIDcountry=1, fkIDfirst_stage=1, fkIDlast_stage=1,
            gene_i_number_stages=1, gene_f_popularity=95.5,
            gene_b_national_tour=1, gene_b_aso=0, gene_b_selected=1,
            gene_ilist_fkIDteam='(12,3,99)', gene_ilist_objectif_info='()'""")
        source.execute("""UPDATE STA_stage SET fkIDrace=1, gene_i_day=14,
            gene_i_month=7, gene_i_stage_number=1, gene_b_selected=1,
            gene_sz_variant='example_2026'""")
        source.execute("""UPDATE STA_race_class SET CONSTANT='GrandTour',
            gene_i_max_team=23, gene_i_min_riders=8, gene_i_max_riders=8,
            fkIDdivision=1, gene_b_is_stagerace=1""")
    original = path.read_bytes()
    with CDBImporter(str(path), url, version) as importer:
        counts = importer.import_all_tables()
        assert {kind: counts[kind] for kind in ('race', 'stage', 'race_class')} == {
            'race': 1, 'stage': 1, 'race_class': 1,
        }
    with engine.connect() as conn:
        row = conn.execute(text('SELECT * FROM race WHERE id=1')).mappings().one()
        assert row['race_name'] == 'Example Tour'
        assert row['popularity'] == 95.5
        assert row['is_national_tour'] is True
        assert row['is_aso'] is False
        assert row['team_ids'] == '(12,3,99)'
        assert row['objective_info'] == '()'
        assert row['organizer_email'] is None
        assert conn.execute(text("""SELECT s.day, s.month, s.variant, c.max_teams, c.is_stage_race
            FROM stage s JOIN race r ON r.id=s.race_id
            JOIN race_class c ON c.id=r.race_class_id
            WHERE r.first_stage_id=s.id AND r.last_stage_id=s.id""")).one() == (
                14, 7, 'example_2026', 23, True,
            )
    assert path.read_bytes() == original
    with sqlite3.connect(path) as source:
        source.execute("UPDATE STA_race SET gene_sz_race_name='External change'")
        source.execute("UPDATE STA_stage SET gene_i_day=15")
        source.execute("UPDATE STA_race_class SET gene_i_max_team=25")
    with CDBImporter(str(path), url, version) as importer:
        plan = importer.analyze()
        assert all(plan['tables'][kind]['preserved'] == 1 for kind in ('race', 'stage', 'race_class'))
        importer.import_all_tables()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT race_name FROM race')) == 'Example Tour'
        assert conn.scalar(text('SELECT day FROM stage')) == 14
        assert conn.scalar(text('SELECT max_teams FROM race_class')) == 23


def test_invalid_stage_rolls_back_all_race_tables(import_database, tmp_path):
    engine, url = import_database
    path = tmp_path / 'invalid-stage.sqlite'
    _make_cdb(path, 1)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE STA_stage SET gene_i_day='invalid'")
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        with pytest.raises(ValueError):
            importer.import_all_tables()
    with engine.connect() as conn:
        for table in ('race', 'race_class', 'stage', 'country'):
            assert conn.scalar(text(f'SELECT count(*) FROM {table}')) == 0


def test_pcs_slug_import_overwrites_only_with_a_constant_or_fills_a_blank(import_database, tmp_path):
    engine, url = import_database
    path = tmp_path / 'pcs.sqlite'
    _make_cdb(path, 1)

    def import_file():
        with CDBImporter(str(path), url, 'PCM2026') as importer:
            review = importer.analyze()
            importer.import_all_tables(expected_fingerprint=review['fingerprint'])
            return review['changes']

    def rider_identity():
        with engine.connect() as conn:
            return tuple(conn.execute(text('SELECT pcs_slug, pcs_identity_status FROM rider')).one())

    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET CONSTANT = '  existing-pcs-slug  '")
    import_file()
    assert rider_identity() == ('existing-pcs-slug', 'unknown')

    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET CONSTANT = '0'")
    assert import_file() == []
    assert rider_identity() == ('existing-pcs-slug', 'unknown')

    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_slug='0'"))
    import_file()
    assert rider_identity() == ('updated-rider', 'unknown')

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE rider SET pcs_slug='manual-slug', pcs_identity_status='verified', pcs_birth_date='2000-01-01'
        """))
    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET CONSTANT = '  '")
    assert import_file() == []
    assert rider_identity() == ('manual-slug', 'verified')
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT pcs_birth_date::text FROM rider')) == '2000-01-01'

    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET CONSTANT = 'updated-pcs-slug'")
    assert any(change['field'] == 'pcs_slug' and change['action'] == 'overwrite' for change in import_file())
    assert rider_identity() == ('updated-pcs-slug', 'unknown')
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT pcs_birth_date FROM rider')) is None

    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_slug=NULL, pcs_identity_status='verified', "
                          "first_name='Łukasz', last_name='D’Ávila'"))
    with sqlite3.connect(path) as source:
        source.execute('ALTER TABLE DYN_cyclist DROP COLUMN CONSTANT')
    import_file()
    assert rider_identity() == ('lukasz-d-avila', 'unknown')

    with engine.begin() as conn:
        conn.execute(text('DELETE FROM rider'))
    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET gene_sz_firstname='Søren', gene_sz_lastname='de  L’Église'")
    import_file()
    assert rider_identity() == ('soren-de-l-eglise', 'unknown')


def test_creation_upload_previews_counts_before_creating_namespace(app, client, tmp_path, monkeypatch):
    import io
    from webapp.extensions import db
    from webapp.models import Namespace, Role, User
    from webapp.namespace.setup import NamespaceProvisioner

    def no_provision(*args, **kwargs):
        raise AssertionError('Preview must not provision a database')

    monkeypatch.setattr(NamespaceProvisioner, 'provision_namespace', no_provision)
    path = tmp_path / 'initial.sqlite'
    _make_cdb(path, 42)
    name = uuid4().hex
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.post('/admin/namespaces/create', data={
            'name': name, 'gender': 'Men', 'database_year': '2026',
            'pcm_version': 'PCM2026', 'status': 'draft',
            'cdb_file': (io.BytesIO(path.read_bytes()), 'initial.sqlite'),
        })
        assert response.status_code == 200
        assert b'Initial CDB import plan' in response.data
        assert b'Confirm and create namespace' in response.data
        assert b'Records to import' in response.data
        with app.app_context():
            assert Namespace.query.filter_by(name=name).first() is None
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize('policy,nulls,incoming,expected,error', [
    ('overwrite', 'allow', 'External', 'External', False),
    ('preserve', 'allow', 'External', 'Updated', False),
    ('reject', 'allow', 'External', 'Updated', True),
    ('overwrite', 'allow', None, None, False),
    ('overwrite', 'ignore', None, 'Updated', False),
    ('overwrite', 'reject', None, 'Updated', True),
])
def test_import_policies_and_nulls(import_database, tmp_path, monkeypatch,
                                   policy, nulls, incoming, expected, error):
    from webapp.integrations.cdb import cdb_import
    engine, url = import_database
    path = tmp_path / 'policy.sqlite'
    _make_cdb(path, 1)
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables()
    with sqlite3.connect(path) as source:
        source.execute('UPDATE DYN_cyclist SET gene_sz_firstname = ?', (incoming,))
    original = cdb_import.get_field_mapping

    def configured(version, kind):
        mapping = original(version, kind)
        if kind == 'rider':
            mapping['mappings']['first_name'].update(policy=policy, nulls=nulls)
        return mapping

    monkeypatch.setattr(cdb_import, 'get_field_mapping', configured)
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        review = importer.analyze()
        assert bool(review['errors']) == error
        if error:
            with pytest.raises(ValueError):
                importer.import_all_tables()
        else:
            importer.import_all_tables(expected_fingerprint=review['fingerprint'])
    with engine.connect() as connection:
        assert connection.execute(text('SELECT first_name FROM rider WHERE id = 1')).scalar_one() == expected


def test_review_staleness(import_database, tmp_path):
    engine, url = import_database
    path = tmp_path / 'stale.sqlite'
    _make_cdb(path, 1)
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables()
        review = importer.analyze()
        with engine.begin() as connection:
            connection.execute(text("UPDATE rider SET first_name = 'App edit' WHERE id = 1"))
        with pytest.raises(ValueError, match='stale'):
            importer.import_all_tables(expected_fingerprint=review['fingerprint'])
        importer.import_all_tables()
        assert importer.analyze()['tables']['rider']['updated'] == 0
    with engine.connect() as connection:
        assert connection.execute(text('SELECT first_name FROM rider WHERE id = 1')).scalar_one() == 'App edit'


def test_import_namespace_boundary_with_overlapping_ids(import_database, tmp_path):
    first_engine, first_url = import_database
    second_url = make_url(first_url).set(database=f'worlddb_test_{uuid4().hex}')
    create_database(second_url)
    second_engine = create_engine(second_url)
    try:
        NamespaceBase.metadata.create_all(second_engine)
        path = tmp_path / 'isolation.sqlite'
        _make_cdb(path, 1)
        second_string = second_url.render_as_string(hide_password=False)
        for url in (first_url, second_string):
            with CDBImporter(str(path), url, 'PCM2026') as importer:
                importer.import_all_tables()
        with first_engine.begin() as connection:
            connection.execute(text("UPDATE rider SET first_name = 'First namespace' WHERE id = 1"))
            connection.execute(text("UPDATE race SET race_name = 'First race' WHERE id = 1"))
            connection.execute(text("UPDATE stage SET day = 12 WHERE id = 1"))
            connection.execute(text("UPDATE race_class SET max_teams = 22 WHERE id = 1"))
        with CDBImporter(str(path), second_string, 'PCM2026', expected_database=second_url.database) as importer:
            importer.import_all_tables()
        with first_engine.connect() as connection:
            assert connection.execute(text('SELECT first_name FROM rider WHERE id = 1')).scalar_one() == 'First namespace'
            assert connection.scalar(text('SELECT race_name FROM race WHERE id = 1')) == 'First race'
            assert connection.scalar(text('SELECT day FROM stage WHERE id = 1')) == 12
            assert connection.scalar(text('SELECT max_teams FROM race_class WHERE id = 1')) == 22
        with second_engine.connect() as connection:
            assert connection.scalar(text('SELECT race_name FROM race WHERE id = 1')) is None
            assert connection.scalar(text('SELECT day FROM stage WHERE id = 1')) is None
            assert connection.scalar(text('SELECT max_teams FROM race_class WHERE id = 1')) is None
        with pytest.raises(ValueError, match='selected namespace'):
            with CDBImporter(str(path), first_url, 'PCM2026', expected_database=second_url.database):
                pass
    finally:
        second_engine.dispose()
        drop_database(second_url)


def test_reappearing_deleted_rider_stays_deleted(import_database, tmp_path):
    engine, url = import_database
    path = tmp_path / 'restore.sqlite'
    _make_cdb(path, 1)
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables()
    with engine.begin() as connection:
        connection.execute(text('UPDATE rider SET is_deleted = true WHERE id = 1'))
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        importer.import_all_tables()
    with engine.connect() as connection:
        assert connection.execute(text('SELECT is_deleted FROM rider WHERE id = 1')).scalar_one() is True


def test_duplicate_identity_is_rejected(import_database, tmp_path):
    engine, url = import_database
    path = tmp_path / 'duplicates.sqlite'
    _make_cdb(path, 1)
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO DYN_cyclist SELECT * FROM DYN_cyclist')
    with CDBImporter(str(path), url, 'PCM2026') as importer:
        with pytest.raises(ValueError, match='duplicate'):
            importer.import_all_tables()
    with engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM country')).scalar_one() == 0


def test_import_ui_analyzes_before_confirmation(app, client, import_database, tmp_path, monkeypatch):
    import io
    from webapp.extensions import db
    from webapp.models import AuditLog, Namespace, Role, User
    engine, url = import_database
    monkeypatch.setattr(Namespace, 'build_connection_string', lambda self: url)
    path = tmp_path / 'upload.sqlite'
    _make_cdb(path, 7)
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        namespace = Namespace(name=uuid4().hex, slug=uuid4().hex,
                              database_name=engine.url.database, database_year=2026,
                              pcm_version='PCM2026')
        db.session.add_all([user, namespace])
        db.session.commit()
        user_id, namespace_id = user.id, namespace.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        endpoint = f'/admin/namespaces/{namespace_id}/cdb'
        response = client.post(endpoint, data={'cdb_file': (io.BytesIO(path.read_bytes()), 'upload.sqlite')})
        assert response.status_code == 200
        assert b'Confirm import' in response.data
        assert b'CDB Import' in response.data
        with engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM rider')).scalar_one() == 0
        response = client.post(endpoint, data={'confirm': 'yes'}, follow_redirects=True)
        assert response.status_code == 200
        assert b'class="flash-messages"' not in response.data
        with engine.connect() as connection:
            assert connection.execute(text('SELECT id FROM rider')).scalar_one() == 7
        assert client.post(endpoint, data={'confirm': 'yes'}).status_code == 400
        response = client.post(endpoint, follow_redirects=True)
        assert b'class="flash error">Choose a .cdb or .sqlite file' in response.data
    finally:
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(Namespace, namespace_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize('role', ['admin', 'viewer', 'stat_editor'])
def test_import_confirmation_requires_review_and_permission(app, client, role):
    from webapp.extensions import db
    from webapp.models import Namespace, Role, User
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        namespace = Namespace(name=uuid4().hex, slug=uuid4().hex, database_name=uuid4().hex,
                              database_year=2026, pcm_version='PCM2026')
        db.session.add_all([user, namespace])
        db.session.commit()
        user_id, namespace_id = user.id, namespace.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.post(f'/admin/namespaces/{namespace_id}/cdb', data={'confirm': 'yes'})
        assert response.status_code == (400 if role == 'admin' else 403)
    finally:
        with app.app_context():
            db.session.delete(db.session.get(Namespace, namespace_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
