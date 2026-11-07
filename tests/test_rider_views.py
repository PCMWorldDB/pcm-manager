"""Rider views filter before pagination and keep namespace identities separate."""
import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models import Namespace, Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.web import routes


@pytest.fixture
def rider_views(app, client, monkeypatch):
    urls, engines, namespaces = [], [], []
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        for index, slug in enumerate(('view-alpha', 'view-beta')):
            url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            ActivityMonitorBase.metadata.create_all(engine)
            namespace = Namespace(id=70000 + index, name=slug, slug=slug, database_name=url.database,
                                  database_year=2026, gender='Men')
            namespaces.append(namespace)
            with engine.begin() as conn:
                conn.execute(text("""INSERT INTO rider (id, first_name, last_name, stat_mo)
                    VALUES (1, :name, 'Rider', :stat), (2, 'Untracked', 'Rider', 60)"""),
                             {'name': slug, 'stat': 80 if index == 0 else 70})
                conn.execute(text("""INSERT INTO activity_monitor_riders
                    (id, identity_key, source_worlddb_rider_id, identity_generation, first_name, last_name,
                     team_name, identity_status, identity_hold, eligible, present_in_cdb, created_at, updated_at)
                    VALUES (1, 'worlddb:1:g0', 1, 0, :name, 'Rider', '', 'verified', FALSE, TRUE, TRUE, NOW(), NOW())"""),
                             {'name': slug})
                conn.execute(text("""INSERT INTO activity_monitor_source_checks
                    (id, rider_id, identity_generation, source, source_status, coverage_status, identity_result, checked_at)
                    VALUES (1, 1, 0, 'firstcycling', 'success', 'complete', 'verified', NOW())"""))
                conn.execute(text("""INSERT INTO activity_monitor_results
                    (rider_id, identity_generation, identity_verified, source, source_result_key,
                     result_date, race_name, participation_status, imported_at, season, row_type,
                     race_category, race_url, stage_name, result_position, gc_position)
                    VALUES (1, 0, TRUE, 'firstcycling', 'race-1', :day, :race, 'confirmed', NOW(),
                            2026, 'race', '', '', '', '', '')"""),
                             {'day': date.today() - timedelta(days=5 if index == 0 else 200), 'race': slug + ' race'})
        monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        yield namespaces, engines
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_rider_view_columns_filters_and_namespace_state(app, client, rider_views):
    namespaces, _ = rider_views
    page = client.get('/n/view-alpha/riders')
    assert page.status_code == 200
    assert b'<th class="col-stat">MO</th>' in page.data
    assert b'name="stat_min"' in page.data and b'name="activity_status"' not in page.data
    assert b'<th>Last Race</th>' not in page.data
    response = client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'activity'})
    assert response.location.endswith('/n/view-alpha/riders')
    page = client.get(response.location)
    assert b'view-alpha race' in page.data and b'view-beta race' not in page.data
    assert b'name="activity_status"' in page.data and b'name="stat_min"' not in page.data
    assert b'<th class="col-stat">MO</th>' not in page.data
    client.post('/n/view-alpha/riders', data={'activity_status': 'recent'})
    page = client.get('/n/view-alpha/riders')
    assert b'view-alpha Rider' in page.data and b'Untracked Rider' not in page.data
    # The second namespace keeps Stats as its default despite overlapping rider IDs.
    page = client.get('/n/view-beta/riders')
    assert b'<th class="col-stat">MO</th>' in page.data
    client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'stats'})
    client.post('/n/view-alpha/riders', data={'stat_key': 'mo', 'stat_min': '75'})
    page = client.get('/n/view-alpha/riders')
    assert b'view-alpha Rider' in page.data and b'Untracked Rider' not in page.data
    with app.app_context():
        alpha = routes._load_namespace_roster_riders(namespaces[0], {'view': 'activity', 'activity_status': 'recent'})
        beta = routes._load_namespace_roster_riders(namespaces[1], {'view': 'activity', 'activity_status': 'recent'})
        assert alpha[1] == 1 and beta[1] == 0
    assert client.post('/n/view-alpha/riders', data={'stat_key': 'mo', 'stat_min': '100'}).status_code == 400
    assert client.post('/n/view-alpha/riders', data={'stat_key': 'not_a_stat'}).status_code == 400
    assert client.get('/n/missing/riders').status_code == 404


def test_activity_view_checks_permission_on_direct_requests(app, client, rider_views, monkeypatch):
    original = routes._can_access_panel
    monkeypatch.setattr(routes, '_can_access_panel', lambda panel: panel != 'activity_monitor' and original(panel))
    assert client.get('/n/view-alpha/riders').status_code == 200
    assert client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'activity'}).status_code == 403
    with client.session_transaction() as session:
        session['riders_state_by_namespace'] = {'70000': {**routes._default_riders_state(), 'view': 'activity'}}
    assert client.get('/n/view-alpha/riders').status_code == 403


def test_activity_view_ignores_unverified_future_and_old_identity_results(app, rider_views):
    namespaces, engines = rider_views
    with engines[0].begin() as conn:
        conn.execute(text("""INSERT INTO activity_monitor_results
            (rider_id, identity_generation, identity_verified, source, source_result_key,
             result_date, race_name, participation_status, imported_at, season, row_type,
             race_category, race_url, stage_name, result_position, gc_position)
            VALUES (1, :generation, :verified, :source, :key, :day, 'Excluded Race',
                    'confirmed', NOW(), 2026, 'race', '', '', '', '', '')"""), [
                        {'generation': generation, 'verified': verified, 'source': source,
                         'key': str(index), 'day': date.today() + timedelta(days=offset)}
                        for index, (generation, verified, source, offset) in enumerate([
                            (0, False, 'firstcycling', -1), (0, True, 'firstcycling', 1),
                            (1, True, 'firstcycling', -1), (0, True, 'pcs', -1),
                        ])])
    with app.app_context():
        rows, total, _, _ = routes._load_namespace_roster_riders(namespaces[0], {'view': 'activity'})
        assert total == 2
        assert rows[0].latest_race_name == 'view-alpha race'
        assert rows[0].days_since_last_race == 5


def test_missing_activity_history_keeps_roster_visible(app, rider_views):
    namespaces, engines = rider_views
    ActivityMonitorBase.metadata.drop_all(engines[0])
    with app.app_context():
        rows, total, _, _ = routes._load_namespace_roster_riders(namespaces[0], {'view': 'activity'})
        assert total == 2 and len(rows) == 2
        assert all(row.activity_available is False for row in rows)
        assert all(row.latest_result_date is None for row in rows)


def test_country_names_use_namespace_country_flag_name(app, client, rider_views):
    namespaces, engines = rider_views
    for index, engine in enumerate(engines):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO country (id, constant, flag_name) VALUES (1, 'ITA', :name)"),
                         {'name': 'Italy' if index == 0 else 'Different Italy'})
            conn.execute(text("INSERT INTO region (id, country_id) VALUES (1, 1)"))
            conn.execute(text("INSERT INTO team (id, team_name, country_id) VALUES (1, 'Test Team', 1)"))
            conn.execute(text('UPDATE rider SET team_id=1, region_id=1'))
            conn.execute(text("UPDATE activity_monitor_riders SET nationality='ITA', nationality_code='ITA'"))
    with app.app_context():
        for index, namespace in enumerate(namespaces):
            expected = 'Italy' if index == 0 else 'Different Italy'
            riders, total, countries, _ = routes._load_namespace_roster_riders(namespace, {'country': '1'})
            assert total == 2 and all(rider.nation_name == expected for rider in riders)
            assert countries == [{'value': '1', 'label': expected}]
            teams, _, countries, _ = routes._load_namespace_roster_teams(namespace, selected_country='1')
            assert len(teams) == 1 and teams[0].nation_name == expected
            assert countries == [{'value': '1', 'label': expected}]
            assert routes._load_namespace_roster_team(namespace, 1).nation_name == expected
            assert routes._load_namespace_roster_rider(namespace, 1).nation_name == expected
            assert all(rider.nation_name == expected for rider in routes._load_namespace_roster_riders_for_team(namespace, 1))
            with engines[index].connect() as conn:
                label = conn.execute(text(f'SELECT {routes._activity_monitor_country_sql()} FROM activity_monitor_riders r')).scalar_one()
                assert label == expected
    for path in ['/n/view-alpha/riders', '/n/view-alpha/teams', '/n/view-alpha/teams/1', '/n/view-alpha/riders/1']:
        page = client.get(path)
        assert page.status_code == 200, path
        assert b'Italy' in page.data and b'>ITA<' not in page.data
    with engines[0].begin() as conn:
        conn.execute(text("UPDATE country SET flag_name=' ' WHERE id=1"))
    with app.app_context():
        assert routes._load_namespace_roster_rider(namespaces[0], 1).nation_name == 'ITA'
