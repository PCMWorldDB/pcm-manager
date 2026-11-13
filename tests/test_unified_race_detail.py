"""Unified race views preserve source identity and namespace boundaries."""
import os
import json
from datetime import date, time
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models import AuditLog, ExternalCallTask, ExternalCallTaskGroup, FirstCyclingRace, FirstCyclingRaceResult, Namespace, Role, User
from webapp.models.main.identity import RaceIdentity
from webapp.models.main.pcs import PcsRider, PcsRiderResult
from webapp.models.main.pcs import PcsRace
from webapp.models.ns.base import NamespaceBase
from webapp.web import routes
from webapp.web.race_detail import pcs_result_scope


@pytest.fixture
def race_views(app, client, monkeypatch):
    namespaces, engines, urls = [], [], []
    fc_id = 980071
    slug = 'race-' + uuid4().hex
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        race = FirstCyclingRace(race_id=fc_id, season_year=2026, gender='Men', name='FC Edition')
        identity = RaceIdentity(firstcycling_id=fc_id, pcs_slug=slug)
        rider = PcsRider(rider_slug=slug, display_name='PCS Finisher', profile_url='https://www.procyclingstats.com/rider/' + slug)
        db.session.add_all([user, race, identity, rider])
        db.session.flush()
        pcs_rows = []
        for year, name, path, position in [(2026, 'right', 'result', '3'),
                                           (2025, 'old', 'result', '1'),
                                           (2026, 'points', 'points', '1')]:
            row = PcsRiderResult(rider_id=rider.id, source_result_key=name, season=year,
                                 race_url=f'https://www.procyclingstats.com/race/{slug}/{year}/{path}',
                                 row_type='race', result_position=position,
                                 participation_status='confirmed', confirmed_participation=True)
            db.session.add(row)
            pcs_rows.append(row)
        db.session.commit()
        user_id = user.id
    try:
        for index, label in enumerate(('race-alpha', 'race-beta')):
            url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            namespaces.append(Namespace(id=78000 + index, name=label, slug=label,
                                        database_name=url.database, database_year=2026, gender='Men'))
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO country(id,constant) VALUES (1,'CDB Nation')"))
                conn.execute(text("INSERT INTO race_class(id,constant) VALUES (1,'CDB Class')"))
                conn.execute(text('''INSERT INTO race(id,race_name,first_cycling_id,pcs_slug,country_id,race_class_id,number_stages)
                    VALUES (1,:name,:fc,:pcs,1,1,1)'''),
                             dict(name=label, fc=fc_id if index == 0 else None, pcs=slug if index == 0 else None))
                conn.execute(text('''INSERT INTO stage(id,race_id,stage_number,day,month,constant)
                    VALUES (1,1,1,2,3,'CDB Stage')'''))
        monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        yield namespaces, engines, fc_id, slug
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            for row in pcs_rows:
                db.session.delete(row)
            for row in (rider, identity, race, db.session.get(User, user_id)):
                db.session.delete(row)
            db.session.commit()


def test_linked_race_unifies_metadata_and_pcs_fallback(client, race_views):
    _, _, fc_id, slug = race_views
    response = client.get(f'/n/race-alpha/races/cdb/1')
    assert response.status_code == 302
    assert response.location.endswith(f'/n/race-alpha/races/{fc_id}')
    response = client.get(response.location)
    assert response.status_code == 200
    page = BeautifulSoup(response.data, 'html.parser')
    assert 'FC Edition' in page.h2.get_text()
    assert 'CDB Nation' in page.get_text()
    assert 'CDB Class' in page.get_text()
    assert '2026-03-02' in page.get_text()
    assert slug in page.select_one('#race-identifiers').get_text()
    assert 'CDB Stage' in page.select_one('#cdb-stage-calendar').get_text()
    assert 'PCS Finisher' in page.select_one('#stage-results').get_text()
    assert page.select_one('.results-table tbody tr td').get_text(strip=True) == '3'
    assert not page.select('#stat-delta-form')
    assert not page.select('form[action$="approve-stat-deltas"]')


def test_cdb_only_uses_fc_layout_and_local_stage_ids(app, client, race_views):
    _, engines, fc_id, _ = race_views
    response = client.get('/n/race-beta/races/cdb/1')
    assert response.status_code == 200
    page = BeautifulSoup(response.data, 'html.parser')
    assert 'race-beta' in page.h2.get_text()
    assert 'FC Edition' not in page.get_text()
    assert 'PCS Finisher' not in page.get_text()
    assert 'CDB Class' in page.get_text()
    assert 'CDB Stage' in page.select_one('#stage-selection').get_text()
    link = page.select_one('#stage-selection a')['href']
    assert '/n/race-beta/races/cdb/1?stage_id=1' in link
    assert client.get(link).status_code == 200
    assert client.get('/n/race-beta/races/cdb/1?stage_id=999').status_code == 404
    assert client.get('/n/race-beta/races/cdb/999').status_code == 404
    assert client.get(f'/n/race-alpha/races/{fc_id}/999').status_code == 404
    # A same-number CDB ID must never be treated as an FC identity.
    assert client.get('/n/race-beta/races/1').status_code == 404
    with client.session_transaction() as session:
        user_id = int(session['_user_id'])
    with app.app_context():
        user = db.session.get(User, user_id)
        for role in user.roles.all():
            user.roles.remove(role)
        db.session.commit()
    assert client.get('/n/race-beta/races/cdb/1').status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.get('/n/race-beta/races/cdb/1').status_code == 302


def test_wrong_namespace_connection_rejected(client, race_views, monkeypatch):
    namespaces, _, _, _ = race_views
    wrong_url = namespaces[1].build_connection_string()
    monkeypatch.setattr(namespaces[0], 'build_connection_string', lambda: wrong_url)
    with pytest.raises(RuntimeError, match='expected namespace database'):
        client.get('/n/race-alpha/races/cdb/1')


def test_fc_results_remain_primary_with_pcs_on_same_page(app, client, race_views):
    _, _, fc_id, _ = race_views
    with app.app_context():
        race = FirstCyclingRace.query.filter_by(race_id=fc_id).one()
        db.session.add(FirstCyclingRaceResult(race_id=race.id, rider_id=980071,
                                             result_scope='race', overall_position=1))
        db.session.commit()
    page = BeautifulSoup(client.get(f'/n/race-alpha/races/{fc_id}').data, 'html.parser')
    assert 'Rider #980071' in page.select_one('.results-table').get_text()
    assert 'PCS Finisher' not in page.select_one('.results-table').get_text()
    assert page.select_one('nav[aria-label="Result source"] .active').get_text() == 'FC Results'
    pcs_page = BeautifulSoup(client.get(f'/n/race-alpha/races/{fc_id}?source=pcs').data, 'html.parser')
    assert 'PCS Finisher' in pcs_page.select_one('.results-table').get_text()
    assert not pcs_page.select('#stat-delta-form')
    assert 'CDB Attributes' in pcs_page.get_text()


def test_ambiguous_cdb_identity_is_not_merged(client, race_views):
    _, engines, fc_id, _ = race_views
    with engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race(id,race_name,first_cycling_id) VALUES (2,\'Conflicting CDB\',:fc)'), {'fc': fc_id})
    page = BeautifulSoup(client.get(f'/n/race-alpha/races/{fc_id}').data, 'html.parser')
    assert not page.select('#cdb-race-attributes')
    assert 'PCS Finisher' not in page.get_text()
    response = client.get('/n/race-alpha/races/cdb/1')
    assert response.status_code == 200
    assert 'race-alpha' in BeautifulSoup(response.data, 'html.parser').h2.get_text()


def test_pcs_scope_rejects_other_editions_and_classifications():
    from types import SimpleNamespace
    for path, expected in [('race/test/2026/gc', 'overall'), ('race/test/2026/stage-2', 2),
                           ('race/test/2025/result', None), ('race/test-other/2026/result', None),
                           ('race/test/2026/points', None)]:
        assert pcs_result_scope(SimpleNamespace(race_url=path), 'test', 2026) == expected


def test_race_debug_sources_and_identifiers(app, client, race_views):
    namespaces, engines, fc_id, slug = race_views
    with engines[0].begin() as conn:
        conn.execute(text('ALTER TABLE race ADD COLUMN debug_extra text'))
        conn.execute(text("UPDATE race SET debug_extra='<script>race</script>' WHERE id=1"))
        expected_cdb = conn.execute(text('SELECT to_jsonb(r) FROM race r WHERE id=1')).scalar_one()
    with app.app_context():
        race = FirstCyclingRace.query.filter_by(race_id=fc_id, season_year=2026).one()
        race.start_time = time(10, 30)
        race.start_date = date(2026, 3, 2)
        db.session.commit()
        expected_fc = {column.name: value.isoformat() if hasattr(value, 'isoformat') else value
                       for column in race.__table__.columns for value in [getattr(race, column.key)]}
    page = BeautifulSoup(client.get(f'/n/race-alpha/races/{fc_id}').data, 'html.parser')
    identifiers = page.select_one('#race-identifiers')
    assert identifiers.span.get_text(' ', strip=True) == 'Race Id: 1'
    assert page.select_one('#race-firstcycling-identifier a')['href'] == f'https://firstcycling.com/race.php?r={fc_id}&y=2026'
    assert page.select_one('#race-pcs-identifier a')['href'] == f'https://www.procyclingstats.com/race/{slug}/2026'
    button = page.select_one('button[aria-label="Debug race attributes"]')
    assert button['data-bs-target'] == '#race-debug-modal'
    assert button.select_one('svg[aria-hidden="true"]')
    modal = page.select_one('#race-debug-modal')
    assert namespaces[0].slug in modal.select_one('#race-debug-title').get_text()
    assert [tab.get_text(strip=True) for tab in modal.select('[role="tab"]')] == ['CDB', 'FC', 'PCS']
    assert [tab['aria-selected'] for tab in modal.select('[role="tab"]')] == ['true', 'false', 'false']
    def values(source):
        return {row.th.get_text(strip=True): json.loads(row.td.get_text())
                for row in modal.select(f'#race-debug-{source} tbody tr')}
    assert values('cdb') == expected_cdb
    assert values('fc') == expected_fc
    pcs = values('pcs')
    assert pcs['pcs_slug'] == slug
    assert pcs['race_identity']['firstcycling_id'] == fc_id
    assert len(pcs['race_results']) == 1
    assert pcs['race_results'][0]['result_position'] == '3'
    assert not modal.select('script')
    assert not page.find('a', string=lambda value: value and value.strip().startswith('Back '))
    # The overlapping local race ID in the other namespace has no provider links.
    other = BeautifulSoup(client.get('/n/race-beta/races/cdb/1').data, 'html.parser')
    assert [tab.get_text(strip=True) for tab in other.select('#race-debug-modal [role="tab"]')] == ['CDB']
    assert not other.select('#race-identifiers a')


def test_fc_only_debug_does_not_join_ambiguous_cdb_records(client, race_views):
    _, engines, fc_id, _ = race_views
    with engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race(id,first_cycling_id) VALUES (2,:fc)'), {'fc': fc_id})
    page = BeautifulSoup(client.get(f'/n/race-alpha/races/{fc_id}').data, 'html.parser')
    assert [tab.get_text(strip=True) for tab in page.select('#race-debug-modal [role="tab"]')] == ['FC']
    assert 'Race Id: -' in page.select_one('#race-identifiers').get_text(' ', strip=True)


def test_race_list_bulk_selection_and_namespace_group(app, client, race_views, monkeypatch):
    namespaces, _, fc_id, _ = race_views
    queued = []
    def create_group(requests, **kwargs):
        from types import SimpleNamespace
        queued.append((requests, kwargs))
        return SimpleNamespace(id=123)
    monkeypatch.setattr(routes, 'create_manual_external_group', create_group)
    page = BeautifulSoup(client.get('/n/race-alpha/races').data, 'html.parser')
    form = page.select_one('#race-selection-form')
    assert form['action'] == '/n/race-alpha/races/fetch-results'
    assert page.select_one(f'.race-select[value="{fc_id}"]').get('disabled') is None
    assert page.select_one('#select-all-races')
    assert page.find('button', string='Fetch Race Results')['disabled'] == ''
    for namespace in namespaces:
        response = client.post(f'/n/{namespace.slug}/races/fetch-results', data={'race_ids': f'{fc_id},{fc_id}'})
        assert response.status_code == 302
        requests, metadata = queued[-1]
        assert len(requests) == 1
        assert requests[0]['endpoint'] == 'get_race_results'
        assert requests[0]['params'] == dict(race_id=fc_id, season_year=2026, gender='Men')
        assert metadata['namespace_id'] == namespace.id
        assert metadata['workflow_type'] == 'fc_race_results'


@pytest.mark.parametrize('ids', ['', 'abc', '-1', '0', '2147483648', '1,' * 251, '1'])
def test_race_bulk_rejects_invalid_or_unknown_selection(client, race_views, monkeypatch, ids):
    monkeypatch.setattr(routes, 'create_manual_external_group', lambda *args, **kwargs: pytest.fail('Must not enqueue'))
    assert client.post('/n/race-alpha/races/fetch-results', data={'race_ids': ids}).status_code == 400


def test_race_bulk_revalidates_edition_and_conflicts(app, client, race_views, monkeypatch):
    _, engines, fc_id, _ = race_views
    monkeypatch.setattr(routes, 'create_manual_external_group', lambda *args, **kwargs: pytest.fail('Must not enqueue'))
    with app.app_context():
        race = FirstCyclingRace.query.filter_by(race_id=fc_id).one()
        for values in [dict(season_year=2025), dict(season_year=2026, gender='Women'),
                       dict(gender='Men', start_date=date(2099, 1, 1))]:
            for name, value in values.items():
                setattr(race, name, value)
            db.session.commit()
            assert client.post('/n/race-alpha/races/fetch-results', data={'race_ids': str(fc_id)}).status_code == 400
        race.start_date = None
        db.session.commit()
    with engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race(id,first_cycling_id) VALUES (2,:fc)'), {'fc': fc_id})
    assert client.post('/n/race-alpha/races/fetch-results', data={'race_ids': str(fc_id)}).status_code == 400
    page = BeautifulSoup(client.get('/n/race-alpha/races').data, 'html.parser')
    assert page.select_one(f'.race-select[value="{fc_id}"]').has_attr('disabled')


def test_race_bulk_requires_download_permission(client, race_views, monkeypatch):
    _, _, fc_id, _ = race_views
    monkeypatch.setattr(routes, '_can_download_stats', lambda: False)
    page = BeautifulSoup(client.get('/n/race-alpha/races').data, 'html.parser')
    assert not page.select('#race-selection-form')
    assert client.post('/n/race-alpha/races/fetch-results', data={'race_ids': str(fc_id)}).status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.post('/n/race-alpha/races/fetch-results', data={'race_ids': str(fc_id)}).status_code == 302


def test_multiple_selected_races_create_one_committed_taskgroup(app, client, race_views, monkeypatch):
    from webapp import worker
    namespaces, _, fc_id, _ = race_views
    namespace = namespaces[0]
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', lambda group_id: submitted.append(group_id))
    with app.app_context():
        db.session.execute(Namespace.__table__.insert().values(
            id=namespace.id, name=namespace.name, slug=namespace.slug,
            database_name=namespace.database_name, database_year=2026, gender='Men'))
        other = FirstCyclingRace(race_id=fc_id + 1, season_year=2026, gender='Men', name='Second race')
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    try:
        response = client.post('/n/race-alpha/races/fetch-results', data={'race_ids': f'{fc_id},{fc_id + 1},{fc_id}'})
        assert response.status_code == 302
        with app.app_context():
            group = ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id).one()
            assert submitted == [group.id]
            assert group.requested_count == 2
            assert (group.status, group.priority, group.origin) == ('queued', 'high', 'manual')
            assert (group.season_year, group.gender, group.workflow_type) == (2026, 'Men', 'fc_race_results')
            tasks = ExternalCallTask.query.filter_by(task_group_id=group.id).all()
            assert len(tasks) == 2
            assert {json.loads(task.request_params_json)['race_id'] for task in tasks} == {fc_id, fc_id + 1}
            assert all(task.status == 'queued' and task.endpoint == 'get_race_results' for task in tasks)
    finally:
        with app.app_context():
            group_ids = [row.id for row in ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id)]
            ExternalCallTask.query.filter(ExternalCallTask.task_group_id.in_(group_ids)).delete(synchronize_session=False)
            ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id).delete()
            db.session.delete(db.session.get(Namespace, namespace.id))
            db.session.delete(db.session.get(FirstCyclingRace, other_id))
            db.session.commit()


def test_add_pcs_existing_identity_redirects_without_queue(client, race_views, monkeypatch):
    _, _, fc_id, slug = race_views
    monkeypatch.setattr(routes, 'create_manual_external_group', lambda *args, **kwargs: pytest.fail('Existing identity must not fetch'))
    page = BeautifulSoup(client.get('/n/race-alpha/races').data, 'html.parser')
    assert page.find('button', string='Add PCS Race')
    assert not page.find('a', string='Manage identities')
    response = client.post('/n/race-alpha/races/pcs/add', data={'pcs_url': f'https://www.procyclingstats.com/race/{slug}/2026/result'})
    assert response.status_code == 200
    assert response.json == dict(status='success', existing=True, race_url=f'/n/race-alpha/races/{fc_id}')


@pytest.mark.parametrize('url', ['https://evil.example/race/test/2026',
                                 'https://www.procyclingstats.com/rider/test',
                                 'https://www.procyclingstats.com/race/test/2025',
                                 'https://www.procyclingstats.com@evil.example/race/test/2026',
                                 'https://www.procyclingstats.com:9999/race/test/2026'])
def test_add_pcs_rejects_noncanonical_destination(client, race_views, url):
    assert client.post('/n/race-alpha/races/pcs/add', data={'pcs_url': url}).status_code == 400


def test_add_pcs_rejects_known_gender_mismatch(app, client, race_views):
    _, _, _, slug = race_views
    with app.app_context():
        db.session.add(PcsRace(race_slug=slug, season_year=2026, gender='Women',
                               race_url=f'https://www.procyclingstats.com/race/{slug}/2026', name='Other gender'))
        db.session.commit()
    try:
        response = client.post('/n/race-alpha/races/pcs/add', data={'pcs_url': f'https://www.procyclingstats.com/race/{slug}/2026'})
        assert response.status_code == 400
        assert 'gender' in response.json['error']
    finally:
        with app.app_context():
            PcsRace.query.filter_by(race_slug=slug).delete()
            db.session.commit()


@pytest.mark.parametrize('success', [True, False])
def test_add_pcs_task_polling_and_pcs_only_page(app, client, race_views, monkeypatch, success):
    from webapp import worker
    from webapp.models.main.identity import RaceIdentity
    namespaces, _, _, _ = race_views
    namespace = namespaces[0]
    slug = 'new-' + uuid4().hex
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', lambda group_id: submitted.append(group_id))
    if success:
        monkeypatch.setattr(worker, 'fetch_pcs_race', lambda params: dict(
            race_slug=slug, season_year=2026, name='New PCS Race', classification='1.2',
            source_note='<script>safe</script>'))
    else:
        def unavailable(params):
            raise NotImplementedError('PCS race fetching is not configured yet.')
        monkeypatch.setattr(worker, 'fetch_pcs_race', unavailable)
    with app.app_context():
        db.session.execute(Namespace.__table__.insert().values(
            id=namespace.id, name=namespace.name, slug=namespace.slug, database_name=namespace.database_name,
            database_year=2026, gender='Men'))
        db.session.commit()
    try:
        form = dict(pcs_url=f'https://www.procyclingstats.com/race/{slug}/2026')
        response = client.post('/n/race-alpha/races/pcs/add', data=form)
        assert response.status_code == 202
        group_id, status_url = response.json['group_id'], response.json['status_url']
        assert submitted == [group_id]
        assert client.get(status_url).json['status'] == 'queued'
        assert client.post('/n/race-alpha/races/pcs/add', data=form).json['group_id'] == group_id
        assert client.post('/n/race-alpha/races/pcs/add', data={'pcs_url': form['pcs_url'] + '/result'}).json['group_id'] == group_id
        assert len(submitted) == 1
        assert client.get(status_url.replace('race-alpha', 'race-beta')).status_code == 404
        with app.app_context():
            task = ExternalCallTask.query.filter_by(task_group_id=group_id).one()
            assert worker.execute_task(task.id)
        result = client.get(status_url)
        assert result.headers['Cache-Control'] == 'no-store'
        if success:
            assert result.json['status'] == 'success'
            assert result.json['race_url'] == f'/n/race-alpha/races/pcs/{slug}/2026'
            page = BeautifulSoup(client.get(result.json['race_url']).data, 'html.parser')
            assert 'New PCS Race' in page.h2.get_text()
            assert 'Race Id: -' in page.select_one('#race-identifiers').get_text(' ', strip=True)
            assert [tab.get_text(strip=True) for tab in page.select('#race-debug-modal [role="tab"]')] == ['PCS']
            assert not page.select('#race-debug-modal script')
            listing = BeautifulSoup(client.get('/n/race-alpha/races').data, 'html.parser')
            assert listing.find('a', string=lambda text: text and text.strip() == 'New PCS Race')['href'] == result.json['race_url']
            assert client.post('/n/race-alpha/races/pcs/add', data=form).json['existing'] is True
        else:
            assert result.json['status'] == 'failed'
            assert 'not configured' in result.json['error']
            with app.app_context():
                assert PcsRace.query.filter_by(race_slug=slug).first() is None
    finally:
        with app.app_context():
            group_ids = [row.id for row in ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id)]
            ExternalCallTask.query.filter(ExternalCallTask.task_group_id.in_(group_ids)).delete(synchronize_session=False)
            ExternalCallTaskGroup.query.filter_by(namespace_id=namespace.id).delete()
            PcsRace.query.filter_by(race_slug=slug).delete()
            RaceIdentity.query.filter_by(pcs_slug=slug).delete()
            db.session.delete(db.session.get(Namespace, namespace.id))
            db.session.commit()


def test_add_pcs_and_link_permissions(client, race_views, monkeypatch):
    _, _, _, slug = race_views
    assert client.post(f'/n/race-alpha/races/pcs/{slug}/2026/link-fc', data={'firstcycling_id': '5'}).status_code == 403
    assert client.post(f'/n/race-alpha/races/pcs/{slug}/2026/link-cdb', data={'cdb_id': '1'}).status_code == 403
    monkeypatch.setattr(routes, '_can_download_stats', lambda: False)
    assert client.post('/n/race-alpha/races/pcs/add', data={'pcs_url': 'https://www.procyclingstats.com/race/test/2026'}).status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.post('/n/race-alpha/races/pcs/add').status_code == 302


def test_pcs_race_manual_schema_matches_model(app):
    from pathlib import Path
    from sqlalchemy import inspect
    schema = 'pcs_races_' + uuid4().hex
    with app.app_context():
        engine = db.engines['main']
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        isolated = create_engine(engine.url, connect_args={'options': f'-csearch_path={schema}'})
        try:
            with isolated.connect() as conn:
                conn.exec_driver_sql((Path(__file__).resolve().parents[1] / 'migrations/pcs_races_main.sql').read_text())
            assert {column['name'] for column in inspect(isolated).get_columns('pcs_races')} == set(PcsRace.__table__.columns.keys())
            assert {row['name'] for row in inspect(isolated).get_check_constraints('pcs_races')} == {'ck_pcs_race_slug', 'ck_pcs_race_year', 'ck_pcs_race_gender'}
        finally:
            isolated.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))


def test_pcs_only_race_links_follow_existing_identity_and_approval_policy(app, client, race_views):
    from webapp.models.main.identity import RaceIdentity
    _, engines, fc_id, _ = race_views
    slug = 'links-' + uuid4().hex
    with client.session_transaction() as session:
        user_id = int(session['_user_id'])
    with app.app_context():
        db.session.get(User, user_id).roles.append(Role.query.filter_by(name='admin').one())
        db.session.add(RaceIdentity(pcs_slug=slug))
        db.session.add(PcsRace(race_slug=slug, season_year=2026, gender='Men',
                               race_url=f'https://www.procyclingstats.com/race/{slug}/2026', name='Linkable PCS Race'))
        db.session.commit()
    try:
        path = f'/n/race-beta/races/pcs/{slug}/2026'
        page = BeautifulSoup(client.get(path).data, 'html.parser')
        assert page.select_one('#link-fc-race')
        assert page.select_one('#link-cdb-race')
        # An already-mapped FC identity is preserved on conflict.
        assert client.post(path + '/link-fc', data={'firstcycling_id': str(fc_id)}).status_code == 302
        with app.app_context():
            assert RaceIdentity.query.filter_by(pcs_slug=slug).one().firstcycling_id is None
        assert client.post(path + '/link-fc', data={'firstcycling_id': str(fc_id + 1000)}).status_code == 302
        with app.app_context():
            assert RaceIdentity.query.filter_by(pcs_slug=slug).one().firstcycling_id == fc_id + 1000
        assert client.post(path + '/link-cdb', data={'cdb_id': '999'}).status_code == 404
        assert client.post(path + '/link-cdb', data={'cdb_id': '1'}).status_code == 302
        with engines[1].connect() as conn:
            changes = conn.execute(text("SELECT attribute_name,after_value,status FROM cdb_changes WHERE entity_type='race' AND entity_id=1 ORDER BY id")).mappings().all()
            assert {row['attribute_name']: row['after_value'] for row in changes} == {'pcs_slug': slug, 'first_cycling_id': str(fc_id + 1000)}
            assert all(row['status'] == 'pending' for row in changes)
            assert conn.execute(text('SELECT pcs_slug FROM race WHERE id=1')).scalar_one() is None
        with engines[0].connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM cdb_changes')).scalar_one() == 0
        # A namespace with an established, different mapping rejects the link.
        response = client.post(path.replace('race-beta', 'race-alpha') + '/link-cdb', data={'cdb_id': '1'})
        assert response.status_code == 302
        with engines[0].connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM cdb_changes')).scalar_one() == 0
    finally:
        with app.app_context():
            PcsRace.query.filter_by(race_slug=slug).delete()
            RaceIdentity.query.filter_by(pcs_slug=slug).delete()
            db.session.commit()
