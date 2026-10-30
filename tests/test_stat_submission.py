"""Explicit race review submission uses saved edits from the selected namespace."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import AuditLog, FirstCyclingRace, Role, User
from webapp.web import routes


@pytest.mark.parametrize('role,expected', [
    ('admin', 404), ('stat_editor', 404),
    ('stat_editor_approver', 403), ('viewer', 403),
])
def test_submit_and_save_permissions(app, client, role, expected):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        for action in ('submit-for-approval', 'save-stat-deltas'):
            assert client.post(f'/stats-editor/races/999999999/{action}').status_code == expected
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_submit_counts_stage_edits_only_in_selected_namespace(app, client, monkeypatch):
    schema = 'submit_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE stat_changes (id serial PRIMARY KEY, race_result_id integer, stat_list_id integer, is_approved boolean)'))
            conn.execute(text('INSERT INTO stat_changes (race_result_id, stat_list_id, is_approved) VALUES (11,NULL,FALSE), (12,NULL,FALSE), (12,NULL,TRUE), (99,NULL,FALSE), (11,1,FALSE)'))
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        race = FirstCyclingRace(race_id=987654321, season_year=2026, name='Submission test')
        db.session.add_all([user, race])
        db.session.commit()
        user_id, race_pk = user.id, race.id
    namespace = SimpleNamespace(id=123456789, build_connection_string=lambda: 'selected-namespace')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, 'create_engine', lambda url: engine if url == 'selected-namespace' else pytest.fail('Wrong namespace'))
    monkeypatch.setattr(routes, '_load_firstcycling_race_result_map_ids', lambda *a, **kw: [11, 12])
    monkeypatch.setattr(routes, '_load_scope_stat_change_conflicts', lambda *a: [])
    applied = []
    monkeypatch.setattr(routes, '_apply_scope_stat_changes_to_cdb', lambda *a: applied.append(True) or 2)
    monkeypatch.setattr(routes, 'notify_discord_race_stat_approved', lambda *a, **kw: False)
    sent = []
    monkeypatch.setattr(routes, 'notify_discord_race_stat_saved', lambda *a, **kw: sent.append(a) or True)
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        path = '/stats-editor/races/987654321/submit-for-approval'
        approve_path = '/stats-editor/races/987654321/approve-stat-deltas'
        assert client.post(approve_path).status_code == 302
        assert not applied
        assert client.post(path, data={'stage_id': '12'}).status_code == 400
        assert not sent
        with app.app_context():
            assert not routes._race_is_submitted_for_approval(namespace, 987654321)
        assert client.post(path).status_code == 302
        assert len(sent) == 1
        assert sent[0][0] is namespace
        assert sent[0][4] is None
        assert sent[0][6] == 2
        with app.app_context():
            assert routes._race_is_submitted_for_approval(namespace, 987654321)
            other = SimpleNamespace(id=987654321)
            with engine.connect() as conn:
                fingerprint = routes._race_pending_submission_fingerprint(conn, [11, 12])
            assert not routes._race_submission_matches(other, 987654321, fingerprint)
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO stat_changes (race_result_id, is_approved) VALUES (11,FALSE)'))
        with app.app_context():
            assert not routes._race_is_submitted_for_approval(namespace, 987654321)
        assert client.post(approve_path).status_code == 302
        assert not applied
        assert client.post(path).status_code == 302
        assert client.post(approve_path).status_code == 302
        assert applied == [True]
        with app.app_context():
            approvals = AuditLog.query.filter_by(user_id=user_id, action='race_stats_approved').all()
            assert len(approvals) == 1
            assert approvals[0].resource_type == f'namespace:{namespace.id}:race'
            assert approvals[0].resource_id == 987654321
            assert approvals[0].details['approved_count'] == 3
            assert approvals[0].details['applied_count'] == 2
        assert client.post(path).status_code == 302
        assert len(sent) == 2
    finally:
        engine.dispose()
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.delete(db.session.get(FirstCyclingRace, race_pk))
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()
