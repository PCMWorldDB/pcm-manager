"""The first added rider persists a draft in the selected namespace."""
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event, text

from webapp.extensions import db
from webapp.models import Role, User
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
        assert conn.execute(text('SELECT count(*) FROM stat_lists')).scalar_one() == 0
    response = client.post('/stats-editor/lists/create', data={'rider_id': 1})
    assert response.status_code == 302
    with engines[0].connect() as conn:
        row = conn.execute(text('SELECT * FROM stat_lists')).mappings().one()
        list_id = row['id']
        assert row['list_name'].startswith('Draft list - ')
        assert (row['season_year'], row['gender'], row['created_by_id']) == (2026, 'Men', user_id)
        assert conn.execute(text('SELECT rider_id FROM stat_list_items')).scalar_one() == 1
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_lists')).scalar_one() == 0
    # A fresh browser session can reopen the persisted list without any draft state.
    reopened = app.test_client()
    with reopened.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    assert b'Test Rider' in reopened.get(response.location).data
    assert reopened.post(f'/stats-editor/lists/{list_id}/rename', data={'list_name': 'My riders'}).status_code == 302
    assert b'My riders' in reopened.get(response.location).data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_changes')).scalar_one() == 0


def test_invalid_first_rider_and_wrong_connection_leave_no_draft(client, draft_context, monkeypatch):
    namespaces, engines, _ = draft_context
    assert client.post('/stats-editor/lists/create', data={'rider_id': 999}).status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_lists')).scalar_one() == 0
    monkeypatch.setattr(routes, 'create_engine', lambda _: engines[1])
    assert client.post('/stats-editor/lists/create', data={'rider_id': 1}).status_code == 302
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_lists')).scalar_one() == 0


def test_failed_first_add_rolls_back_list(client, draft_context):
    _, engines, _ = draft_context

    def fail_item_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO stat_list_items'):
            raise RuntimeError('Synthetic first-rider insert failure')

    event.listen(engines[0], 'before_cursor_execute', fail_item_insert)
    try:
        # Route engines are separate instances; share the injected failure through the URL factory.
        from unittest.mock import patch
        with patch.object(routes, 'create_engine', return_value=engines[0]):
            assert client.post('/stats-editor/lists/create', data={'rider_id': 1}).status_code == 302
        with engines[0].connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM stat_lists')).scalar_one() == 0
            assert conn.execute(text('SELECT count(*) FROM stat_list_items')).scalar_one() == 0
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
                         ('post', '/stats-editor/lists/1/rename')]:
        response = getattr(client, method)(path)
        assert response.status_code == 302
        assert '/login' in response.location


def test_bulk_tabs_filters_and_query_selected_editor(client, draft_context):
    namespaces, engines, user_id = draft_context
    with engines[0].begin() as conn:
        conn.execute(text('''INSERT INTO stat_lists (id,list_name,season_year,gender,created_by_id)
            VALUES (1,'Alpha list',2026,'Men',:user),(2,'Beta list',2026,'Men',NULL)'''), {'user': user_id})
        conn.execute(text('INSERT INTO stat_list_items (stat_list_id,rider_id) VALUES (1,1)'))
    page = client.get('/riders/bulk-editor')
    markup = BeautifulSoup(page.data, 'html.parser')
    tabs = markup.select_one('nav[aria-label="Bulk Editor sections"]')
    assert [a.get_text(strip=True) for a in tabs.select('a')] == ['Lists', 'Editor']
    assert tabs.select_one('.active').get_text() == 'Lists'
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
    assert b'Test Rider' in page.data
    search = markup.select_one('#rider-search-form')
    assert search.select_one('input[name="list_id"]')['value'] == '1'
    assert search.select_one('input[name="tab"]')['value'] == 'editor'
    assert client.get('/riders/bulk-editor?tab=editor&list_id=999').status_code == 404
    assert client.get('/riders/bulk-editor?tab=editor&list_id=invalid').status_code == 404
    assert client.get(f'/n/{namespaces[0].slug}/riders/bulk-editor?tab=editor&list_id=1').status_code == 200


def test_approved_list_is_read_only_and_namespace_isolated(client, draft_context, monkeypatch):
    namespaces, engines, _ = draft_context
    for index, engine in enumerate(engines):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO stat_lists (id,list_name,season_year,gender) VALUES (1,'Locked list',2026,'Men')"))
            conn.execute(text('INSERT INTO stat_list_items (stat_list_id,rider_id) VALUES (1,1)'))
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
        assert conn.execute(text('SELECT list_name FROM stat_lists WHERE id=1')).scalar_one() == 'Locked list'
        assert conn.execute(text('SELECT count(*) FROM stat_list_items')).scalar_one() == 1
        assert conn.execute(text('SELECT final_value FROM stat_changes')).scalar_one() == 72
    # The same list and rider IDs in another namespace stay independently editable.
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespaces[1])
    assert client.post('/stats-editor/lists/1/rename', data={'list_name': 'Still editable'}).status_code == 302
    assert client.post('/stats-editor/lists/1/toggle-rider', data={'rider_id': 1, 'is_checked': '0'}).status_code == 302
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM stat_list_items')).scalar_one() == 0
