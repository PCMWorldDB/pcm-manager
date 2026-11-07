"""Object navigation keeps dedicated pages and existing permission boundaries."""
from uuid import uuid4
from types import SimpleNamespace

import pytest

from webapp.extensions import db
from webapp.models import Role, User
from webapp.web import routes


PATHS = [
    '/teams', '/riders', '/races', '/riders/bulk-stat-editor',
    '/riders/stat-edit-log', '/teams/review', '/riders/review', '/races/review',
    '/teams/change-log', '/riders/change-log', '/races/change-log',
]


@pytest.mark.parametrize('path', PATHS)
def test_object_pages_require_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert '/login' in response.location


@pytest.mark.parametrize('allowed', [True, False])
def test_object_pages_and_permissions(app, client, monkeypatch, allowed):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        if allowed:
            user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [])
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            # Stale tab state must not override a dedicated page URL.
            session['team_panel_state_by_namespace'] = {'none': {'active_method': 'riders'}}
        for path in PATHS:
            response = client.get(path)
            assert response.status_code == (200 if allowed else 403), path
            if allowed:
                assert b'>Review</a>' in response.data
                if path.endswith('/review'):
                    assert b'Select a namespace to review' in response.data
                if path.endswith('/change-log'):
                    assert b'Select a namespace to view change history.' in response.data
                    assert b'class="nav-link active" href="' + path.encode() + b'">Change Log</a>' in response.data
        if allowed:
            assert b'<h2 class="mb-1">Teams</h2>' in client.get('/teams').data
            assert b'<h2 class="mb-1">Riders</h2>' in client.get('/riders').data
            assert b'<h2 class="mb-1">Bulk Stat Editor</h2>' in client.get('/riders/bulk-stat-editor').data
            assert b'<h2 class="mb-1">Races</h2>' in client.get('/races').data
            response = client.post('/riders', data={'action': 'apply_filters', 'q': 'Example'})
            assert response.location.endswith('/riders')
            assert client.get('/stats-editor').location.endswith('/races')
            page = client.get('/teams').data
            labels = [b'>Namespace</span>', b'>Teams</span>', b'>Riders</span>', b'>Races</span>', b'>Releases</span>']
            assert [page.index(label) for label in labels] == sorted(page.index(label) for label in labels)
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_review_namespace_urls_do_not_fall_back(app, client, monkeypatch):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    namespaces = [SimpleNamespace(id=index, slug=slug) for index, slug in [(123, 'alpha'), (456, 'beta')]]
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
    monkeypatch.setattr(routes, '_load_attribute_review', lambda namespace, entity_type, include_history=False: [])
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        for namespace in namespaces:
            for section in ['teams', 'riders', 'races']:
                response = client.get(f'/n/{namespace.slug}/{section}/review')
                assert response.status_code == 200
                assert f'href="/n/{namespace.slug}/{section}/review"'.encode() in response.data
                assert b'No pending attribute changes.' in response.data
                with client.session_transaction() as session:
                    assert session['selected_namespace_id'] == namespace.id
                response = client.get(f'/n/{namespace.slug}/{section}/change-log')
                assert response.status_code == 200
                assert f'href="/n/{namespace.slug}/{section}/change-log"'.encode() in response.data
                assert b'No attribute change events recorded.' in response.data
                for heading in [b'Occurred On', b'Attribute', b'Before', b'After', b'Source', b'Status']:
                    assert heading in response.data
        assert client.get('/n/missing/riders/review').status_code == 404
        assert client.get('/n/missing/riders/change-log').status_code == 404
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
