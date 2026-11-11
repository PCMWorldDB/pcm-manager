"""The first added rider persists a draft in the selected namespace."""
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event, text

from webapp.extensions import db
from webapp.models import Action, AuditLog, Resource, Role, RolePermission, User
from webapp.integrations.cdb import attribute_changes
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


@pytest.fixture
def draft_context(app, client, monkeypatch):
    with app.app_context():
        admin = create_engine(db.engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    namespaces, engines = [], []
    try:
        for index in (1, 2):
            name = 'draft_' + uuid4().hex
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE {name}'))
            engine = create_engine(admin.url.set(database=name))
            engines.append(engine)
            namespaces.append(SimpleNamespace(
                id=900000 + index, slug=f'draft-{index}', database_name=name,
                database_year=2026, gender='Men',
                build_connection_string=lambda engine=engine: engine.url,
            ))
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO rider (id,first_name,last_name,stat_mo) VALUES (1,'Test','Rider',70)"))
        monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
        monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespaces[0])
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        yield namespaces, engines, user_id
    finally:
        for namespace, engine in zip(namespaces, engines):
            engine.dispose()
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE {namespace.database_name} WITH (FORCE)'))
        admin.dispose()
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_first_rider_saves_reopenable_draft(app, client, draft_context):
    namespaces, engines, user_id = draft_context
    page = client.get('/riders/bulk-editor/new?list_search_rider=Test')
    assert page.status_code == 200
    assert b'Test Rider' in page.data
    assert b'name="rider_id" value="1"' in page.data
    assert client.get(f'/n/{namespaces[0].slug}/riders/bulk-editor/new').status_code == 200
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM rider_list')).scalar_one() == 0
    response = client.post('/stats-editor/lists/create', data={'rider_id': 1})
    assert response.status_code == 302
    with engines[0].connect() as conn:
        row = conn.execute(text('SELECT * FROM rider_list')).mappings().one()
        list_id = row['id']
        assert row['list_name'].startswith('Draft list - ')
        assert (row['season_year'], row['gender'], row['created_by_id']) == (2026, 'Men', user_id)
        assert conn.execute(text('SELECT rider_id FROM rider_list_item')).scalar_one() == 1
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM rider_list')).scalar_one() == 0
    # A fresh browser session can reopen the persisted list without any draft state.
    reopened = app.test_client()
    with reopened.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    editor = BeautifulSoup(reopened.get(response.location).data, 'html.parser')
    assert 'Test Rider' in editor.get_text()
    assert editor.select_one('[aria-label="List status"]').get_text(strip=True) == 'Draft'
    lists = BeautifulSoup(reopened.get('/riders/bulk-editor').data, 'html.parser')
    assert lists.select_one('tbody td:nth-child(4)').get_text(strip=True) == 'Draft'
    assert reopened.post(f'/stats-editor/lists/{list_id}/rename', data={'list_name': 'My riders'}).status_code == 302
    assert b'My riders' in reopened.get(response.location).data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_changes')).scalar_one() == 0


def test_bulk_editor_status_tracks_submission_of_current_changes(client, draft_context, monkeypatch):
    monkeypatch.setattr(routes, 'notify_discord_stat_list_saved', lambda *args, **kwargs: True)
    created = client.post('/stats-editor/lists/create', data={'rider_id': 1})
    editor_url = created.location
    base = '/stats-editor/lists/1/'

    def save_changes(value):
        payload = json.dumps([{'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': value}])
        assert client.post(base + 'save-stat-deltas', data={'changes_json': payload}).status_code == 302

    def status_cells():
        editor = BeautifulSoup(client.get(editor_url).data, 'html.parser')
        lists = BeautifulSoup(client.get('/riders/bulk-editor').data, 'html.parser')
        return [editor.select_one('[aria-label="List status"]'), lists.select_one('tbody td:nth-child(4)')]

    save_changes(72)
    assert all(cell.select_one('.badge').get_text(strip=True) == 'Draft' for cell in status_cells())
    assert client.post(base + 'submit-for-approval').status_code == 302
    assert all(cell.select_one('[title="Approval pending"]') for cell in status_cells())
    save_changes(73)
    assert all(cell.select_one('.badge').get_text(strip=True) == 'Draft' for cell in status_cells())


def test_attribute_list_uses_list_approval(app, client, draft_context, monkeypatch):
    namespaces, engines, user_id = draft_context
    endpoint = '/stats-editor/lists/create'
    proposal = '/attributes/rider/1/propose'
    payload = {'list_id': 1, 'attribute_name': 'birth_date', 'value': '2000-01-02'}
    assert client.post(endpoint, data={'rider_id': 1, 'list_type': 'attribute'}).status_code == 403
    assert client.post(proposal, data=payload).status_code == 403
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name='admin').one()]
        db.session.commit()

    new_page = client.get('/riders/bulk-editor/new?list_type=attribute&list_name=Birthdates&list_search_rider=Test')
    markup = BeautifulSoup(new_page.data, 'html.parser')
    assert [cell.get_text(strip=True) for cell in markup.select('thead th')] == [
        '', 'Name', 'Team', 'Country', 'Rider ID', 'FirstCycling ID', 'Birth Date', 'Height', 'Weight',
    ]
    assert markup.select_one('.stat-col') is None
    form = markup.select_one('#new-list-form')
    assert form.select_one('#list-type option[selected]').get_text(strip=True) == 'Attribute List'
    add_rider = markup.select_one('tbody button[name="rider_id"]')
    assert add_rider['form'] == form['id'] and add_rider.has_attr('formnovalidate')
    created = client.post(form['action'], data={
        'rider_id': add_rider['value'],
        'list_type': form.select_one('#list-type option[selected]')['value'],
        'list_name': form.select_one('#list-name')['value'],
    })
    assert created.status_code == 302

    def editor():
        return BeautifulSoup(client.get(created.location).data, 'html.parser')

    markup = editor()
    assert 'List Attributes' in markup.get_text()
    assert markup.select_one('#list-name')['value'] == 'Birthdates'
    assert markup.select_one('[aria-label="List status"]').get_text(strip=True) == 'Draft'
    attributes = {item.select_one('.object-attribute-label').get_text(strip=True):
                  item.select_one('.object-attribute-value').get_text(strip=True)
                  for item in markup.select('.object-attributes > .object-attribute')}
    assert attributes['Items'] == '1'
    assert attributes['Type'] == 'Attribute List'
    assert 'Birth Date' in [cell.get_text(strip=True) for cell in markup.select('thead th')]
    assert markup.select_one('input[name="attribute_name"]')['value'] == 'birth_date'
    assert markup.select_one('.stat-input') is None
    assert client.post('/stats-editor/lists/1/rename', data={'list_name': 'Verified birthdates'}).status_code == 302
    assert editor().select_one('#list-name')['value'] == 'Verified birthdates'
    assert client.post('/stats-editor/lists/1/submit-for-approval').status_code == 409
    assert client.post('/attributes/rider/2/propose', data=payload).status_code == 404
    assert client.post(proposal, data={**payload, 'attribute_name': 'stat_mo', 'value': '73'}).status_code == 403

    with engines[1].begin() as conn:
        conn.execute(text("INSERT INTO rider_list (id,list_name,season_year,gender) VALUES (1,'Other',2026,'Men')"))
        conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1)'))
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespaces[1])
    assert client.post(proposal, data=payload).status_code == 409
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM cdb_changes')).scalar_one() == 0
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespaces[0])

    assert client.post(proposal, data=payload).status_code == 302
    assert editor().select_one('[aria-label="List status"]').get_text(strip=True) == 'Approval pending'
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar_one() is None
        change_id, list_id = conn.execute(text('SELECT id, stat_list_id FROM cdb_changes')).one()
        assert list_id == 1
    blocked = client.post(f'/attributes/rider/review/{change_id}/approve', follow_redirects=True)
    assert b'Approve this change together with its Attribute List' in blocked.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar_one() is None
    review = BeautifulSoup(client.get('/riders/review?tab=attributes').data, 'html.parser')
    assert review.select_one(f'form[action$="/review/{change_id}/approve"]') is None
    assert review.find('a', string='Review list') is not None
    assert client.post('/stats-editor/lists/1/toggle-rider', data={'rider_id': 1, 'is_checked': '0'}).status_code == 409
    approve_form = editor().find('button', string='Approve List').find_parent('form')
    assert client.post(approve_form['action']).status_code == 302
    assert editor().select_one('[aria-label="List status"]').get_text(strip=True) == 'Approved'
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar_one() == 20000102
    lists = client.get('/riders/bulk-editor?list_type=attribute')
    markup = BeautifulSoup(lists.data, 'html.parser')
    assert [cell.get_text(strip=True) for cell in markup.select('tbody td')[2:]] == ['Attribute List', 'Approved', '1', '1']
    assert b'Verified birthdates' not in client.get('/riders/bulk-editor?list_type=stat').data


@pytest.fixture
def attribute_batch_context(app, draft_context):
    namespaces, engines, user_id = draft_context
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name='admin').one()]
        db.session.commit()
    for engine in engines:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO rider (id,first_name) VALUES (2,'Second'),(3,'Outside')"))
            conn.execute(text("""INSERT INTO rider_list (id,list_name,list_type,season_year,gender)
                VALUES (1,'Attributes','attribute',2026,'Men'),(2,'Stats','stat',2026,'Men')"""))
            conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1),(1,2)'))
            for rider_id in (1, 2):
                attribute_changes.propose(conn, 'rider', rider_id, 'birth_date', '2000-01-02', user_id, stat_list_id=1)
            attribute_changes.propose(conn, 'rider', 2, 'pcs_slug', 'second-rider', user_id, stat_list_id=1)
            attribute_changes.propose(conn, 'rider', 3, 'birth_date', '1999-01-01', user_id)
    return namespaces, engines, user_id


def test_attribute_list_bulk_approval_is_atomic_and_namespace_isolated(client, attribute_batch_context, monkeypatch):
    namespaces, engines, _ = attribute_batch_context
    endpoint = '/stats-editor/lists/1/approve-attributes'
    assert client.post('/stats-editor/lists/2/approve-attributes').status_code == 409
    assert client.post('/stats-editor/lists/999/approve-attributes').status_code == 404
    # Connecting to the other namespace cannot approve its overlapping list/rider IDs.
    with monkeypatch.context() as patch:
        patch.setattr(routes, 'create_engine', lambda _: engines[1])
        assert client.post(endpoint).status_code == 302
        with client.session_transaction() as session:
            assert any('could not be approved' in message for _, message in session.get('_flashes', []))
    approved = client.post(f'/n/{namespaces[0].slug}' + endpoint, follow_redirects=True)
    assert b'Approved and applied 3 attribute change(s)' in approved.data
    markup = BeautifulSoup(approved.data, 'html.parser')
    assert markup.select_one('[aria-label="List status"]').get_text(strip=True) == 'Approved'
    assert markup.find('button', string='Approve List').has_attr('disabled')
    assert client.post(endpoint).status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider ORDER BY id')).scalars().all() == [20000102, 20000102, None]
        assert conn.execute(text('SELECT pcs_slug FROM rider WHERE id=2')).scalar_one() == 'second-rider'
        assert conn.execute(text("SELECT count(*) FROM cdb_change_events WHERE event_type='approved'")).scalar_one() == 3
        assert conn.execute(text("SELECT status FROM cdb_changes WHERE stat_list_id IS NULL")).scalar_one() == 'pending'
        assert conn.execute(text('SELECT has_pending_attribute_changes FROM rider ORDER BY id')).scalars().all() == [False, False, True]
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider ORDER BY id')).scalars().all() == [None, None, None]
        assert conn.execute(text("SELECT count(*) FROM cdb_changes WHERE status='pending'")).scalar_one() == 4


def test_attribute_list_bulk_approval_conflict_rolls_back_every_change(client, attribute_batch_context):
    _, engines, _ = attribute_batch_context
    with engines[0].begin() as conn:
        conn.execute(text('UPDATE rider SET birth_date=19990101 WHERE id=2'))
    response = client.post('/stats-editor/lists/1/approve-attributes', follow_redirects=True)
    assert b'The list could not be approved.' in response.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar_one() is None
        assert conn.execute(text("SELECT count(*) FROM cdb_changes WHERE status='pending'")).scalar_one() == 4
        assert conn.execute(text("SELECT count(*) FROM cdb_change_events WHERE event_type='approved'")).scalar_one() == 0


@pytest.mark.parametrize('role', ['viewer', 'database_editor', 'stat_editor', 'stat_editor_approver'])
def test_attribute_list_bulk_approval_requires_approver(app, client, attribute_batch_context, role):
    _, engines, user_id = attribute_batch_context
    with app.app_context():
        db.session.get(User, user_id).roles = [Role.query.filter_by(name=role).one()]
        db.session.commit()
    assert client.post('/stats-editor/lists/1/approve-attributes').status_code == 403
    with engines[0].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM cdb_changes WHERE status='pending'")).scalar_one() == 4


def test_attribute_list_bulk_approval_checks_every_field_permission(app, client, attribute_batch_context):
    _, engines, _ = attribute_batch_context
    with app.app_context():
        role = Role.query.filter_by(name='admin').one()
        resource = Resource.query.filter_by(name='rider:pcs_slug').one()
        action = Action.query.filter_by(name='approve').one()
        ids = role.id, resource.id, action.id
        permission = RolePermission.query.filter_by(role_id=ids[0], resource_id=ids[1], action_id=ids[2]).one()
        db.session.delete(permission)
        db.session.commit()
    try:
        page = BeautifulSoup(client.get('/riders/bulk-editor?tab=editor&list_id=1').data, 'html.parser')
        assert page.find('button', string='Approve List').has_attr('disabled')
        assert client.post('/stats-editor/lists/1/approve-attributes').status_code == 403
        with engines[0].connect() as conn:
            assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar_one() is None
            assert conn.execute(text("SELECT count(*) FROM cdb_changes WHERE status='pending'")).scalar_one() == 4
            assert conn.execute(text("SELECT count(*) FROM cdb_change_events WHERE event_type='approved'")).scalar_one() == 0
    finally:
        with app.app_context():
            db.session.add(RolePermission(role_id=ids[0], resource_id=ids[1], action_id=ids[2]))
            db.session.commit()


def test_invalid_first_rider_and_wrong_connection_leave_no_draft(client, draft_context, monkeypatch):
    namespaces, engines, _ = draft_context
    assert client.post('/stats-editor/lists/create', data={'rider_id': 999}).status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM rider_list')).scalar_one() == 0
    monkeypatch.setattr(routes, 'create_engine', lambda _: engines[1])
    assert client.post('/stats-editor/lists/create', data={'rider_id': 1}).status_code == 302
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM rider_list')).scalar_one() == 0


def test_failed_first_add_rolls_back_list(client, draft_context):
    _, engines, _ = draft_context

    def fail_item_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO rider_list_item'):
            raise RuntimeError('Synthetic first-rider insert failure')

    event.listen(engines[0], 'before_cursor_execute', fail_item_insert)
    try:
        # Route engines are separate instances; share the injected failure through the URL factory.
        from unittest.mock import patch
        with patch.object(routes, 'create_engine', return_value=engines[0]):
            assert client.post('/stats-editor/lists/create', data={'rider_id': 1}).status_code == 302
        with engines[0].connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM rider_list')).scalar_one() == 0
            assert conn.execute(text('SELECT count(*) FROM rider_list_item')).scalar_one() == 0
    finally:
        event.remove(engines[0], 'before_cursor_execute', fail_item_insert)


@pytest.mark.parametrize('role', ['viewer', 'stat_editor_approver'])
def test_draft_creation_and_rename_permissions(app, client, draft_context, role):
    _, _, user_id = draft_context
    with app.app_context():
        user = db.session.get(User, user_id)
        user.roles = [Role.query.filter_by(name=role).one()]
        db.session.commit()
    assert client.get('/riders/bulk-editor/new').status_code == 403
    assert client.post('/stats-editor/lists/create', data={'rider_id': 1}).status_code == 403
    assert client.post('/stats-editor/lists/1/rename', data={'list_name': 'Denied'}).status_code == 403


def test_anonymous_draft_actions_require_login(client):
    for method, path in [('get', '/riders/bulk-editor/new'),
                         ('post', '/stats-editor/lists/create'),
                         ('post', '/stats-editor/lists/1/rename'),
                         ('post', '/stats-editor/lists/1/approve-attributes')]:
        response = getattr(client, method)(path)
        assert response.status_code == 302
        assert '/login' in response.location


def test_bulk_tabs_filters_and_query_selected_editor(client, draft_context):
    namespaces, engines, user_id = draft_context
    with engines[0].begin() as conn:
        conn.execute(text('''INSERT INTO rider_list (list_name,season_year,gender,created_by_id)
            VALUES ('Alpha list',2026,'Men',:user),('Beta list',2026,'Men',NULL)'''), {'user': user_id})
        conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1)'))
    page = client.get('/riders/bulk-editor')
    markup = BeautifulSoup(page.data, 'html.parser')
    assert [heading.get_text(strip=True) for heading in markup.find_all('h2')].count('Bulk Editor') == 1
    assert b'Make changes to rider stats' in page.data
    tabs = markup.select_one('nav[aria-label="Bulk Editor sections"]')
    assert [a.get_text(strip=True) for a in tabs.select('a')] == ['Lists', 'Editor']
    assert tabs.select_one('.active').get_text() == 'Lists'
    assert b'Start a List' not in page.data
    assert markup.find('a', string='New list') is None
    for query, expected in [('list_name=beta', ['Beta list']),
                            (f'creator_id={user_id}', ['Alpha list']),
                            ('list_type=stat', ['Alpha list', 'Beta list']),
                            ('list_type=attribute', []),
                            (f'creator_id={user_id}&list_name=Beta', [])]:
        markup = BeautifulSoup(client.get('/riders/bulk-editor?' + query).data, 'html.parser')
        assert sorted(a.get_text(strip=True) for a in markup.select('table tbody td:first-child a')) == expected
    page = client.get('/riders/bulk-editor?tab=editor&list_id=1')
    assert page.status_code == 200
    markup = BeautifulSoup(page.data, 'html.parser')
    assert markup.select_one('nav[aria-label="Bulk Editor sections"] .active').get_text() == 'Editor'
    assert markup.find('h2', string='Bulk Editor') is not None
    assert b'Make changes to rider stats' in page.data
    assert b'Test Rider' in page.data
    assert markup.select_one('#list-type').has_attr('disabled')
    headings = [heading.get_text(strip=True) for heading in markup.select('h3, h4, h5')]
    assert headings.index('List Attributes') < headings.index('Add Riders To List') < headings.index('Rider List')
    new_list_url = markup.find('a', string='New list')['href']
    assert 'list_id=' not in new_list_url
    search = markup.select_one('#rider-search-form')
    assert search.select_one('input[name="list_id"]')['value'] == '1'
    assert search.select_one('input[name="tab"]')['value'] == 'editor'
    new_list = client.get(new_list_url)
    assert new_list.status_code == 200
    assert b'>Bulk Editor</h2>' in new_list.data and b'Make changes to rider stats' in new_list.data
    markup = BeautifulSoup(new_list.data, 'html.parser')
    assert markup.select_one('#new-list-form #list-name')['value'] == ''
    assert [option['value'] for option in markup.select('#new-list-form #list-type option')] == ['stat']
    assert not markup.select_one('#new-list-form').select('#rider-search-form')
    created = client.post(markup.select_one('#new-list-form')['action'], data={'list_name': 'New list', 'list_type': 'stat'})
    assert created.status_code == 302
    assert b'No riders found in this list.' in client.get(created.location).data
    assert client.get('/riders/bulk-editor?tab=editor&list_id=999').status_code == 404
    assert client.get('/riders/bulk-editor?tab=editor&list_id=invalid').status_code == 404
    assert client.get(f'/n/{namespaces[0].slug}/riders/bulk-editor?tab=editor&list_id=1').status_code == 200


def test_approved_list_is_read_only_and_namespace_isolated(client, draft_context, monkeypatch):
    namespaces, engines, _ = draft_context
    for index, engine in enumerate(engines):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO rider_list (id,list_name,season_year,gender) VALUES (1,'Locked list',2026,'Men')"))
            conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1)'))
            conn.execute(text('''INSERT INTO stat_changes
                (stat_list_id,rider_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
                VALUES (1,1,'mo',70,72,2,:approved,FALSE)'''), {'approved': index == 0})
    markup = BeautifulSoup(client.get('/riders/bulk-editor?tab=editor&list_id=1').data, 'html.parser')
    assert 'approved and read-only' in markup.get_text()
    assert markup.select_one('#rider-search-form') is None
    assert markup.select_one('#stat-delta-form') is None
    assert all(field.has_attr('disabled') for field in markup.select('.stat-input'))
    payloads = {
        'add-rider': {'rider_id': 1},
        'toggle-rider': {'rider_id': 1, 'is_checked': '0'},
        'save-stat-deltas': {'changes_json': json.dumps([{'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': 75}])},
        'rename': {'list_name': 'Changed'},
        'submit-for-approval': {},
    }
    for action, data in payloads.items():
        assert client.post(f'/stats-editor/lists/1/{action}', data=data).status_code == 409
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT list_name FROM rider_list WHERE id=1')).scalar_one() == 'Locked list'
        assert conn.execute(text('SELECT count(*) FROM rider_list_item')).scalar_one() == 1
        assert conn.execute(text('SELECT final_value FROM stat_changes')).scalar_one() == 72
    # The same list and rider IDs in another namespace stay independently editable.
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespaces[1])
    assert client.post('/stats-editor/lists/1/rename', data={'list_name': 'Still editable'}).status_code == 302
    assert client.post('/stats-editor/lists/1/toggle-rider', data={'rider_id': 1, 'is_checked': '0'}).status_code == 302
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM rider_list_item')).scalar_one() == 0
