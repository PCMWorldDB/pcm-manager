"""Race URLs are first-party, with safe compatibility for legacy bookmarks."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import url_for

from webapp.extensions import db
from webapp.models import Role, User
from webapp.web import routes


ACTIONS = {
    'get-result': 'race_get_result',
    'save-stat-deltas': 'race_save_stat_deltas',
    'submit-for-approval': 'race_submit_for_approval',
    'approve-stat-deltas': 'race_approve_stat_deltas',
}


@pytest.mark.parametrize('prefix', ['', '/n/alpha'])
@pytest.mark.parametrize('legacy', [False, True])
def test_race_routes_require_login(client, prefix, legacy):
    base = prefix + ('/stats-editor/races/1' if legacy else '/races/1')
    for path in (base, base + '/2'):
        response = client.get(path)
        assert response.status_code == 302
        assert '/login' in response.location
    for action in ACTIONS:
        response = client.post(f'{base}/{action}')
        assert response.status_code == 302
        assert '/login' in response.location


def test_canonical_race_urls_and_legacy_action_handlers(app):
    with app.test_request_context():
        for slug in (None, 'alpha', 'beta'):
            values = {'namespace_slug': slug} if slug else {}
            prefix = f'/n/{slug}' if slug else ''
            assert url_for('web.race_detail', race_id=1, **values) == prefix + '/races/1'
            assert url_for('web.race_detail', race_id=1, stage_id=2, **values) == prefix + '/races/1/2'
            for action, endpoint in ACTIONS.items():
                assert url_for('web.' + endpoint, race_id=1, **values) == f'{prefix}/races/1/{action}'
                # Old POSTs execute the same protected handler; generated URLs use the new route.
                assert app.view_functions['web.legacy_' + endpoint] is app.view_functions['web.' + endpoint]


@pytest.mark.parametrize('allowed', [True, False])
def test_legacy_race_bookmarks_preserve_namespace_stage_and_query(app, client, monkeypatch, allowed):
    namespaces = [SimpleNamespace(id=123, slug='alpha'), SimpleNamespace(id=456, slug='beta')]
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        if allowed:
            user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        for namespace in namespaces:
            for suffix in ('', '/2'):
                base = f'/n/{namespace.slug}'
                response = client.get(f'{base}/stats-editor/races/1{suffix}?q=a%20b&q=c')
                assert response.status_code == (301 if allowed else 403)
                if allowed:
                    assert response.location == f'{base}/races/1{suffix}?q=a%20b&q=c'
                    with client.session_transaction() as session:
                        assert session['selected_namespace_id'] == namespace.id
                else:
                    assert client.get(f'{base}/races/1{suffix}').status_code == 403
        if allowed:
            assert client.get('/n/missing/stats-editor/races/1').status_code == 404
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
