"""Dashboard queues use workflow permissions and namespace-specific statuses."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from webapp.web import routes


@pytest.mark.parametrize('release,review,editing', [
    (False, False, False), (True, False, False),
    (False, True, False), (False, False, True), (True, True, True),
])
def test_dashboard_task_queues(app, release, review, editing):
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    races = [SimpleNamespace(race_id=i, name=f'Race {i}', end_date=end, classification='1.1')
             for i, end in [(1, yesterday), (2, yesterday), (3, yesterday),
                            (4, yesterday + timedelta(days=1)), (5, None)]]
    namespace = SimpleNamespace(slug='selected')
    with app.test_request_context(), \
            patch.object(routes, '_can_approve_releases', return_value=release), \
            patch.object(routes, '_can_approve_stats', return_value=review), \
            patch.object(routes, '_can_update_stats', return_value=editing), \
            patch.object(routes, '_can_access_panel', return_value=True), \
            patch.object(routes, '_load_namespace_releases') as releases, \
            patch.object(routes, '_load_namespace_race_delta_status') as statuses, \
            patch.object(routes, '_load_namespace_stat_lists') as lists, \
            patch.object(routes.FirstCyclingRace, 'query') as query:
        query.order_by.return_value.all.return_value = races
        releases.return_value = [SimpleNamespace(status=status) for status in
                                 ('draft', 'review', 'pending_approval', 'approved', 'published')]
        statuses.return_value = {2: {'total_changes': 2, 'approved_changes': 1},
                                 3: {'total_changes': 1, 'approved_changes': 1}}
        lists.return_value = [dict(id=i, list_name=f'List {i}', total_changes=total,
                                   approved_changes=approved)
                             for i, total, approved in [(1, 2, 0), (2, 1, 1), (3, 0, 0)]]
        tasks = routes._load_dashboard_tasks(namespace)
        assert len(tasks['releases']) == (2 if release else 0)
        assert [item['label'] for item in tasks['reviews']] == (
            ['Race 2', 'List 1'] if review else [])
        assert tasks['editing'] == ({'1.1': [races[0]]} if editing else {})
        if review or editing:
            assert statuses.call_args.args[0] is namespace
        else:
            statuses.assert_not_called()
        assert releases.called == release
        assert lists.called == review
        empty = routes._load_dashboard_tasks(None)
        assert not empty['releases'] and not empty['reviews'] and not empty['editing']

@pytest.mark.parametrize('role_name,headings', [
    ('viewer', []),
    ('release_editor', []),
    ('release_manager', ['Releases Ready for Review']),
    ('stat_editor', ['Race Results Ready for Editing']),
    ('stat_editor_approver', ['Stat Edits Ready for Review']),
    ('admin', ['Releases Ready for Review', 'Stat Edits Ready for Review',
               'Race Results Ready for Editing']),
])
def test_dashboard_role_visibility(app, client, monkeypatch, role_name, headings):
    from uuid import uuid4

    from webapp.extensions import db
    from webapp.models import Role, User

    namespace = SimpleNamespace(id=999, slug='dashboard-test')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda namespaces: namespace)
    monkeypatch.setattr(routes, '_load_namespace_releases', lambda namespace: [])
    monkeypatch.setattr(routes, '_load_namespace_race_delta_status', lambda namespace, ids: {})
    monkeypatch.setattr(routes, '_load_namespace_stat_lists', lambda namespace: [])
    with app.app_context():
        user = User(username=f'dashboard-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name=role_name).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.get('/dashboard')
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert 'Waiting On You...' in page
        assert 'Main Team Count' not in page
        for heading in ('Releases Ready for Review', 'Stat Edits Ready for Review',
                        'Race Results Ready for Editing'):
            assert (heading in page) == (heading in headings)
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
