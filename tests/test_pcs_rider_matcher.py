"""PCS matching derives road profile URLs from namespace rider slugs."""
from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations.cdb.field_configuration import default_rows
from webapp.integrations.pcs import PcsRiderMatcher, _normalize_pcs_rider_path
from webapp.models import Namespace
from webapp.models.ns.activity_monitor import ActivityMonitorRider
from webapp.models.ns.base import NamespaceBase
from webapp.models.ns.cdb import Rider
from webapp.models.ns.field_configuration import FieldLevelConfiguration


@pytest.fixture
def matcher_namespaces(app):
    root = make_url(os.environ['WORLDDB_TEST_DATABASE_URL'])
    urls, engines, namespaces = [], [], []
    try:
        for label in ('Alpha', 'Beta'):
            url = root.set(database='worlddb_test_' + uuid4().hex)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(Rider.__table__.insert(), [
                    dict(id=1, first_name=label, last_name='Rider', pcs_slug=label.lower() + '-rider', is_deleted=False),
                    dict(id=2, first_name='Deleted', last_name='Rider', pcs_slug='deleted-rider', is_deleted=True),
                    dict(id=3, first_name='Fallback', last_name='Rider', pcs_slug=None, is_deleted=False),
                    dict(id=4, first_name='Invalid', last_name='Rider', pcs_slug='0', is_deleted=False),
                ])
            namespaces.append(Namespace(name=label, slug=label.lower(), database_name=url.database))
        yield namespaces, engines
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)


@pytest.mark.parametrize('href', [
    '/rider/alpha-rider', '/rider/alpha-rider/', '/rider/alpha-rider/road',
    '/rider/alpha-rider/road/', 'https://www.procyclingstats.com/rider/alpha-rider/road',
    'https://procyclingstats.com/rider/alpha-rider?year=2026',
])
def test_result_links_normalize_to_the_road_profile(href):
    assert _normalize_pcs_rider_path(href) == '/rider/alpha-rider/road'


@pytest.mark.parametrize('href', [
    None, '', '/race/alpha-rider', '/rider/0/road', '/rider/alpha-rider/results',
    'https://example.com/rider/alpha-rider/road', '//example.com/rider/alpha-rider',
    'javascript:alert(1)', 'https://[invalid/rider/alpha-rider',
])
def test_non_profile_links_cannot_be_exact_matches(href):
    assert _normalize_pcs_rider_path(href) is None


def test_exact_matches_build_urls_from_slugs_and_stay_in_the_selected_namespace(app, matcher_namespaces):
    namespaces, engines = matcher_namespaces
    assert 'pcs_rider_path' not in Rider.__table__.c
    assert ('rider', 'pcs_rider_path') not in {(row['object'], row['name']) for row in default_rows('PCM2026')}
    with app.app_context():
        for index, namespace in enumerate(namespaces):
            matcher = PcsRiderMatcher(namespace)
            own_slug = namespace.slug + '-rider'
            other_slug = namespaces[1 - index].slug + '-rider'
            for href in (f'/rider/{own_slug}', f'/rider/{own_slug}/road'):
                match = matcher.match('A different display name', href)
                assert match.cyclist_id == 1 and match.score == 1.0
                assert match.method == 'pcs_rider_path_exact'
                assert match.cyclist['pcs_rider_url'] == f'https://www.procyclingstats.com/rider/{own_slug}/road'
                assert 'pcs_rider_path' not in match.cyclist
            assert matcher.match(None, f'/rider/{other_slug}/road').cyclist_id is None
            assert matcher.match(None, '/rider/deleted-rider/road').cyclist_id is None
            assert matcher.match('Deleted Rider').cyclist_id is None
    for engine in engines:
        assert 'pcs_rider_path' not in {column['name'] for column in inspect(engine).get_columns('rider')}


def test_missing_slug_uses_name_fallback_without_inventing_an_exact_source_link(app, matcher_namespaces):
    namespaces, _ = matcher_namespaces
    with app.app_context():
        matcher = PcsRiderMatcher(namespaces[0])
        match = matcher.match('Fallback Rider', '/rider/unrelated-rider/road')
        assert match.cyclist_id == 3 and match.method == 'cdb_cyclist_name_fuzzy'
        assert match.cyclist['pcs_rider_url'] is None
        assert matcher.match(None, '/rider/fallback-rider/road').cyclist_id is None
        assert matcher.match(None, '/rider/0/road').cyclist_id is None


def test_matcher_rejects_wrong_namespace_and_main_connections(app, matcher_namespaces):
    namespaces, engines = matcher_namespaces
    with app.app_context():
        wrong = SimpleNamespace(database_name=namespaces[1].database_name,
                                build_connection_string=lambda: engines[0].url)
        with pytest.raises(ValueError, match='selected namespace'):
            PcsRiderMatcher(wrong).match(None, '/rider/alpha-rider/road')
        main = SimpleNamespace(database_name=db.engines['main'].url.database,
                               build_connection_string=lambda: db.engines['main'].url)
        with pytest.raises(ValueError, match='selected namespace'):
            PcsRiderMatcher(main).match(None, '/rider/alpha-rider/road')


def test_drop_script_preserves_slugs_other_namespaces_and_activity_monitor(matcher_namespaces):
    _, engines = matcher_namespaces
    for engine in engines:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE rider ADD COLUMN pcs_rider_path VARCHAR(500)'))
            conn.execute(text('CREATE INDEX ix_rider_pcs_rider_path ON rider (pcs_rider_path)'))
            conn.execute(text("UPDATE rider SET pcs_rider_path='/rider/legacy-value' WHERE id=1"))
            conn.execute(FieldLevelConfiguration.__table__.insert().values(
                object='rider', name='pcs_rider_path', slug='rider:pcs_rider_path',
                is_mapped_to_cdb=False, is_editable=False, is_require_approval=True))
    ActivityMonitorRider.__table__.create(engines[0])
    with engines[0].begin() as conn:
        now = datetime.utcnow()
        conn.execute(ActivityMonitorRider.__table__.insert().values(
            id=1, identity_key='monitor-only', pcs_rider_path='/rider/monitor-only',
            created_at=now, updated_at=now))
        before = conn.execute(select(Rider.__table__).order_by(Rider.id)).all()
    script = (Path(__file__).resolve().parents[1] / 'migrations' / 'drop_rider_pcs_rider_path_namespace.sql').read_text()
    with engines[0].connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.exec_driver_sql(script)
        conn.exec_driver_sql(script)  # Safe to rerun on an already upgraded namespace.
    with engines[0].connect() as conn:
        assert conn.execute(select(Rider.__table__).order_by(Rider.id)).all() == before
        assert conn.scalar(text("SELECT count(*) FROM field_level_configuration WHERE name='pcs_rider_path'")) == 0
        assert conn.scalar(text('SELECT pcs_rider_path FROM activity_monitor_riders WHERE id=1')) == '/rider/monitor-only'
    assert 'pcs_rider_path' not in {column['name'] for column in inspect(engines[0]).get_columns('rider')}
    assert 'ix_rider_pcs_rider_path' not in {index['name'] for index in inspect(engines[0]).get_indexes('rider')}
    with engines[1].connect() as conn:
        assert conn.scalar(text('SELECT pcs_rider_path FROM rider WHERE id=1')) == '/rider/legacy-value'
        assert conn.scalar(text("SELECT count(*) FROM field_level_configuration WHERE name='pcs_rider_path'")) == 1
