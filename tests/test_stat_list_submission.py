"""Stat lists save drafts and explicitly submit the current edits for approval."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import AuditLog, Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


@pytest.mark.parametrize('role', ['viewer', 'stat_editor_approver'])
def test_list_save_and_submit_denied(app, client, role):
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
        for action in ('save-stat-deltas', 'submit-for-approval'):
            assert client.post(f'/stats-editor/lists/1/{action}').status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize('role', ['admin', 'stat_editor'])
def test_list_save_submit_resubmit_and_approve(app, client, monkeypatch, role):
    schema = 'list_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        NamespaceBase.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO rider_list (id,list_name,season_year,gender) VALUES (1,'Test list',2026,'Men'),(2,'Other list',2026,'Men')"))
            conn.execute(text("INSERT INTO rider (id,first_cycling_id,stat_mo) VALUES (1,2,70),(2,1,70)"))
            conn.execute(text('INSERT INTO rider_list_item (rider_list_id,rider_id) VALUES (1,1)'))
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    namespace = SimpleNamespace(id=123456789, database_name='worlddb_test', slug='list-test', build_connection_string=lambda: 'selected-list-namespace')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, 'create_engine', lambda url: engine if url == 'selected-list-namespace' else pytest.fail('Wrong namespace'))
    monkeypatch.setattr(routes, '_load_scope_stat_change_conflicts', lambda *a: [])
    sent = []
    monkeypatch.setattr(routes, 'notify_discord_stat_list_saved', lambda *a, **kw: sent.append(a) or True)
    monkeypatch.setattr(routes, 'notify_discord_stat_list_approved', lambda *a, **kw: False)
    base = '/stats-editor/lists/1/'
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        # Namespace and FirstCycling IDs overlap and point at different riders.
        list_two = '/stats-editor/lists/2/'
        for data, expected in (({'rider_id': 1, 'first_cycling_id': 1}, [1]),
                               ({'first_cycling_id': 1}, [1, 2])):
            assert client.post(list_two + 'add-rider', data=data).status_code == 302
            with engine.connect() as conn:
                assert conn.execute(text(
                    'SELECT rider_id FROM rider_list_item WHERE rider_list_id=2 ORDER BY rider_id'
                )).scalars().all() == expected
        for data, expected in (({'rider_id': 1, 'is_checked': 'false'}, [2]),
                               ({'first_cycling_id': 1, 'is_checked': 'false'}, [])):
            assert client.post(list_two + 'toggle-rider', data=data).status_code == 302
            with engine.connect() as conn:
                assert conn.execute(text(
                    'SELECT rider_id FROM rider_list_item WHERE rider_list_id=2 ORDER BY rider_id'
                )).scalars().all() == expected
        assert client.post(base + 'submit-for-approval').status_code == 302
        assert not sent
        for value in (72, 73):
            payload = json.dumps([{'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': value}])
            assert client.post(base + 'save-stat-deltas', data={'changes_json': payload}).status_code == 302
            with app.app_context():
                assert not routes._list_is_submitted_for_approval(namespace, 1)
            assert len(sent) == value - 72
            if role == 'admin':
                assert client.post(base + 'approve-stat-deltas').status_code == 302
                with engine.connect() as conn:
                    assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar_one() == 70
            assert client.post(base + 'submit-for-approval').status_code == 302
            assert sent[-1][0] is namespace
            assert sent[-1][4] == 1
            with app.app_context():
                assert routes._list_is_submitted_for_approval(namespace, 1)
                with engine.connect() as conn:
                    fingerprint = routes._list_pending_submission_fingerprint(conn, 1)
                assert not routes._list_submission_matches(SimpleNamespace(id=987654321), 1, fingerprint)
        if role == 'admin':
            assert client.post(base + 'approve-stat-deltas').status_code == 302
            with engine.connect() as conn:
                assert conn.execute(text('SELECT stat_mo FROM rider WHERE id=1')).scalar_one() == 73
        else:
            assert client.post(base + 'approve-stat-deltas').status_code == 403
    finally:
        engine.dispose()
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()

