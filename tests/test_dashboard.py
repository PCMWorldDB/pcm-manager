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
    races = [SimpleNamespace(race_id=i, name=f'Race {i}', end_date=end, classification='1.1', season_year=2026, gender='Men')
             for i, end in [(1, yesterday), (2, yesterday), (3, yesterday),
                            (4, yesterday + timedelta(days=1)), (5, None)]]
    namespace = SimpleNamespace(slug='selected', database_year=2026, gender='Men')
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
    ('stat_editor', ['Race Results Ready for Stat Editing']),
    ('stat_editor_approver', ['Stat Edits Ready for Review']),
    ('admin', ['Releases Ready for Review', 'Stat Edits Ready for Review',
               'Race Results Ready for Stat Editing']),
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
                        'Race Results Ready for Stat Editing'):
            assert (heading in page) == (heading in headings)
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_classification_shortcut_and_waiting_edits_filter(app, client, monkeypatch):
    from uuid import uuid4

    from webapp.extensions import db
    from webapp.models import Role, User
    from webapp.models.main.firstcycling import FirstCyclingRace

    today = datetime.utcnow().date()
    year = today.year
    namespace = SimpleNamespace(id=123456, slug='shortcut', database_year=year, gender='Men')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda namespaces: namespace)
    monkeypatch.setattr(routes, '_load_namespace_stage_delta_status', lambda namespace, ids: {})
    build_result_state = routes._build_race_result_state

    def result_state(race):
        state = build_result_state(race)
        state['is_result_saved'] = race.name == 'downloaded'
        return state

    monkeypatch.setattr(routes, '_build_race_result_state', result_state)
    monkeypatch.setattr(routes, 'render_template', lambda template, **context: {
        'races': [race.name for race in context['races']],
        'filters': context['selected_filters'], 'method': context['active_method'],
    })
    with app.app_context():
        user = User(username=f'shortcut-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        names = ['untouched', 'saved', 'approved', 'today', 'ongoing', 'other-class', 'unknown-end', 'downloaded']
        race_rows = [FirstCyclingRace(
            race_id=980000 + i, season_year=year, gender='Men', name=name,
            classification='2.1' if name == 'other-class' else '1.1',
            start_date=today - timedelta(days=5),
            end_date=(None if name == 'unknown-end' else today if name == 'today'
                      else today + timedelta(days=1) if name == 'ongoing'
                      else today - timedelta(days=1)),
        ) for i, name in enumerate(names)]
        db.session.add_all(race_rows)
        db.session.commit()
        user_id = user.id
        row_ids = [row.id for row in race_rows]
    statuses = {980001: {'total_changes': 2, 'approved_changes': 0},
                980002: {'total_changes': 2, 'approved_changes': 2}}
    monkeypatch.setattr(routes, '_load_namespace_race_delta_status', lambda namespace, ids: statuses)
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            session['stats_editor_state_by_namespace'] = {
                str(namespace.id): {'active_method': 'by_list', 'race_filters': {'nation': 'old'}},
                'other': {'active_method': 'by_list'},
            }
        response = client.post('/stats-editor', data={
            'action': 'dashboard_classification', 'classification': '1.1',
        })
        assert response.status_code == 302
        with client.session_transaction() as session:
            assert session['stats_editor_state_by_namespace']['other'] == {'active_method': 'by_list'}
        result = client.get('/stats-editor').get_json()
        assert set(result['races']) == {'untouched', 'downloaded', 'unknown-end'}
        assert result['method'] == 'by_race'
        assert result['filters']['start_date_to'] == today.isoformat()
        assert result['filters']['edit_statuses'] == ['waiting_edits']
        assert result['filters']['nation'] == ''
        response = client.post('/stats-editor', data={
            'action': 'apply_filters', 'classification': '1.1',
            'start_date_to': today.isoformat(), 'edit_status': 'waiting_edits',
        })
        assert response.status_code == 302
        assert set(client.get('/stats-editor').get_json()['races']) == {'untouched', 'downloaded', 'unknown-end'}
        for legacy_status in ('no_edits', 'pending_edits'):
            with client.session_transaction() as session:
                states = dict(session['stats_editor_state_by_namespace'])
                states[str(namespace.id)]['race_filters']['edit_statuses'] = [legacy_status]
                session['stats_editor_state_by_namespace'] = states
            result = client.get('/stats-editor').get_json()
            assert result['filters']['edit_statuses'] == ['waiting_edits']
            assert set(result['races']) == {'untouched', 'downloaded', 'unknown-end'}
        client.post('/stats-editor', data={'action': 'clear_filters'})
        with client.session_transaction() as session:
            filters = session['stats_editor_state_by_namespace'][str(namespace.id)]['race_filters']
            assert filters['start_date_to'] == ''
            assert filters['edit_statuses'] == []
    finally:
        with app.app_context():
            for row_id in row_ids:
                db.session.delete(db.session.get(FirstCyclingRace, row_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
