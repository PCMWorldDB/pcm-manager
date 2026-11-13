"""Rider views filter before pagination and keep namespace identities separate."""
import os
import json
import gzip
from time import perf_counter
from datetime import date, datetime, timedelta
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models import (
    ExternalCallTask, ExternalCallTaskGroup, FirstCyclingRace, FirstCyclingRaceResult,
    FirstCyclingRaceStage, FirstCyclingRider, Namespace, Role, User,
)
from webapp.models.ns.base import NamespaceBase
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.models.main.pcs import PcsRider
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


def test_rider_debug_shows_complete_namespace_row(app, client, rider_views):
    namespaces, engines = rider_views
    for namespace, engine in zip(namespaces, engines):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rider ADD COLUMN debug_extra text"))
            conn.execute(text("UPDATE rider SET debug_extra=:value WHERE id=1"),
                         {'value': f'<script>{namespace.slug}</script>'})
            expected = conn.execute(text('SELECT to_jsonb(r) FROM rider r WHERE id=1')).scalar_one()
        response = client.get(f'/n/{namespace.slug}/riders/1')
        assert response.status_code == 200
        markup = BeautifulSoup(response.data, 'html.parser')
        button = markup.select_one('#rider-attributes button[aria-label="Debug rider attributes"]')
        assert button['data-bs-toggle'] == 'modal'
        assert button['data-bs-target'] == '#rider-debug-modal'
        assert button.select_one('svg[aria-hidden="true"]')
        modal = markup.select_one('#rider-debug-modal')
        assert namespace.slug in modal.select_one('#rider-debug-title').get_text()
        assert [tab.get_text(strip=True) for tab in modal.select('[role="tab"]')] == ['CDB']
        actual = {row.th.get_text(strip=True): json.loads(row.td.get_text())
                  for row in modal.select('#rider-debug-table tbody tr')}
        assert actual == expected
        assert not modal.select('script')
        assert actual['stat_mo'] == (80 if namespace.slug == 'view-alpha' else 70)
    assert client.get('/n/missing/riders/1').status_code == 404
    assert client.get('/n/view-alpha/riders/999999').status_code == 404
    with client.session_transaction() as session:
        user_id = session['_user_id']
    with app.app_context():
        user = db.session.get(User, int(user_id))
        for role in user.roles.all():
            user.roles.remove(role)
        db.session.commit()
    assert client.get('/n/view-alpha/riders/1').status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.get('/n/view-alpha/riders/1').status_code == 302


def test_rider_debug_tabs_show_only_exact_linked_profiles(app, client, rider_views):
    namespaces, engines = rider_views
    fc_ids = [990071, 990072]
    slugs = ['debug-' + uuid4().hex for _ in namespaces]
    with app.app_context():
        for fc_id, slug in zip(fc_ids, slugs):
            db.session.add(FirstCyclingRider(rider_id=fc_id, name=slug, birth_date=date(2000, 1, 2)))
            db.session.add(PcsRider(rider_slug=slug, profile_url=f'https://www.procyclingstats.com/rider/{slug}',
                                    birth_date=date(2001, 2, 3), source_payload={'raw': '<script>test</script>'}))
        db.session.commit()
    try:
        for namespace, engine, fc_id, slug in zip(namespaces, engines, fc_ids, slugs):
            with engine.begin() as conn:
                conn.execute(text('UPDATE rider SET first_cycling_id=:fc, pcs_slug=:pcs WHERE id=1'),
                             {'fc': fc_id, 'pcs': slug})
            page = BeautifulSoup(client.get(f'/n/{namespace.slug}/riders/1').data, 'html.parser')
            modal = page.select_one('#rider-debug-modal')
            tabs = modal.select('[role="tab"]')
            assert [tab.get_text(strip=True) for tab in tabs] == ['CDB', 'FC', 'PCS']
            assert [tab['aria-selected'] for tab in tabs] == ['true', 'false', 'false']
            for tab in tabs:
                panel = modal.select_one(tab['data-bs-target'])
                assert panel['aria-labelledby'] == tab['id']
            with app.app_context():
                expected_fc = db.session.execute(text('SELECT row_to_json(p) FROM firstcycling_riders p WHERE rider_id=:id'),
                                                 {'id': fc_id}).scalar_one()
                expected_pcs = db.session.execute(text('SELECT row_to_json(p) FROM pcs_riders p WHERE rider_slug=:slug'),
                                                  {'slug': slug}).scalar_one()
            for label, expected in [('fc', expected_fc), ('pcs', expected_pcs)]:
                actual = {row.th.get_text(strip=True): json.loads(row.td.get_text())
                          for row in modal.select(f'#rider-debug-{label} tbody tr')}
                for key in ('created_at', 'updated_at', 'last_synced_at'):
                    if expected.get(key) is not None:
                        actual[key] = datetime.fromisoformat(actual[key])
                        expected[key] = datetime.fromisoformat(expected[key])
                assert actual == expected
            assert not modal.select('script')
            assert 'show' in modal.select_one('#rider-debug-cdb')['class']
            unlinked = BeautifulSoup(client.get(f'/n/{namespace.slug}/riders/2').data, 'html.parser')
            assert [tab.get_text(strip=True) for tab in unlinked.select('#rider-debug-modal [role="tab"]')] == ['CDB']
    finally:
        with app.app_context():
            FirstCyclingRider.query.filter(FirstCyclingRider.rider_id.in_(fc_ids)).delete(synchronize_session=False)
            PcsRider.query.filter(PcsRider.rider_slug.in_(slugs)).delete(synchronize_session=False)
            db.session.commit()


@pytest.mark.parametrize('fc_id,status,override,slug,pcs_dob,expected', [
    (900001, 'verified', False, 'saved-slug', '2000-01-01', '🟢'),
    (900001, 'unknown', False, 'saved-slug', None, '🔴'),
    (900001, 'review', False, 'saved-slug', None, '🟡'),
    (900001, 'verified', False, 'saved-slug', '2001-01-01', '🟡'),
    (900001, 'verified', False, '0', '2000-01-01', '🔴'),
    (900001, 'failed', False, 'saved-slug', None, '🔴'),
    (None, 'verified', False, 'saved-slug', '2000-01-01', '🔴'),
    (0, 'unknown', False, 'saved-slug', None, '🔴'),
    (None, 'failed', False, 'saved-slug', None, '🔴'),
    (900001, 'failed', True, 'saved-slug', None, '🟢'),
    (None, 'failed', True, 'saved-slug', None, '🔴'),
])
def test_rider_profile_overall_identity_flag(client, rider_views, monkeypatch, fc_id, status, override, slug, pcs_dob, expected):
    _, engines = rider_views
    monkeypatch.setattr(routes, '_rider_pcs_activity', lambda slug: [
        ('Days since last race', 10), ('Last PCS sync (UTC)', '2026-11-13 12:00:00'),
    ])
    with engines[0].begin() as conn:
        conn.execute(text('''UPDATE rider SET first_cycling_id=:fc, pcs_identity_status=:status,
            pcs_identity_verified_override=:override, pcs_slug=:slug, birth_date=20000101,
            pcs_birth_date=:dob WHERE id=1'''),
                     dict(fc=fc_id, status=status, override=override, slug=slug, dob=pcs_dob))
    response = client.get('/n/view-alpha/riders/1')
    assert response.status_code == 200
    markup = BeautifulSoup(response.data, 'html.parser')
    flag = markup.select_one('#rider-name-title #rider-overall-flag')
    assert flag.get_text(strip=True) == expected
    assert markup.select_one('#rider-identity-flag').get_text(strip=True) == expected
    assert flag['role'] == 'img' and flag['tabindex'] == '0' and flag['aria-label']
    assert not markup.select('#rider-name-title #rider-pending-approval-flag')
    assert markup.select_one('#rider-identity-title #rider-identity-flag')


@pytest.mark.parametrize('days,checked,expected', [
    (0, True, '🟢'), (199, True, '🟢'), (200, True, '🟡'), (360, True, '🟡'),
    (361, True, '🔴'), (365, True, '🔴'), (366, True, '🔴'),
    (None, True, '🔴'), (-1, True, '🔴'), (10, False, '🔴'),
])
def test_rider_latest_activity_flags(app, client, rider_views, days, checked, expected):
    _, engines = rider_views
    slug = 'activity-flags-' + uuid4().hex
    with engines[0].begin() as conn:
        conn.execute(text('''UPDATE rider SET first_cycling_id=900001, pcs_slug=:slug,
            birth_date=20000101, pcs_birth_date='2000-01-01', pcs_identity_status='verified' WHERE id=1'''),
                     {'slug': slug})
    if checked:
        with app.app_context():
            profile = PcsRider(
                rider_slug=slug, profile_url=f'https://www.procyclingstats.com/rider/{slug}',
                latest_participation_date=date.today() - timedelta(days=days) if days is not None else None,
                last_synced_at=datetime.utcnow(),
            )
            db.session.add(profile)
            db.session.commit()
    try:
        response = client.get('/n/view-alpha/riders/1')
        assert response.status_code == 200
        markup = BeautifulSoup(response.data, 'html.parser')
        assert markup.select_one('#rider-results-summary-title').get_text(strip=True) == 'Latest Activity' + expected
        flag = markup.select_one('#rider-activity-flag')
        assert flag['role'] == 'img' and flag['tabindex'] == '0' and flag['aria-label']
        assert markup.select_one('#rider-overall-flag').get_text(strip=True) == expected
        assert markup.select_one('#rider-identity-flag').get_text(strip=True) == '🟢'
        beta = BeautifulSoup(client.get('/n/view-beta/riders/1').data, 'html.parser')
        assert beta.select_one('#rider-activity-flag').get_text(strip=True) == '🔴'
    finally:
        with app.app_context():
            PcsRider.query.filter_by(rider_slug=slug).delete()
            db.session.commit()


@pytest.mark.parametrize('identity,days,expected', [
    ('verified', 10, '🟢'), ('verified', 200, '🟡'), ('verified', 361, '🔴'),
    ('review', 10, '🟡'), ('review', 200, '🟡'), ('review', 361, '🔴'),
    ('unknown', 10, '🔴'), ('unknown', 200, '🔴'), ('unknown', 361, '🔴'),
])
def test_rider_overall_flag_combines_identity_and_activity(client, rider_views, monkeypatch, identity, days, expected):
    _, engines = rider_views
    with engines[0].begin() as conn:
        conn.execute(text('''UPDATE rider SET first_cycling_id=900001, pcs_slug='saved-slug',
            birth_date=20000101, pcs_identity_status=:status, pcs_birth_date=:dob WHERE id=1'''),
                     {'status': identity, 'dob': '2000-01-01' if identity == 'verified' else '2001-01-01'})
    monkeypatch.setattr(routes, '_rider_pcs_activity', lambda slug: [
        ('Days since last race', days), ('Last PCS sync (UTC)', '2026-11-13 12:00:00'),
    ])
    markup = BeautifulSoup(client.get('/n/view-alpha/riders/1').data, 'html.parser')
    assert markup.select_one('#rider-overall-flag').get_text(strip=True) == expected


def test_rider_profile_pending_approval_flag_is_namespace_scoped(client, rider_views):
    _, engines = rider_views

    def pending(slug, rider_id=1):
        response = client.get(f'/n/view-{slug}/riders/{rider_id}')
        assert response.status_code == 200
        return BeautifulSoup(response.data, 'html.parser').select_one('#rider-name-title #rider-pending-approval-flag')

    assert pending('alpha') is None
    with engines[0].begin() as conn:
        conn.execute(text('''INSERT INTO stat_changes
            (rider_id,stat_list_id,stat_name,original_value,final_value,delta_value,is_confilict_override,is_approved)
            VALUES (1,123,'mo',70,71,1,FALSE,FALSE)'''))
    assert pending('alpha').get_text(strip=True) == '⏳'
    assert pending('beta') is None
    assert pending('alpha', 2) is None
    with engines[0].begin() as conn:
        conn.execute(text('UPDATE stat_changes SET is_approved=TRUE'))
    assert pending('alpha') is None
    with engines[0].begin() as conn:
        conn.execute(text('''INSERT INTO cdb_changes
            (entity_type,entity_id,attribute_name,operation,source,status)
            VALUES ('rider',1,'weight_kg','update','attribute_editor','pending')'''))
    assert pending('alpha').get_text(strip=True) == '⏳'
    assert pending('beta') is None
    with engines[0].begin() as conn:
        conn.execute(text("UPDATE cdb_changes SET status='rejected'"))
    assert pending('alpha') is None
    with engines[0].begin() as conn:
        conn.execute(text("UPDATE cdb_changes SET status='approved', applied_at=NOW()"))
    assert pending('alpha') is None


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
                for flag in (markup.select('#rider-pcs-identifier .rider-flag[role="img"][tabindex="0"]')
                             + markup.select('#rider-firstcycling-identifier .rider-flag[role="img"][tabindex="0"]'))]

    def pcs_slug(markup):
        label = markup.select_one('#rider-identifiers').find(class_='object-attribute-label', string='PCS')
        return label.find_next_sibling(class_='object-attribute-value').get_text(strip=True)

    def pcs_dob(markup):
        label = markup.select_one('#rider-identity').find(class_='object-attribute-label', string='PCS Date of Birth')
        return label.find_next_sibling(class_='object-attribute-value').get_text(strip=True)

    try:
        alpha = profile('alpha', 1)
        assert not alpha.find('a', string='Shared identities')
        assert alpha.select_one('#rider-name-title > span').get_text(strip=True) == 'Alice Smith'
        assert pcs_slug(alpha) == 'alice-smith'
        identity = alpha.select_one('#rider-identity')
        assert [label.get_text(strip=True) for label in identity.select('.object-attribute-label')] == [
            'PCS Identity Status', 'PCS Date of Birth', 'PCS Identity Verified Override', 'Last PCS sync (UTC)',
        ]
        identifiers = alpha.select_one('#rider-identifiers')
        assert not identifiers.select('.attribute-inline-editor')  # Stat editors cannot edit identity attributes.
        assert identifiers.select_one('#rider-pcs-identifier .rider-flag')['aria-label'] == (
            'PCS identity needs review. A birth date is missing or the dates differ.'
        )
        assert not identifiers.select('#rider-firstcycling-identifier .rider-flag')
        assert identity.select('.object-attribute-value')[0].get_text(strip=True) == 'Needs review'
        assert identity.select('.object-attribute-value')[2].get_text(strip=True) == 'No'
        assert identity.select_one('#rider-identity-title #rider-identity-flag')
        assert pcs_dob(alpha) == '2001-01-01'
        assert not alpha.select('#rider-attributes [aria-label="Edit PCS Identity Verified Override"]')
        assert alpha.select_one('#rider-attributes .object-attribute-label').get_text(strip=True) == 'Team'
        assert not alpha.select('#rider-attributes .rider-flag')
        assert flags(alpha) == [('🟡', 'PCS identity needs review. A birth date is missing or the dates differ.')]
        columns = alpha.select('#rider-details .row > .col-12')
        assert [column['id'] for column in columns] == ['rider-attributes', 'rider-identity', 'rider-results-summary']
        assert [label.get_text(strip=True) for label in identifiers.select('.object-attribute-label')] == [
            'FirstCycling', 'PCS',
        ]
        assert not alpha.find('a', string='Open PCS')
        assert not alpha.find('a', string='Open FirstCycling')
        for label, href in [
            ('alice-smith', 'https://www.procyclingstats.com/rider/alice-smith'),
            ('900001', 'https://firstcycling.com/rider.php?r=900001'),
        ]:
            link = identifiers.find('a', string=label)
            assert link['href'] == href
            assert link['target'] == '_blank'
            assert {'noopener', 'noreferrer'} <= set(link['rel'])
        assert [section['id'] for section in alpha.select('section[id^="rider-"]')] == [
            'rider-details', 'rider-stats', 'rider-review', 'rider-results',
        ]
        assert alpha.select_one('#rider-review #rider-history') is not None
        assert alpha.select_one('#rider-review-title').get_text(strip=True) == 'Review (0)'
        tabs = alpha.select('nav[aria-label="Rider details"] [role="tab"]')
        assert [tab.get_text(strip=True) for tab in tabs] == ['Review (0)', 'Results']
        assert [tab['aria-selected'] for tab in tabs] == ['true', 'false']
        for tab in tabs:
            panel = alpha.select_one(tab['data-bs-target'])
            assert panel['role'] == 'tabpanel' and panel['aria-labelledby'] == tab['id']
        assert 'active' in alpha.select_one('#rider-review')['class']
        assert 'active' not in alpha.select_one('#rider-results')['class']
        assert [heading.find(string=True, recursive=False).strip() for heading in alpha.select('section[id^="rider-"] h3')][:4] == [
            'Attributes', 'Identity', 'Latest Activity', 'Stats',
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
        assert alpha_unlinked.select_one('#rider-firstcycling-identifier .rider-flag')['aria-label'] == (
            'Missing FirstCycling ID'
        )
        assert not alpha_unlinked.select('#rider-firstcycling-identifier a')
        assert flags(alpha_unlinked) == [
            ('🔴', 'PCS identity error. The check failed.'), ('🔴', 'Missing FirstCycling ID'),
        ]
        beta_unlinked = profile('beta', 1)
        assert pcs_slug(beta_unlinked) == 'view-beta-rider'
        assert pcs_dob(beta_unlinked) == 'Unknown'
        assert flags(beta_unlinked) == [
            ('🔴', 'PCS identity unknown. No check result is recorded.'), ('🔴', 'Missing FirstCycling ID'),
        ]
        assert 'No FirstCycling ID' in beta_unlinked.select_one('#rider-results').get_text()
        assert not beta_unlinked.select('#rider-results tbody tr')
        beta_linked = profile('beta', 2)
        assert beta_linked.select_one('#rider-pcs-identifier a')['href'] == (
            'https://www.procyclingstats.com/rider/saved-slug'
        )
        assert pcs_slug(beta_linked) == 'saved-slug'
        assert pcs_dob(beta_linked) == '2000-01-01'
        assert flags(beta_linked) == [('🟢', 'PCS identity verified. DOB matches.')]

        with engines[0].begin() as conn:
            conn.execute(text('UPDATE rider SET birth_date=20010101 WHERE id=1'))
        assert flags(profile('alpha', 1)) == [('🟢', 'PCS identity verified. DOB matches.')]

        with engines[1].begin() as conn:
            conn.execute(text("UPDATE rider SET pcs_identity_status='failed', pcs_identity_verified_override=TRUE WHERE id=2"))
        assert flags(profile('beta', 2)) == [('🟢', 'PCS identity verified by manual override.')]
    finally:
        with app.app_context():
            for race_id in race_ids:
                db.session.delete(db.session.get(FirstCyclingRace, race_id))
            db.session.commit()


def test_profile_results_show_exact_saved_pcs_activity(app, client, rider_views):
    _, engines = rider_views
    profiles = []
    with app.app_context():
        for slug, race in [('profile-alpha-test', 'Alpha PCS Race'), ('profile-beta-test', 'Beta PCS Race'),
                           ('untracked-rider', 'Unmapped PCS Race')]:
            profiles.append(PcsRider(
                rider_slug=slug, profile_url=f'https://www.procyclingstats.com/rider/{slug}',
                latest_participation_year=2026, latest_participation_date=date(2026, 4, 3),
                latest_participation_race_name=race, latest_participation_race_category='2.Pro',
                latest_participation_race_type='UCI',
                latest_participation_stage_number=0, latest_participation_stage_name='Prologue',
                latest_participation_result_position='DNF', latest_participation_status='confirmed',
                latest_participation_confirmed=False,
                last_synced_at=datetime(2026, 4, 4, 12, 30),
            ))
        profiles[1].latest_participation_date = date.today() + timedelta(days=1)
        profiles[1].latest_participation_race_type = None
        profiles[1].latest_participation_race_category = None
        profiles[1].latest_participation_stage_number = None
        profiles[1].latest_participation_stage_name = None
        db.session.add_all(profiles)
        db.session.commit()
        ids = [profile.id for profile in profiles]
    try:
        for engine, slug in zip(engines, ['profile-alpha-test', 'profile-beta-test']):
            with engine.begin() as conn:
                conn.execute(text('UPDATE rider SET pcs_slug=:slug WHERE id=1'), {'slug': slug})
        alpha = BeautifulSoup(client.get('/n/view-alpha/riders/1').data, 'html.parser')
        activity = alpha.select_one('#rider-details #rider-results-summary #rider-pcs-activity')
        assert not alpha.select('#rider-results #rider-pcs-activity')
        values = {item.select_one('.object-attribute-label').get_text(strip=True):
                  item.select_one('.object-attribute-value').get_text(strip=True)
                  for item in activity.select('.object-attribute')}
        assert values == {
            'Days since last race': str((date.today() - date(2026, 4, 3)).days),
            'Last race date': '2026-04-03',
            'Last race': 'UCI 2.Pro - Alpha PCS Race',
            'Stage': '0 - Prologue',
            'Position': 'DNF',
            'Participation status': 'confirmed',
        }
        identity = alpha.select_one('#rider-identity')
        sync_label = identity.find(class_='object-attribute-label', string='Last PCS sync (UTC)')
        assert sync_label.find_next_sibling().get_text(strip=True) == '2026-04-04 12:30:00'
        assert 'Beta PCS Race' not in alpha.get_text()
        beta = client.get('/n/view-beta/riders/1')
        assert b'Beta PCS Race' in beta.data and b'Alpha PCS Race' not in beta.data
        beta_activity = BeautifulSoup(beta.data, 'html.parser').select_one('#rider-pcs-activity')
        beta_values = {item.select_one('.object-attribute-label').get_text(strip=True):
                       item.select_one('.object-attribute-value').get_text(strip=True)
                       for item in beta_activity.select('.object-attribute')}
        assert beta_values['Days since last race'] == '—'
        assert beta_values['Last race'] == 'Beta PCS Race'
        assert beta_values['Stage'] == '—'
        unmapped = client.get('/n/view-alpha/riders/2')
        assert b'No PCS racing activity recorded' in unmapped.data
        assert b'Unmapped PCS Race' not in unmapped.data
    finally:
        with app.app_context():
            PcsRider.query.filter(PcsRider.id.in_(ids)).delete(synchronize_session=False)
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
        tabs = markup.select('nav[aria-label="Rider details"] [role="tab"]')
        assert [tab.get_text(strip=True) for tab in tabs] == ['Review (0)', 'Results', 'External Calls']
        assert [panel['id'] for panel in markup.select('section[role="tabpanel"]')] == [
            'rider-review', 'rider-results', 'rider-external-calls',
        ]
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
    assert not markup.select('nav[aria-label="Rider views"]')
    assert '-riders-table-2' in markup.select_one('script[src*="js/rider_table.js"]')['src']
    assert '-riders-drag-2' in markup.select_one('script[src*="js/rider_drag_selection.js"]')['src']
    assert [option.get_text(strip=True) for option in markup.select('#rider-view option')] == [
        'Stats', 'Activity', 'Attributes', 'Identity',
    ]
    assert markup.select_one('#rider-view option[selected]')['value'] == 'stats'
    filter_names = {field.get('name') for field in markup.select('#riders-filters-form input, #riders-filters-form select')}
    assert {'stat_min', 'stat_max', 'activity_status', 'identity_flag'} <= filter_names
    rider_row = markup.select_one('.riders-table a[href="/n/view-alpha/riders/1"]').find_parent('tr')
    stat_cells = {cell.get_text(strip=True): cell['class'] for cell in rider_row.select('.rider-stat')}
    for value, stat_range in {
        '65': 'range-low', '69': 'range-decent', '73': 'range-good',
        '77': 'range-very-good', '80': 'range-excellent', '82': 'range-elite',
    }.items():
        assert stat_range in stat_cells[value]
    assert b'<th class="col-stat">MO</th>' in page.data
    assert b'name="stat_min"' in page.data and b'name="activity_status"' in page.data
    assert b'<th>Last Race</th>' not in page.data
    response = client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'activity'})
    assert response.location.endswith('/n/view-alpha/riders')
    page = client.get(response.location)
    assert b'view-alpha race' in page.data and b'view-beta race' not in page.data
    assert b'name="activity_status"' in page.data and b'name="stat_min"' in page.data
    assert b'<th class="col-stat">MO</th>' not in page.data
    client.post('/n/view-alpha/riders', data={'activity_status': 'recent'})
    page = client.get('/n/view-alpha/riders')
    table_text = BeautifulSoup(page.data, 'html.parser').select_one('.riders-table tbody').get_text()
    assert 'view-alpha Rider' in table_text and 'Untracked Rider' not in table_text
    # The second namespace keeps Stats as its default despite overlapping rider IDs.
    page = client.get('/n/view-beta/riders')
    assert b'<th class="col-stat">MO</th>' in page.data
    client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'stats'})
    client.post('/n/view-alpha/riders', data={'stat_key': 'mo', 'stat_min': '75'})
    page = client.get('/n/view-alpha/riders')
    table_text = BeautifulSoup(page.data, 'html.parser').select_one('.riders-table tbody').get_text()
    assert 'view-alpha Rider' in table_text and 'Untracked Rider' not in table_text
    response = client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'attributes'}, follow_redirects=True)
    assert response.status_code == 200
    markup = BeautifulSoup(response.data, 'html.parser')
    assert markup.select_one('#rider-view option[selected]')['value'] == 'attributes'
    assert {field.get('name') for field in markup.select('#riders-filters-form input, #riders-filters-form select')} == filter_names
    assert [heading.get_text(strip=True) for heading in markup.select('.riders-table th')] == [
        'Select', 'Name', 'Team', 'Country', 'Birth Date', 'Height', 'Weight',
    ]
    rows = [[cell.get_text(strip=True) for cell in row.select('td')] for row in markup.select('.riders-table tbody tr')]
    attributes_by_rider = {row[1]: row[4:] for row in rows}
    assert attributes_by_rider['view-alpha Rider'] == ['2001-03-04', '181.0 cm', '72.5 kg']
    assert 'Untracked Rider' not in attributes_by_rider  # Filters remain active in Attributes.
    assert b'name="activity_status"' in response.data and b'name="stat_min"' in response.data
    assert b'<th class="col-stat">MO</th>' in client.get('/n/view-beta/riders').data
    with app.app_context():
        alpha = routes._load_namespace_roster_riders(namespaces[0], {'view': 'activity', 'activity_status': 'recent'})
        beta = routes._load_namespace_roster_riders(namespaces[1], {'view': 'activity', 'activity_status': 'recent'})
        assert alpha[1] == 1 and beta[1] == 0
    assert client.post('/n/view-alpha/riders', data={'stat_key': 'mo', 'stat_min': '100'}).status_code == 400
    assert client.post('/n/view-alpha/riders', data={'stat_key': 'not_a_stat'}).status_code == 400
    assert client.get('/n/missing/riders').status_code == 404


def test_identity_review_multiselect_flags_and_pagination(app, client, rider_views):
    _, (alpha, _) = rider_views
    with alpha.begin() as conn:
        conn.execute(text("""UPDATE rider SET first_cycling_id=11, birth_date=20000101,
            pcs_birth_date='2000-01-01', pcs_identity_status='verified' WHERE id=1"""))
        conn.execute(text("""INSERT INTO rider
            (id, first_name, last_name, first_cycling_id, birth_date, pcs_birth_date,
             pcs_identity_status, pcs_identity_verified_override, is_deleted, pcs_slug)
            VALUES (3,'Needs','Review',13,20000101,'2001-01-01','review',FALSE,FALSE,'needs-review'),
                   (4,'Manual','Override',14,NULL,NULL,'failed',TRUE,FALSE,'manual-override'),
                   (5,'Failed','Check',15,NULL,NULL,'failed',FALSE,FALSE,'failed-check'),
                   (6,'Deleted','Rider',16,NULL,NULL,'review',FALSE,TRUE,'deleted-rider'),
                   (7,'Missing','FC',NULL,20000101,'2000-01-01','verified',FALSE,FALSE,'missing-fc'),
                   (8,'Stale','DOB',18,20010101,'2000-01-01','verified',FALSE,FALSE,'stale-dob')"""))
    path = '/n/view-alpha/riders/review'

    def page_ids(**filters):
        response = client.get(path, query_string={'tab': 'identity', **filters})
        assert response.status_code == 200
        markup = BeautifulSoup(response.data, 'html.parser')
        return markup, {int(row.select('td')[1].get_text()) for row in markup.select('#identity-review-table tbody tr')}

    markup, ids = page_ids()
    assert ids == {2, 3, 5, 7, 8}
    selected = markup.select('input[name="flag"][checked]')
    assert {field['value'] for field in selected} == {'review', 'failed', 'unknown', 'missing_fc'}
    assert len(markup.select('input[type="checkbox"][name="flag"]')) == 6
    assert markup.select_one('a[href$="/riders/3"]')
    assert not markup.select('#identity-review-table .btn[href]')
    assert page_ids(flag=['review', 'failed'])[1] == {3, 5, 8}
    assert page_ids(flag=['verified'])[1] == {1, 7}
    assert page_ids(flag=['override'])[1] == {4}
    assert page_ids(flag=['missing_fc', 'unknown', 'missing_fc'])[1] == {2, 7}
    assert page_ids(flag=['review'], q='Needs')[1] == {3}
    assert page_ids(filtered='1')[1] == set()
    assert client.get(path, query_string={'tab': 'identity', 'flag': ['review', 'invalid']}).status_code == 400
    beta = client.get('/n/view-beta/riders/review?tab=identity')
    assert beta.status_code == 200 and b'Needs' not in beta.data and b'view-alpha' not in beta.data
    with alpha.begin() as conn:
        conn.execute(text("""INSERT INTO rider (id,first_name,last_name,first_cycling_id)
            SELECT n,'Unknown','Rider',10000+n FROM generate_series(20,79) n"""))
    markup, ids = page_ids(flag=['unknown'], page=2)
    assert len(ids) == 11
    assert '61 riders' in markup.get_text()
    previous = markup.select_one('nav[aria-label="Identity review pages"] a')
    assert parse_qs(urlsplit(previous['href']).query)['flag'] == ['unknown']
    assert client.get(path, query_string={'tab': 'identity', 'flag': 'unknown', 'page': 99}).status_code == 302


def test_identity_review_actions_enforce_permissions_and_namespace(app, client, rider_views, monkeypatch):
    namespaces, (alpha, _) = rider_views
    with alpha.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_slug='alpha-only' WHERE id=1"))
        conn.execute(text('UPDATE rider SET is_deleted=TRUE WHERE id=2'))
    with client.session_transaction() as session:
        user_id = int(session['_user_id'])
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name='viewer').one()]
        db.session.commit()
    path = '/n/view-alpha/riders/review?tab=identity'
    calls = []
    monkeypatch.setattr(routes, 'create_manual_external_group', lambda *args, **kwargs: calls.append((args, kwargs)))
    markup = BeautifulSoup(client.get(path).data, 'html.parser')
    assert not markup.select('#identity-review-table form')
    check_url = '/n/view-alpha/riders/1/check-identity'
    assert client.post(check_url, data={'return_to': 'identity_review'}).status_code == 403
    assert not calls
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name='admin').one()]
        db.session.commit()
    markup = BeautifulSoup(client.get(path).data, 'html.parser')
    form = markup.select_one(f'form[action="{check_url}"]')
    assert form and form.select_one('input[name="csrf_token"]')
    response = client.post(check_url, data={
        'return_to': 'identity_review', 'return_flag': ['unknown', 'missing_fc'], 'return_q': 'alpha', 'return_page': '2',
    })
    assert urlsplit(response.location).path == '/n/view-alpha/riders/review'
    assert parse_qs(urlsplit(response.location).query) == {
        'tab': ['identity'], 'filtered': ['1'], 'flag': ['unknown', 'missing_fc'], 'q': ['alpha'], 'page': ['2'],
    }
    args, kwargs = calls[0]
    assert kwargs['namespace_id'] == namespaces[0].id
    assert args[0][0]['params']['rider_slug'] == 'alpha-only'
    assert args[0][0]['related_to_id'] == 1
    assert client.post(check_url, data={'return_to': 'identity_review', 'return_flag': 'invalid'}).status_code == 400
    assert client.post('/n/view-alpha/riders/2/check-identity').status_code == 404
    assert client.get('/n/missing/riders/review?tab=identity').status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(Namespace, 'build_connection_string', lambda self: alpha.url)
        assert client.post('/n/view-beta/riders/1/check-identity').status_code == 404
    assert len(calls) == 1
    with monkeypatch.context() as patch:
        original = routes._can_access_panel
        patch.setattr(routes, '_can_access_panel', lambda panel: panel != 'team_manager' and original(panel))
        assert client.get(path).status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.get(path).status_code == 302


def test_identity_view_mappings_flags_and_filters(app, client, rider_views):
    _, engines = rider_views
    with app.app_context():
        model = routes.IDENTITY_MODELS['rider']
        identity = model(firstcycling_id=987654, pcs_slug='identity-test-rider')
        db.session.add(identity)
        db.session.commit()
        identity_id = identity.id
    try:
        with engines[0].begin() as conn:
            conn.execute(text("""UPDATE rider SET first_cycling_id=987654,
                pcs_slug='identity-test-rider', birth_date=20010304,
                pcs_birth_date='2001-03-04', pcs_identity_status='verified' WHERE id=1"""))
            conn.execute(text("UPDATE rider SET pcs_identity_status='failed' WHERE id=2"))
        response = client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'identity'}, follow_redirects=True)
        assert response.status_code == 200
        markup = BeautifulSoup(response.data, 'html.parser')
        assert [heading.get_text(strip=True) for heading in markup.select('.riders-table th')] == [
            'Select', 'Name', 'Team', 'Country', 'PCS', 'Identity ID', 'FirstCycling ID', 'DOB', 'Flags',
        ]
        row = markup.select_one('.riders-table a[href="/n/view-alpha/riders/1"]').find_parent('tr')
        assert [cell.get_text(strip=True) for cell in row.select('td')][4:8] == [
            'identity-test-rider', str(identity_id), '987654', '2001-03-04',
        ]
        assert row.select_one('[aria-label="PCS identity verified"]')
        for flag, expected in [('verified', 'view-alpha Rider'), ('failed', 'Untracked Rider'),
                               ('missing_fc', 'Untracked Rider')]:
            response = client.post('/n/view-alpha/riders', data={'identity_flag': flag}, follow_redirects=True)
            markup = BeautifulSoup(response.data, 'html.parser')
            assert len(markup.select('.riders-table tbody tr')) == 1
            assert expected in markup.select_one('.riders-table tbody').get_text()
        # A stale verified status is shown and filtered as review after a DOB change.
        with engines[0].begin() as conn:
            conn.execute(text('UPDATE rider SET birth_date=20020304 WHERE id=1'))
        response = client.post('/n/view-alpha/riders', data={'identity_flag': 'review'}, follow_redirects=True)
        table_text = BeautifulSoup(response.data, 'html.parser').select_one('.riders-table tbody').get_text()
        assert 'view-alpha Rider' in table_text and 'Untracked Rider' not in table_text
        with engines[0].begin() as conn:
            conn.execute(text('UPDATE rider SET pcs_identity_verified_override=TRUE WHERE id=1'))
        response = client.post('/n/view-alpha/riders', data={'identity_flag': 'override'}, follow_redirects=True)
        assert b'view-alpha Rider' in response.data and b'Verified by manual override' in response.data
        with engines[0].begin() as conn:
            conn.execute(text("UPDATE rider SET pcs_slug='conflicting-slug' WHERE id=1"))
            conn.execute(text("""INSERT INTO rider (id, first_name, last_name, pcs_identity_status)
                SELECT n, 'Other', 'Rider', 'unknown' FROM generate_series(3, 35) n"""))
        response = client.get('/n/view-alpha/riders')
        assert b'Mapping conflict' in response.data
        # Count and pagination use only matching flags, even with more than a page of other riders.
        with app.app_context():
            matches = routes._load_namespace_roster_riders(
                rider_views[0][0], {'view': 'identity', 'identity_flag': 'override'}, per_page=25)
            assert matches[1] == 1 and [rider.rider_id for rider in matches[0]] == [1]
        response = client.post('/n/view-alpha/riders', data={'identity_flag': 'unknown'}, follow_redirects=True)
        table_text = BeautifulSoup(response.data, 'html.parser').select_one('.riders-table tbody').get_text()
        assert 'Other Rider' in table_text and 'view-alpha Rider' not in table_text
        # Namespace-local IDs overlap, but neither view state nor values leak.
        assert b'<th class="col-stat">MO</th>' in client.get('/n/view-beta/riders').data
        client.post('/n/view-beta/riders', data={'action': 'set_view', 'view': 'identity'})
        response = client.get('/n/view-beta/riders')
        assert b'identity-test-rider' not in response.data
        assert client.post('/n/view-alpha/riders', data={'identity_flag': 'invalid'}).status_code == 400
    finally:
        with app.app_context():
            db.session.delete(db.session.get(model, identity_id))
            db.session.commit()


def test_activity_view_checks_permission_on_direct_requests(app, client, rider_views, monkeypatch):
    original = routes._can_access_panel
    monkeypatch.setattr(routes, '_can_access_panel', lambda panel: panel != 'activity_monitor' and original(panel))
    assert client.get('/n/view-alpha/riders').status_code == 200
    assert client.post('/n/view-alpha/riders', data={'action': 'set_view', 'view': 'activity'}).status_code == 403
    with client.session_transaction() as session:
        session['riders_state_by_namespace'] = {'70000': {**routes._default_riders_state(), 'view': 'activity'}}
    assert client.get('/n/view-alpha/riders').status_code == 403


def test_roster_snapshot_is_complete_fresh_and_namespace_scoped(app, client, rider_views, monkeypatch):
    namespaces, (alpha, beta) = rider_views
    with alpha.begin() as conn:
        conn.execute(text("""INSERT INTO rider (id, first_name, last_name, stat_mo)
            SELECT n, 'Snapshot', 'Rider ' || n, 75 FROM generate_series(3, 8000) n"""))
    calls = []
    original_load = routes._load_namespace_roster_riders

    def load(*args, **kwargs):
        calls.append(kwargs)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(routes, '_load_namespace_roster_riders', load)
    started = perf_counter()
    page = client.get('/n/view-alpha/riders')
    elapsed = perf_counter() - started
    assert page.status_code == 200
    markup = BeautifulSoup(page.data, 'html.parser')
    snapshot = json.loads(markup.select_one('#rider-snapshot').string)
    assert len(snapshot['riders']) == 8000
    assert len(markup.select('.riders-table tbody tr')) == 100
    assert len(calls) == 1 and calls[0]['snapshot'] is True
    print(f'8000-rider page: {elapsed:.3f}s, {len(page.data)} bytes; gzip: {len(gzip.compress(page.data))} bytes')
    assert snapshot['namespace_id'] == namespaces[0].id
    assert all(rider['rider_url'].startswith('/n/view-alpha/') for rider in snapshot['riders'])
    assert 'view-beta Rider' not in page.get_data(as_text=True)
    second = client.get('/n/view-beta/riders/snapshot')
    assert second.status_code == 200 and second.cache_control.no_store
    assert len(second.json['riders']) == 2 and second.json['namespace_id'] == namespaces[1].id
    assert {rider['name'] for rider in second.json['riders']} == {'view-beta Rider', 'Untracked Rider'}
    assert all(rider.get('mapping_conflict') is not None for rider in second.json['riders'])
    with beta.begin() as conn:
        conn.execute(text('UPDATE rider SET stat_mo=88 WHERE id=1'))
    refreshed = client.get('/n/view-beta/riders/snapshot').json
    assert next(rider['stat_mo'] for rider in refreshed['riders'] if rider['rider_id'] == 1) == 88
    with monkeypatch.context() as patch:
        patch.setattr(Namespace, 'build_connection_string', lambda self: alpha.url)
        response = client.get('/n/view-beta/riders/snapshot')
        assert response.status_code == 503 and response.json is None


def test_snapshot_and_fixed_filters_enforce_permissions(app, client, rider_views, monkeypatch):
    original = routes._can_access_panel
    monkeypatch.setattr(routes, '_can_access_panel', lambda panel: panel != 'activity_monitor' and original(panel))
    page = BeautifulSoup(client.get('/n/view-alpha/riders').data, 'html.parser')
    assert not page.select_one('#rider-view option[value="activity"]')
    assert not page.select_one('[name="activity_status"]')
    assert page.select_one('[name="identity_flag"]') and page.select_one('[name="stat_key"]')
    snapshot = client.get('/n/view-alpha/riders/snapshot')
    assert snapshot.status_code == 200
    assert all('monitor_id' not in rider and 'latest_race_name' not in rider and 'check_display' not in rider
               for rider in snapshot.json['riders'])
    assert client.post('/n/view-alpha/riders', data={'activity_status': 'recent'}).status_code == 403
    monkeypatch.setattr(routes, '_can_access_panel', lambda panel: False)
    assert client.get('/n/view-alpha/riders/snapshot').status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert client.get('/n/view-alpha/riders/snapshot').status_code == 302


def test_rider_selection_creates_lists_in_the_selected_namespace(app, client, rider_views, monkeypatch):
    _, (alpha, beta) = rider_views
    path = '/n/view-beta/riders'
    create_url = '/n/view-beta/stats-editor/lists/create'
    with beta.begin() as conn:
        conn.execute(text("INSERT INTO rider (id, first_name, last_name, is_deleted) VALUES (3, 'Deleted', 'Rider', TRUE)"))

    markup = BeautifulSoup(client.get(path).data, 'html.parser')
    stat_button = markup.select_one('button[name="list_type"][value="stat"]')
    assert stat_button['formaction'] == create_url
    assert stat_button.get_text(strip=True) == 'Edit Stats'
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
    sync_button = markup.select_one('.dropdown-menu button[value="run_data_sync"]')
    assert sync_button and sync_button.get_text(strip=True) == 'Fetch Data'
    assert sync_button['form'] == 'rider-selection-form'
    assert markup.select_one('.dropdown-menu button[value="attribute"]')
    assert not markup.select_one('button[value="rider_identity_check"]')
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


def test_bulk_data_sync_queues_one_mixed_group_in_selected_namespace(app, client, rider_views, monkeypatch):
    namespaces, (alpha, beta) = rider_views
    with app.app_context():
        db.session.add_all(namespaces)
        db.session.commit()
        for namespace in namespaces:
            db.session.refresh(namespace)
    path = '/n/view-beta/riders/bulk-tasks'
    data = {'action': 'run_data_sync', 'rider_ids': '1,2,1'}
    submitted = []
    monkeypatch.setattr(routes, 'submit_external_group', submitted.append)
    for pcs, fc in ((False, False), (True, False), (False, True)):
        monkeypatch.setattr(routes, '_can_check_rider_identity', lambda: pcs)
        monkeypatch.setattr(routes, '_can_run_firstcycling_integrations', lambda: fc)
        assert client.post(path, data=data).status_code == 403
        review = BeautifulSoup(client.get('/n/view-beta/riders/review?tab=identity').data, 'html.parser')
        assert not review.select('#rider-selection-form, .rider-select, button[value="run_data_sync"]')
    monkeypatch.setattr(routes, '_can_check_rider_identity', lambda: True)
    monkeypatch.setattr(routes, '_can_run_firstcycling_integrations', lambda: True)
    with beta.begin() as conn:
        conn.execute(text('UPDATE rider SET first_cycling_id=9000+id'))
    review = BeautifulSoup(client.get('/n/view-beta/riders/review?tab=identity').data, 'html.parser')
    selection = review.select_one('#rider-selection-form')
    assert selection['data-limit'] == str(routes.MAX_BULK_TASK_RIDERS)
    assert '70001' in selection['data-storage-key']
    assert {box['value'] for box in selection.select('.rider-select')} == {'1', '2'}
    actions = review.select('.dropdown-menu button')
    assert [button.get_text(strip=True) for button in actions] == ['Fetch Data']
    assert actions[0]['formaction'] == path
    assert actions[0]['form'] == selection['id'] and actions[0].has_attr('disabled')
    assert not selection.select('form')
    assert review.select_one('script[src*="rider_selection.js"]')
    assert review.select_one('script[src*="rider_drag_selection.js"]')
    with monkeypatch.context() as patch:
        patch.setattr(Namespace, 'build_connection_string', lambda self: alpha.url)
        assert client.post(path, data=data).status_code == 302
    for invalid in ('', '1,999', '-1', ','.join(['1'] * 251)):
        assert client.post(path, data={**data, 'rider_ids': invalid}).status_code == 302
    # Missing FC links must leave neither provider group behind.
    with beta.begin() as conn:
        conn.execute(text('UPDATE rider SET first_cycling_id=NULL WHERE id=2'))
    assert client.post(path, data=data).status_code == 302
    assert submitted == []
    with app.app_context():
        assert ExternalCallTaskGroup.query.count() == 0
    with beta.begin() as conn:
        conn.execute(text('UPDATE rider SET first_cycling_id=9000+id'))
    response = client.post(path, data=data)
    assert response.status_code == 302 and len(submitted) == 1
    with app.app_context():
        import json

        groups = [db.session.get(ExternalCallTaskGroup, group_id) for group_id in submitted]
        assert [(group.namespace_id, group.provider, group.sync_types, group.status, group.requested_count)
                for group in groups] == [
                    (70001, None, ['firstcycling.get_rider_profile', 'pcs.get_rider_profile'], 'queued', 4),
                ]
        assert groups[0].workflow_type is None
        tasks = ExternalCallTask.query.filter_by(task_group_id=groups[0].id, provider='firstcycling').order_by(ExternalCallTask.id).all()
        assert [(task.related_to_id, json.loads(task.request_params_json)) for task in tasks] == [
            (1, {'rider_id': 9001}), (2, {'rider_id': 9002}),
        ]
        pcs_tasks = ExternalCallTask.query.filter_by(task_group_id=groups[0].id, provider='pcs').all()
        assert all('view-beta' in task.request_url or 'untracked' in task.request_url for task in pcs_tasks)
        for group in groups:
            ExternalCallTask.query.filter_by(task_group_id=group.id).delete()
            db.session.delete(group)
        for namespace in namespaces:
            db.session.delete(db.session.get(Namespace, namespace.id))
        db.session.commit()


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
