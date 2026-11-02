"""The Log tab shares stats read permissions and paginates namespace history."""
from types import SimpleNamespace
from datetime import datetime
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.models import Role, User
from webapp.web import routes


@pytest.mark.parametrize('race_present', [True, False])
@pytest.mark.parametrize('stage,stage_label', [
    (None, 'Stage #42'),
    (SimpleNamespace(stage_name='Final Stage', stage_number=3), 'Final Stage'),
    (SimpleNamespace(stage_name=None, stage_number=3), 'Stage #3'),
    (SimpleNamespace(stage_name=None, stage_number=None), 'Stage #42'),
])
def test_log_race_result_source_handles_missing_metadata(app, race_present, stage, stage_label):
    mapped = SimpleNamespace(external_race_id='123', external_stage_id='42', race_result_id=9)
    races = {123: SimpleNamespace(name='Test Race')} if race_present else {}
    stages = {42: stage} if stage is not None else {}
    with app.test_request_context():
        source = routes._race_result_source_info(mapped, races, stages)
    assert source['type'] == 'Race Result'
    assert source['label'] == f"{'Test Race' if race_present else 'Race #123'} - {stage_label}"


@pytest.mark.parametrize('allowed', [True, False])
def test_stats_log_access_and_pagination(app, client, monkeypatch, allowed):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        if allowed:
            user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    namespace = SimpleNamespace(id=67890, slug='log-test', database_year=2026, gender='Men')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    calls = []

    def load_log(selected, **kwargs):
        calls.append((selected, kwargs))
        return [{'type_label': 'Individual', 'name': 'Full Name', 'detail_url': '/riders/1',
                 'riders_edited': 1, 'total_edits': 3,
                 'approved_on': datetime(2026, 11, 2, 12, 30, 15),
                 'approval_requested_on': datetime(2026, 11, 1, 10, 20, 30),
                 'classification': '-', 'releases': [{'name': 'Test v1', 'url': '/releases?release_id=7&tab=stats'}],
                 'is_approved': False, 'is_released': False,
                 'approval_status': 'Pending', 'release_status': 'Not Released'}] * 101

    monkeypatch.setattr(routes, '_load_namespace_approved_release_items', load_log)
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        switched = client.post('/stats-editor', data={'action': 'set_method', 'method': 'log'})
        if not allowed:
            assert switched.status_code == 403
            assert client.get('/stats-editor').status_code == 403
            assert not calls
            return
        assert switched.status_code == 302
        response = client.get('/stats-editor?log_page=2')
        assert response.status_code == 200
        assert b'Stat Edit Log' in response.data
        assert b'Individual' in response.data
        assert b'Page 2' in response.data
        assert b'Previous' in response.data and b'Next' in response.data
        assert response.data.count(b'>Full Name</a>') == 100
        assert b'Approved On' in response.data and b'Approval Requested On' in response.data
        assert b'2026-11-02 12:30:15' in response.data and b'2026-11-01 10:20:30' in response.data
        assert b'release_id=7' in response.data and b'Test v1' in response.data
        assert calls == [(namespace, {'log_mode': True, 'limit': 101, 'offset': 100})]
        client.get('/stats-editor?log_page=-1')
        assert calls[-1][1]['offset'] == 0
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
