"""Rider views filter before pagination and keep namespace identities separate."""
import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models import (
    ExternalCallTask, ExternalCallTaskGroup, FirstCyclingRace, FirstCyclingRaceResult,
    FirstCyclingRaceStage, Namespace, Role, User,
)
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


def test_rider_profile_flags_and_firstcycling_results(app, client, rider_views):
    _, engines = rider_views
    with engines[0].begin() as conn:
        conn.execute(text("""
            UPDATE rider SET pcs_identity_status='review', pcs_birth_date='2001-01-01',
                             birth_date=20000101, first_cycling_id=900001,
                             first_name='  Alice ', last_name=' Smith  ', first_last_name='Old Display Name',
                             pcs_slug='0'
            WHERE id=1
        """))
        conn.execute(text("UPDATE rider SET pcs_identity_status='failed' WHERE id=2"))
    with engines[1].begin() as conn:
        conn.execute(text("""
            UPDATE rider SET pcs_identity_status='verified', pcs_birth_date='2000-01-01', birth_date=20000101,
                             first_cycling_id=900002, pcs_slug='saved-slug' WHERE id=2
        """))
    with app.app_context():
        race = FirstCyclingRace(race_id=900001, season_year=2026, name='Profile Test Race', start_date=date(2026, 4, 1))
        stage = FirstCyclingRaceStage(race=race, stage_number=2, stage_name='Mountain Stage', stage_date=date(2026, 4, 2))
        older = FirstCyclingRace(race_id=900001, season_year=2025, name='Previous Edition', start_date=date(2025, 4, 1))
        stage_only = FirstCyclingRace(race_id=900003, season_year=2026, name='Stage Race', end_date=date(2026, 4, 5))
        first_stage = FirstCyclingRaceStage(race=stage_only, stage_number=1, stage_name='Opening Stage', stage_date=date(2026, 4, 4))
        final_stage = FirstCyclingRaceStage(race=stage_only, stage_number=2, stage_name='Final Stage', stage_date=date(2026, 4, 5))
        db.session.add_all([
            FirstCyclingRaceResult(race=race, rider_id=900001, overall_position=4, overall_time='4:00:00'),
            FirstCyclingRaceResult(race=race, stage=stage, result_scope='stage',
                                   rider_id=900001, stage_position=2, stage_time='2:00:00'),
            FirstCyclingRaceResult(race=race, rider_id=900002, overall_position=9, overall_time='9:00:00'),
            FirstCyclingRaceResult(race=older, rider_id=900001, overall_position=5, overall_time='5:00:00'),
            FirstCyclingRaceResult(race=stage_only, stage=first_stage, result_scope='stage', rider_id=900001,
                                   stage_position=1, stage_time='1:00:00', overall_position=1, overall_time='1:00:00'),
            FirstCyclingRaceResult(race=stage_only, stage=final_stage, result_scope='stage', rider_id=900001,
                                   stage_position=3, stage_time='2:00:00', overall_position=2, overall_time='3:00:00'),
        ])
        db.session.commit()
        race_ids = [race.id, older.id, stage_only.id]

    def profile(slug, rider_id):
        response = client.get(f'/n/view-{slug}/riders/{rider_id}')
        assert response.status_code == 200
        return BeautifulSoup(response.data, 'html.parser')

    def flags(markup):
        return [(flag.get_text(strip=True), flag['aria-label'])
                for flag in markup.select('[aria-label="Rider flags"] .rider-flag[role="img"][tabindex="0"]')]

    def pcs_slug(markup):
        label = markup.find(class_='object-attribute-label', string='PCS Slug')
        return label.find_next_sibling(class_='object-attribute-value').get_text(strip=True)

    def pcs_dob(markup):
        label = markup.select_one('#rider-identity').find(class_='object-attribute-label', string='PCS Date of Birth')
        return label.find_next_sibling(class_='object-attribute-value').get_text(strip=True)

    try:
        alpha = profile('alpha', 1)
        assert alpha.select_one('h2').get_text(strip=True) == 'Alice Smith'
        assert pcs_slug(alpha) == 'alice-smith'
        identity = alpha.select_one('#rider-identity')
        assert [label.get_text(strip=True) for label in identity.select('.object-attribute-label')] == [
            'Flags', 'PCS Slug', 'PCS Identity Status', 'PCS Date of Birth', 'PCS Identity Verified Override',
        ]
        assert identity.select('.object-attribute-value')[2].get_text(strip=True) == 'Needs review'
        assert identity.select('.object-attribute-value')[4].get_text(strip=True) == 'No'
        assert pcs_dob(alpha) == '2001-01-01'
        assert not alpha.select('#rider-attributes [aria-label="Edit PCS Identity Verified Override"]')
        assert alpha.select_one('#rider-attributes .object-attribute-label').get_text(strip=True) == 'Team'
        assert not alpha.select('#rider-attributes .rider-flag')
        assert flags(alpha) == [('⚠️', 'PCS identity needs review. A birth date is missing or the dates differ.')]
        columns = alpha.select('#rider-details .row > .col-lg-6')
        assert [column['id'] for column in columns] == ['rider-attributes', 'rider-identity']
        assert [section['id'] for section in alpha.select('section[id^="rider-"]')] == [
            'rider-details', 'rider-stats', 'rider-review', 'rider-results', 'rider-history',
        ]
        assert [heading.get_text(strip=True) for heading in alpha.select('section[id^="rider-"] h3')][:3] == [
            'Attributes', 'Identity', 'Stats',
        ]
        result_rows = []
        for row in alpha.select('#rider-results > .card-body > .table-responsive > table > tbody > tr'):
            cells = row.find_all('td', recursive=False)
            race_name = cells[1].find('summary') or cells[1]
            result_rows.append([cells[0].get_text(strip=True), race_name.get_text(strip=True),
                                cells[2].get_text(strip=True), cells[3].get_text(strip=True)])
        assert result_rows == [
            ['2026-04-05', 'Stage Race', '2', '3:00:00'],
            ['2026-04-01', 'Profile Test Race', '4', '4:00:00'],
            ['2025-04-01', 'Previous Edition', '5', '5:00:00'],
        ]
        expansions = alpha.select('#rider-results details')
        assert len(expansions) == 2 and all(not expansion.has_attr('open') for expansion in expansions)
        stage_rows = [[cell.get_text(strip=True) for cell in row.select('td')]
                      for row in expansions[0].select('table > tbody > tr')]
        assert stage_rows == [
            ['2026-04-04', 'Opening Stage', '1', '1:00:00'],
            ['2026-04-05', 'Final Stage', '3', '2:00:00'],
        ]
        assert 'Mountain Stage' in expansions[1].get_text()
        alpha_unlinked = profile('alpha', 2)
        assert flags(alpha_unlinked) == [
            ('❌', 'PCS identity error. The check failed.'), ('🆔', 'Missing FirstCycling ID'),
        ]
        beta_unlinked = profile('beta', 1)
        assert pcs_slug(beta_unlinked) == 'view-beta-rider'
        assert pcs_dob(beta_unlinked) == 'Unknown'
        assert flags(beta_unlinked) == [
            ('⚠️', 'PCS identity unknown. No check result is recorded.'), ('🆔', 'Missing FirstCycling ID'),
        ]
        assert 'No FirstCycling ID' in beta_unlinked.select_one('#rider-results').get_text()
        assert not beta_unlinked.select('#rider-results tbody tr')
        beta_linked = profile('beta', 2)
        assert pcs_slug(beta_linked) == 'saved-slug'
        assert pcs_dob(beta_linked) == '2000-01-01'
        assert flags(beta_linked) == [('✅', 'PCS identity verified. DOB matches.')]

        with engines[0].begin() as conn:
            conn.execute(text('UPDATE rider SET birth_date=20010101 WHERE id=1'))
        assert flags(profile('alpha', 1)) == [('✅', 'PCS identity verified. DOB matches.')]

        with engines[1].begin() as conn:
            conn.execute(text("UPDATE rider SET pcs_identity_status='failed', pcs_identity_verified_override=TRUE WHERE id=2"))
        assert flags(profile('beta', 2)) == [('✅', 'PCS identity verified by manual override.')]
    finally:
        with app.app_context():
            for race_id in race_ids:
                db.session.delete(db.session.get(FirstCyclingRace, race_id))
            db.session.commit()


def test_rider_external_calls_require_read_permission_and_match_namespace(app, client, rider_views):
    (alpha, beta), _ = rider_views
    with client.session_transaction() as session:
        user_id = int(session['_user_id'])
    with app.app_context():
        db.session.add_all([
            Namespace(id=ns.id, slug=ns.slug, name=ns.name, database_name=ns.database_name)
            for ns in (alpha, beta)
        ])
        groups = [ExternalCallTaskGroup(namespace_id=namespace_id) for namespace_id in (alpha.id, beta.id, None)]
        db.session.add_all(groups)
        first, second, global_group = groups
        tasks = [
            ExternalCallTask(task_group=group, provider=provider, endpoint=endpoint, status=status,
                             related_to_table=table, related_to_id=related_id)
            for group, provider, endpoint, status, table, related_id in (
                (first, 'pcs', 'alpha-rider-call', 'retry_wait', 'rider', 1),
                (first, 'firstcycling', 'alpha-rider-audit', 'historical', 'rider', 1),
                (second, 'pcs', 'beta-rider-call', 'failed', 'rider', 1),
                (first, 'pcs', 'wrong-table-call', 'queued', 'team', 1),
                (global_group, 'pcs', 'global-rider-call', 'queued', 'rider', 1),
            )
        ]
        db.session.add_all(tasks)
        db.session.commit()
        group_ids = [group.id for group in groups]
        task_ids = [task.id for task in tasks]
    profile_url = '/n/view-alpha/riders/1'
    task_url = f'/n/view-alpha/admin/integrations/task-groups/{group_ids[0]}/tasks/{task_ids[0]}'
    try:
        denied = client.get(profile_url)
        assert denied.status_code == 200
        assert b'alpha-rider-call' not in denied.data
        assert b'rider-external-calls' not in denied.data
        assert client.get(task_url).status_code == 403

        with app.app_context():
            user = db.session.get(User, user_id)
            user.roles.append(Role.query.filter_by(name='integrations_manager').one())
            db.session.commit()

        response = client.get(profile_url)
        assert response.status_code == 200
        markup = BeautifulSoup(response.data, 'html.parser')
        section = markup.select_one('#rider-external-calls')
        assert section.find_previous_sibling('section')['id'] == 'rider-results'
        assert section.find_next_sibling('section')['id'] == 'rider-history'
        assert section.h3.get_text(strip=True) == 'External Calls'
        assert [row.code.get_text(strip=True) for row in section.select('tbody tr')] == [
            'alpha-rider-audit', 'alpha-rider-call',
        ]
        assert section.select_one(f'a[href="{task_url}"]')
        assert client.get(task_url).status_code == 200

        beta_page = client.get('/n/view-beta/riders/1')
        assert beta_page.status_code == 200
        assert b'beta-rider-call' in beta_page.data
        assert b'alpha-rider-call' not in beta_page.data
        empty = client.get('/n/view-alpha/riders/2')
        assert empty.status_code == 200
        assert b'No external calls recorded for this rider.' in empty.data
    finally:
        with app.app_context():
            ExternalCallTask.query.filter(ExternalCallTask.id.in_(task_ids)).delete()
            ExternalCallTaskGroup.query.filter(ExternalCallTaskGroup.id.in_(group_ids)).delete()
            Namespace.query.filter(Namespace.id.in_([alpha.id, beta.id])).delete()
            db.session.commit()


def test_rider_view_columns_filters_and_namespace_state(app, client, rider_views):
    namespaces, engines = rider_views
    with engines[0].begin() as conn:
        conn.execute(text('''
            UPDATE rider SET first_cycling_id=900001, birth_date=20010304, size_cm=181, weight_kg=72.5,
                             stat_fla=65, stat_mm=69, stat_dh=73, stat_hil=77, stat_tt=82
            WHERE id=1
        '''))
    page = client.get('/n/view-alpha/riders')
    assert page.status_code == 200
    markup = BeautifulSoup(page.data, 'html.parser')
    tabs = markup.select('nav[aria-label="Rider views"] button')
    assert [tab.get_text(strip=True) for tab in tabs] == ['Stats', 'Activity', 'Attribute']
    assert tabs[0]['aria-pressed'] == 'true'
    assert not markup.select('#rider-view-label')
    rider_row = markup.select_one('.riders-table a[href="/n/view-alpha/riders/1"]').find_parent('tr')
    stat_cells = {cell.get_text(strip=True): cell['class'] for cell in rider_row.select('.rider-stat')}
    for value, stat_range in {
        '65': 'range-low', '69': 'range-decent', '73': 'range-good',
        '77': 'range-very-good', '80': 'range-excellent', '82': 'range-elite',
    }.items():
        assert stat_range in stat_cells[value]
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
    response = client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'attributes'}, follow_redirects=True)
    assert response.status_code == 200
    markup = BeautifulSoup(response.data, 'html.parser')
    assert markup.select_one('nav[aria-label="Rider views"] button[aria-pressed="true"]').get_text(strip=True) == 'Attribute'
    assert [heading.get_text(strip=True) for heading in markup.select('.riders-table th')] == [
        'Name', 'Team', 'Country', 'Rider ID', 'FirstCycling ID', 'Birth Date', 'Height', 'Weight',
    ]
    rows = [[cell.get_text(strip=True) for cell in row.select('td')] for row in markup.select('.riders-table tbody tr')]
    attributes_by_rider = {row[0]: row[3:] for row in rows}
    assert attributes_by_rider['view-alpha Rider'] == ['1', '900001', '2001-03-04', '181.0 cm', '72.5 kg']
    assert attributes_by_rider['Untracked Rider'] == ['2', '-', '-', '-', '-']
    assert b'name="activity_status"' not in response.data and b'name="stat_min"' not in response.data
    assert b'<th class="col-stat">MO</th>' in client.get('/n/view-beta/riders').data
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


def test_rider_selection_creates_lists_in_the_selected_namespace(app, client, rider_views, monkeypatch):
    _, (alpha, beta) = rider_views
    path = '/n/view-beta/riders'
    create_url = '/n/view-beta/stats-editor/lists/create'
    with beta.begin() as conn:
        conn.execute(text("INSERT INTO rider (id, first_name, last_name, is_deleted) VALUES (3, 'Deleted', 'Rider', TRUE)"))

    markup = BeautifulSoup(client.get(path).data, 'html.parser')
    assert markup.select_one('button[name="list_type"][value="stat"]')['formaction'] == create_url
    assert {box['value'] for box in markup.select('.rider-select:not([disabled])')} == {'1', '2'}
    assert markup.select_one('.rider-select[value="3"][disabled]')

    for invalid_ids in ('', '1,999', '1,3', '1,-2', ','.join(['1'] * 251)):
        assert client.post(create_url, data={'rider_ids': invalid_ids}).location == path
    with beta.connect() as conn:
        assert conn.scalar(text('SELECT COUNT(*) FROM rider_list')) == 0
    created = client.post(create_url, data={'rider_ids': '1,2,1', 'list_type': 'stat'})
    assert created.location.startswith('/n/view-beta/riders/bulk-editor?')
    assert client.get(created.location).status_code == 200

    attribute_form = {'rider_ids': '2,1', 'list_type': 'attribute'}
    client.post(path, data={'action': 'set_view', 'view': 'attributes'})
    assert b'Bulk Edit Attributes' not in client.get(path).data
    assert client.post(create_url, data=attribute_form).status_code == 403
    with client.session_transaction() as session:
        user_id = int(session['_user_id'])
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name='admin').one()]
        db.session.commit()

    markup = BeautifulSoup(client.get(path).data, 'html.parser')
    assert markup.select_one('button[name="list_type"][value="attribute"]')['formaction'] == create_url
    assert markup.select_one('button[value="rider_identity_check"]')
    assert not markup.select_one('button[value="rider_activity_check"]')
    with monkeypatch.context() as patch:
        patch.setattr(Namespace, 'build_connection_string', lambda self: alpha.url)
        assert client.post(create_url, data=attribute_form).status_code == 302
    created = client.post(create_url, data=attribute_form)
    editor = client.get(created.location)
    assert editor.status_code == 200 and b'view-beta Rider' in editor.data and b'Untracked Rider' in editor.data
    with beta.connect() as conn:
        assert conn.execute(text("""
            SELECT list_type, array_agg(rider_id ORDER BY i.id)
            FROM rider_list l JOIN rider_list_item i ON i.rider_list_id = l.id
            GROUP BY l.id ORDER BY l.id
        """)).all() == [('stat', [1, 2]), ('attribute', [2, 1])]
    with alpha.connect() as conn:
        assert conn.scalar(text('SELECT COUNT(*) FROM rider_list')) == 0


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
