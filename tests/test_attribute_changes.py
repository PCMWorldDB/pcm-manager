"""Non-stat proposals preserve approval, namespace, and transaction boundaries."""
import os
from types import SimpleNamespace
from uuid import uuid4
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database
from sqlalchemy.exc import IntegrityError

from webapp.extensions import db
from webapp.integrations.cdb import attribute_changes as changes
from webapp.models import AuditLog, FirstCyclingRace, RaceResultMap, Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes
from webapp.integrations.cdb.rider_state import refresh_pending_flags


@pytest.fixture
def attribute_namespaces(app, monkeypatch):
    urls, engines, namespaces = [], [], []
    try:
        for index, slug in enumerate(('attribute-alpha', 'attribute-beta')):
            url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO rider (id,first_name,birth_date,stat_mo) VALUES (1,'Test',20000101,70)"))
                conn.execute(text("INSERT INTO team (id,team_name) VALUES (1,'Original')"))
            namespaces.append(SimpleNamespace(
                id=9900 + index, name=slug, slug=slug, database_name=url.database,
                database_year=2026, gender='Men',
                build_connection_string=lambda url=url: url))
        monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
        yield namespaces, engines
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)


def login(app, client, role):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        username = user.username
    with client.session_transaction() as session:
        session['_user_id'] = user_id
        session['_fresh'] = True
    return username


def test_attribute_proposal_review_and_namespace_isolation(app, client, attribute_namespaces):
    namespaces, engines = attribute_namespaces
    with engines[1].begin() as conn:
        conn.execute(text("""
            UPDATE rider SET pcs_slug='test', pcs_birth_date='2001-02-03', pcs_identity_status='review' WHERE id=1
        """))
    username = login(app, client, 'admin')
    page = client.get('/n/attribute-alpha/riders/1')
    assert page.status_code == 200
    assert b'name="attribute_name" value="birth_date"' in page.data
    assert b'type="date" name="value" value="2000-01-01"' in page.data
    assert b'aria-label="Edit Birth Date"' in page.data
    assert b'aria-label="Save Birth Date for review"' in page.data
    assert b'aria-label="Edit PCS Slug"' in page.data
    for ns in namespaces:
        result = client.post(f'/n/{ns.slug}/attributes/rider/1/propose',
                             data={'attribute_name': 'birth_date', 'value': '2001-02-03'})
        assert result.status_code == 302
        assert f'/n/{ns.slug}/riders/1' in result.location
    for engine in engines:
        with engine.begin() as conn:
            assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
            conn.execute(text("UPDATE cdb_changes SET created_at='2026-11-07 02:08:02.831958+00:00'"))
    page = client.get('/n/attribute-alpha/riders/review?tab=attributes')
    assert page.status_code == 200
    assert b'birth_date' in page.data and b'20010203' in page.data
    assert b'Approve' in page.data
    assert b'<th>Rider</th>' in page.data and b'<th>Submitted On</th>' in page.data
    assert b'href="/n/attribute-alpha/riders/1">Test</a>' in page.data
    assert b'name="return_to_rider_id"' not in page.data
    assert username.encode() in page.data
    assert b'2026-11-07 02:08:02' in page.data and b'831958' not in page.data
    profile = client.get('/n/attribute-alpha/riders/1')
    assert b'aria-label="Birth Date pending approval"' in profile.data
    assert b'aria-label="Edit Birth Date"' not in profile.data
    assert b'aria-label="Approve Birth Date"' not in profile.data
    assert b'/n/attribute-alpha/attributes/rider/review/1/approve' in profile.data
    assert b'id="rider-attribute-approvals"' in profile.data
    approvals_section = profile.data.split(b'id="rider-attribute-approvals"', 1)[1]
    assert b'Pending Attribute Approvals' in approvals_section
    assert b'birth_date' in approvals_section
    assert b'/n/attribute-alpha/attributes/rider/review/1/approve' in approvals_section
    context = None
    with app.test_request_context():
        # The actual form reads namespace data rather than enriched display values.
        with engines[0].connect() as conn:
            context = changes.editor_fields(conn, 'rider', 1)
    assert any(field['name'] == 'birth_date' and field['value'] == '2000-01-01' for field in context)
    assert not any(field['name'].startswith('stat_') for field in context)
    result = client.post('/n/attribute-beta/attributes/rider/review/1/approve')
    assert result.status_code == 302
    assert result.location == '/n/attribute-beta/riders/review?tab=attributes'
    assert b'PCS identity verified. DOB matches.' in client.get('/n/attribute-beta/riders/1').data
    history = client.get('/n/attribute-beta/riders/change-log')
    assert history.status_code == 200
    assert b'<td>Submitted</td>' in history.data and b'<td>Approved</td>' in history.data
    assert b'<td>Created</td>' not in history.data and b'<td>Applied</td>' not in history.data
    assert b'birth_date' in history.data and b'20010203' in history.data
    assert username.encode() in history.data
    assert b'href="/n/attribute-beta/riders/1">Test</a>' in history.data
    alpha_history = client.get('/n/attribute-alpha/riders/change-log')
    assert b'<td>Submitted</td>' in alpha_history.data
    assert b'<td>Approved</td>' not in alpha_history.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20010203
        assert conn.execute(text('SELECT pcs_birth_date::text, pcs_identity_status FROM rider WHERE id=1')).one() == (
            '2001-02-03', 'verified',
        )
        change = conn.execute(text('SELECT status, approved_at, applied_at FROM cdb_changes WHERE id=1')).one()
        assert change.status == 'approved'
        assert change.approved_at is not None and change.approved_at == change.applied_at
        events = conn.execute(text('SELECT event_type, context FROM cdb_change_events ORDER BY id')).all()
        assert events == [('submitted', {}), ('approved', {'actual_before_value': '20000101'})]
    assert client.post('/n/attribute-alpha/attributes/rider/review/1/reject').status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
        assert conn.execute(text('SELECT status FROM cdb_changes WHERE id=1')).scalar() == 'rejected'
        events = conn.execute(text('SELECT event_type FROM cdb_change_events ORDER BY id')).scalars().all()
        assert events == ['submitted', 'rejected']
    assert client.post('/n/missing/attributes/rider/1/propose',
                       data={'attribute_name': 'birth_date', 'value': '2001-02-03'}).status_code == 404


@pytest.mark.parametrize('action', ['approve', 'reject'])
def test_attribute_review_from_rider_returns_to_profile(app, client, attribute_namespaces, action):
    login(app, client, 'admin')
    base = '/n/attribute-alpha'
    rider_url = f'{base}/riders/1'
    result = client.post(f'{base}/attributes/rider/1/propose',
                         data={'attribute_name': 'birth_date', 'value': '2001-02-03'})
    assert result.status_code == 302

    page = client.get(rider_url)
    assert page.status_code == 200
    markup = BeautifulSoup(page.data, 'html.parser')
    assert [heading.get_text(strip=True) for heading in markup.select('#rider-review h4')] == ['Pending Attribute Approvals']
    action_url = f'{base}/attributes/rider/review/1/{action}'
    form = markup.select_one(f'#rider-attribute-approvals form[action="{action_url}"]')
    assert form is not None
    result = client.post(form['action'], data={
        field['name']: field['value'] for field in form.select('input[name]')
    })
    assert result.status_code == 302
    assert result.location == rider_url
    markup = BeautifulSoup(client.get(result.location).data, 'html.parser')
    review = markup.select_one('#rider-review')
    assert review.find('p', string='No reviews needed') is not None
    assert not review.select('h4, table')
    events = [cell.get_text(strip=True) for cell in markup.select('#rider-history tbody td:nth-child(2)')]
    assert events == (['Approved', 'Submitted'] if action == 'approve' else [])
    other_rider = BeautifulSoup(client.get('/n/attribute-beta/riders/1').data, 'html.parser')
    assert 'No applied changes recorded.' in other_rider.select_one('#rider-history').get_text()


def test_rider_pcs_slug_uses_attribute_approval(app, client, attribute_namespaces):
    _, engines = attribute_namespaces
    base = '/n/attribute-alpha'
    propose_url = base + '/attributes/rider/1/propose'
    data = {'attribute_name': 'pcs_slug', 'value': 'test-rider'}
    login(app, client, 'viewer')
    assert client.post(propose_url, data=data).status_code == 403

    login(app, client, 'admin')
    profile = BeautifulSoup(client.get(base + '/riders/1').data, 'html.parser')
    slug_editor = profile.select_one('form:has(input[name="attribute_name"][value="pcs_slug"])')
    assert slug_editor.select_one('input[name="value"]')['value'] == 'test'
    assert client.post(propose_url, data=data).status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT pcs_slug FROM rider WHERE id=1')).scalar_one() is None
    assert client.post(base + '/attributes/rider/review/1/approve').status_code == 302
    assert b'test-rider' in client.get(base + '/riders/1').data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT pcs_slug FROM rider WHERE id=1')).scalar_one() == 'test-rider'
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT pcs_slug FROM rider WHERE id=1')).scalar_one() is None


@pytest.mark.parametrize('role', ['stat_editor', 'stat_editor_approver', 'viewer'])
def test_stats_roles_cannot_write_or_approve_attributes(app, client, role):
    login(app, client, role)
    assert client.post('/attributes/rider/1/propose').status_code == 403
    assert client.post('/attributes/rider/review/1/approve').status_code == 403


def test_rider_review_tabs_separate_pending_history_and_namespaces(app, client, attribute_namespaces):
    _, engines = attribute_namespaces
    login(app, client, 'admin')
    with engines[0].begin() as conn:
        conn.execute(text('''
            INSERT INTO stat_changes
                (rider_id,stat_name,original_value,final_value,delta_value,is_confilict_override,is_approved)
            VALUES (1,'mo',70,71,1,FALSE,FALSE), (1,'mm',70,72,2,FALSE,TRUE)
        '''))
    page = client.get('/n/attribute-alpha/riders/review')
    assert page.status_code == 200
    for label in (b'>Stats</a>', b'>Identity</a>', b'>Activity</a>', b'>Attributes</a>'):
        assert label in page.data
    pending, history = page.data.split(b'History', 1)
    assert b'<td>MO</td>' in pending and b'<td>MM</td>' not in pending
    assert b'<td>MM</td>' in history and b'<td>MO</td>' not in history
    assert b'Approve rider stats' in pending
    assert b'/n/attribute-alpha/riders/1/stats/approve' in pending
    assert b'href="/n/attribute-alpha/riders/1"' in page.data
    beta = client.get('/n/attribute-beta/riders/review?tab=stats')
    assert b'No pending stat approvals.' in beta.data
    assert b'No historical or applied stat changes.' in beta.data
    for tab, text_value in [('identity', b'Identity review is coming soon.'),
                            ('activity', b'Rider race participation counts are coming soon.')]:
        response = client.get('/n/attribute-alpha/riders/review?tab=' + tab)
        assert response.status_code == 200 and text_value in response.data
        assert b'<td>MO</td>' not in response.data
    attributes = client.get('/n/attribute-alpha/riders/review?tab=attributes')
    assert b'Pending Attribute Approvals' in attributes.data
    assert b'Attribute Change History' in attributes.data
    assert b'<td>MO</td>' not in attributes.data
    assert client.get('/n/attribute-alpha/riders/review?tab=invalid').status_code == 404
    login(app, client, 'stat_editor')
    editor_page = client.get('/n/attribute-alpha/riders/review?tab=stats')
    assert b'Approve rider stats' not in editor_page.data
    assert client.post('/n/attribute-alpha/riders/1/stats/approve').status_code == 403


def test_rider_review_shows_batch_stat_edits_without_inline_approval(app, client, attribute_namespaces):
    _, engines = attribute_namespaces
    login(app, client, 'admin')
    with app.app_context():
        race = FirstCyclingRace(race_id=900003, season_year=2026, name='Batch Test Race')
        mapping = RaceResultMap(provider='firstcycling', external_race_id=str(race.race_id))
        db.session.add_all([race, mapping])
        db.session.commit()
        race_id, result_id = race.id, mapping.race_result_id

    try:
        with engines[0].begin() as conn:
            conn.execute(text("INSERT INTO rider_list (id,list_name,season_year,gender) VALUES (1,'Batch Test List',2026,'Men')"))
            conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1)'))
            conn.execute(text('''
                INSERT INTO stat_changes (race_result_id,stat_list_id,rider_id,stat_name,
                                          original_value,final_value,delta_value,is_approved,is_confilict_override)
                VALUES (NULL,NULL,1,'mo',70,71,1,FALSE,FALSE),
                       (NULL,1,1,'mm',70,72,2,FALSE,FALSE),
                       (:result,NULL,1,'tt',70,73,3,FALSE,FALSE)
            '''), {'result': result_id})
        base = '/n/attribute-alpha'
        assert client.post(f'{base}/attributes/rider/1/propose',
                           data={'attribute_name': 'birth_date', 'value': '2001-02-03'}).status_code == 302
        page = client.get(f'{base}/riders/1')
        assert page.status_code == 200
        review = BeautifulSoup(page.data, 'html.parser').select_one('#rider-review')
        stat_rows = {row.td.get_text(strip=True): row for row in review.select_one('table').select('tbody tr')}
        for stat, source_type, name, path in (
            ('MM', 'Stat List', 'Batch Test List', '/stats-editor/lists/1'),
            ('TT', 'Race Result', 'Batch Test Race', '/races/900003'),
        ):
            row = stat_rows[stat]
            assert row.find('small', string=source_type) is not None
            assert row.find('a', string=name)['href'] == base + path
            assert row.find('a', string='Review batch')['href'] == base + path
            assert not row.select('form, button')
        assert stat_rows['MO'].select_one('form')['action'] == f'{base}/riders/1/stats/approve'
        assert review.select_one('#rider-attribute-approvals form[action$="/approve"]')
        assert b'No reviews needed' in client.get('/n/attribute-beta/riders/1').data
        login(app, client, 'stat_editor')
        page = client.get(f'{base}/riders/1')
        assert page.status_code == 200
        review = BeautifulSoup(page.data, 'html.parser').select_one('#rider-review')
        assert review.get_text().count('Batch review required') == 2
        assert not review.select('form')
    finally:
        with app.app_context():
            db.session.delete(db.session.get(RaceResultMap, result_id))
            db.session.delete(db.session.get(FirstCyclingRace, race_id))
            db.session.commit()


def test_attribute_conflicts_validation_and_rollback(app, attribute_namespaces, monkeypatch):
    _, engines = attribute_namespaces
    engine = engines[0]
    with engine.begin() as conn:
        for field, value in [('id', '2'), ('stat_mo', '71'), ('first_name', 'Changed')]:
            with pytest.raises(PermissionError):
                changes.propose(conn, 'rider', 1, field, value, 1)
        with pytest.raises(ValueError):
            changes.propose(conn, 'rider', 1, 'birth_date', '2001-02-30', 1)
        change_id = changes.propose(conn, 'rider', 1, 'birth_date', '2001-02-03', 1)
        with pytest.raises(changes.AttributeConflict):
            changes.propose(conn, 'rider', 1, 'birth_date', '2002-02-03', 1)
        conn.execute(text('UPDATE rider SET birth_date=19990101 WHERE id=1'))
    with pytest.raises(changes.AttributeConflict):
        with engine.begin() as conn:
            changes.decide(conn, 'rider', change_id, 'approve', 2)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM cdb_changes WHERE id=:id'), {'id': change_id}).scalar() == 'pending'
        assert conn.execute(text('SELECT count(*) FROM cdb_change_events')).scalar() == 1
    with engine.begin() as conn:
        changes.decide(conn, 'rider', change_id, 'reject', 2)
        with pytest.raises(changes.AttributeConflict):
            changes.decide(conn, 'rider', change_id, 'approve', 2)
    # Team fields share the same workflow when explicitly enabled.
    monkeypatch.setattr(changes, 'get_editable_fields', lambda entity: {'team_name'} if entity == 'team' else set())
    with engine.begin() as conn:
        team_change = changes.propose(conn, 'team', 1, 'team_name', 'Updated', 1)
        changes.decide(conn, 'team', team_change, 'approve', 2)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT team_name FROM team WHERE id=1')).scalar() == 'Updated'
    assert changes.object_table('race').name == 'race'
    with engine.begin() as conn:
        with pytest.raises(PermissionError, match='read-only'):
            changes.propose(conn, 'race', 1, 'race_name', 'Updated race', 1)
    with pytest.raises(LookupError):
        changes.object_table('unknown')


def test_individual_save_submission_survives_stale_audit_and_separate_approver(app, attribute_namespaces):
    namespaces, engines = attribute_namespaces
    editor, approver = app.test_client(), app.test_client()
    username = login(app, editor, 'stat_editor')
    login(app, approver, 'stat_editor_approver')
    base = '/n/attribute-alpha/riders/1/stats/'
    payload = {'changes_json': '[{"first_cycling_rider_id":1,"stat_name":"mo","final_value":72}]'}
    assert editor.post(base + 'save', data=payload).status_code == 302
    with engines[0].connect() as conn:
        fingerprint = routes._individual_pending_fingerprint(conn, 1)
        assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar() == 70
        assert conn.execute(text('SELECT has_pending_stat_changes FROM rider WHERE id=1')).scalar() is True
    with app.app_context():
        # A delayed submission from an older save must not hide the valid one.
        user = User.query.filter_by(username=username).one()
        AuditLog.log_action('individual_stats_submitted', user,
                            resource_type=f'namespace:{namespaces[0].id}:individual', resource_id=1,
                            details={'fingerprint': 'stale-overlapping-save'})
        db.session.commit()
        assert routes._individual_submission_matches(namespaces[0], 1, fingerprint)
        assert not routes._individual_submission_matches(namespaces[1], 1, fingerprint)
    response = approver.post(base + 'approve', follow_redirects=True)
    assert response.status_code == 200
    assert b'Submit the current saved changes for approval before approving.' not in response.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar() == 72
        assert conn.execute(text('SELECT is_approved FROM stat_changes WHERE rider_id=1')).scalar() is True
        assert conn.execute(text('SELECT has_pending_stat_changes FROM rider WHERE id=1')).scalar() is False
    # A later unsubmitted change still fails the exact-content gate.
    assert editor.post(base + 'save', data={'changes_json': payload['changes_json'].replace('72', '73')}).status_code == 302
    with engines[0].begin() as conn:
        conn.execute(text('UPDATE stat_changes SET final_value=74,delta_value=4 WHERE is_approved=FALSE'))
    response = approver.post(base + 'approve', follow_redirects=True)
    assert b'Submit the current saved changes for approval before approving.' in response.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar() == 72


def test_individual_save_rolls_back_if_submission_record_fails(app, client, attribute_namespaces, monkeypatch):
    _, engines = attribute_namespaces
    login(app, client, 'stat_editor')

    def fail_submission(*args, **kwargs):
        raise RuntimeError('Submission storage unavailable')

    monkeypatch.setattr(routes.AuditLog, 'log_action', fail_submission)
    with pytest.raises(RuntimeError, match='Submission storage unavailable'):
        client.post('/n/attribute-alpha/riders/1/stats/save', data={
            'changes_json': '[{"first_cycling_rider_id":1,"stat_name":"mo","final_value":72}]'})
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_changes')).scalar() == 0
        assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar() == 70


def test_rider_pending_flags_track_remaining_changes_and_manual_migration(app, attribute_namespaces, monkeypatch):
    _, engines = attribute_namespaces
    engine = engines[0]
    monkeypatch.setattr(changes, 'get_editable_fields', lambda entity: {'birth_date', 'first_name'})
    with engine.begin() as conn:
        assert tuple(conn.execute(text('SELECT pcs_identity_status,has_pending_stat_changes,has_pending_attribute_changes FROM rider WHERE id=1')).one()) == ('unknown', False, False)
        birth = changes.propose(conn, 'rider', 1, 'birth_date', '2001-02-03', 1)
        name = changes.propose(conn, 'rider', 1, 'first_name', 'Updated', 1)
        changes.decide(conn, 'rider', birth, 'approve', 2)
        assert conn.execute(text('SELECT has_pending_attribute_changes FROM rider WHERE id=1')).scalar() is True
        conn.execute(text('''INSERT INTO stat_changes
            (rider_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
            VALUES (1,'mo',70,71,1,FALSE,FALSE)'''))
        refresh_pending_flags(conn, [1])
        changes.decide(conn, 'rider', name, 'reject', 2)
        assert tuple(conn.execute(text('SELECT has_pending_stat_changes,has_pending_attribute_changes FROM rider WHERE id=1')).one()) == (True, False)
        # Different stat scopes must keep the same aggregate flag set.
        conn.execute(text('''INSERT INTO stat_changes
            (stat_list_id,rider_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
            VALUES (7,1,'mm',70,71,1,FALSE,FALSE)'''))
        conn.execute(text('UPDATE stat_changes SET is_approved=TRUE WHERE stat_list_id IS NULL'))
        refresh_pending_flags(conn, [1])
        assert conn.execute(text('SELECT has_pending_stat_changes FROM rider WHERE id=1')).scalar() is True
    # Simulate a pre-migration namespace, then verify the reviewed SQL backfill.
    with engine.begin() as conn:
        conn.execute(text('''ALTER TABLE rider DROP CONSTRAINT ck_rider_pcs_identity_status,
            DROP COLUMN pcs_identity_status, DROP COLUMN pcs_slug, DROP COLUMN has_pending_stat_changes,
            DROP COLUMN has_pending_attribute_changes'''))
    root = Path(__file__).resolve().parents[1]
    with engine.connect() as conn:
        conn.exec_driver_sql((root / 'webapp/tools/rider_state_namespace.sql').read_text())
    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET identity_pcs_status='verified' WHERE id=1"))
    with engine.connect() as conn:
        conn.exec_driver_sql((root / 'migrations/rider_pcs_slug_namespace.sql').read_text())
    with engine.begin() as conn:
        assert tuple(conn.execute(text('SELECT pcs_slug,pcs_identity_status,has_pending_stat_changes,has_pending_attribute_changes FROM rider WHERE id=1')).one()) == (None, 'verified', True, False)
        conn.execute(text('DELETE FROM stat_changes WHERE is_approved=FALSE'))
        refresh_pending_flags(conn, [1])
        assert conn.execute(text('SELECT has_pending_stat_changes FROM rider WHERE id=1')).scalar() is False
    with engines[1].connect() as conn:
        assert tuple(conn.execute(text('SELECT has_pending_stat_changes,has_pending_attribute_changes FROM rider WHERE id=1')).one()) == (False, False)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("UPDATE rider SET pcs_identity_status='invalid'"))
