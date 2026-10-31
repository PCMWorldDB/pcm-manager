"""Individual edits save drafts and explicitly submit the current edits for approval."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import AuditLog, Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


@pytest.mark.parametrize('action', ['save', 'submit', 'approve'])
def test_anonymous_individual_actions_require_login(client, action):
    response = client.post(f'/riders/1/stats/{action}')
    assert response.status_code == 302
    assert '/login' in response.location


@pytest.mark.parametrize('role', ['viewer', 'stat_editor_approver'])
def test_profile_save_and_submit_denied(app, client, role):
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
        for action in ('save', 'submit'):
            assert client.post(f'/riders/1/stats/{action}').status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize('role', ['admin', 'stat_editor'])
def test_profile_save_submit_resubmit_and_approve(app, client, monkeypatch, role):
    schema = 'individual_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        NamespaceBase.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO cdb_cyclist (id,first_name,last_name,stat_mo) VALUES (1,'Test','Rider',70)"))
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    namespace = SimpleNamespace(id=123456789, slug='profile-test', database_name='worlddb_test', build_connection_string=lambda: 'selected-list-namespace')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, 'create_engine', lambda url: engine if url == 'selected-list-namespace' else pytest.fail('Wrong namespace'))
    monkeypatch.setattr(routes, '_load_namespace_roster_rider', lambda ns, rider_id: SimpleNamespace(
        cyclist_id=rider_id, team_id=None, size_cm=None, weight_kg=None, name='Test Rider',
    ))
    monkeypatch.setattr(routes, '_enrich_namespace_roster_rider_with_firstcycling', lambda rider: rider)
    monkeypatch.setattr(routes, '_namespace_roster_stats_from_rider', lambda rider: {'mo': 70})
    base = '/riders/1/stats/'
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        profile = client.get('/riders/1')
        assert profile.status_code == 200
        assert b'Edit Rider Stats' in profile.data
        assert b'stat_input_controls.js' in profile.data
        assert b'>Edit Stats<' not in profile.data
        assert b'<th>Name</th>' in profile.data
        wrong_rider = json.dumps([{'first_cycling_rider_id': 2, 'stat_name': 'mo', 'final_value': 80}])
        assert client.post(base + 'save', data={'changes_json': wrong_rider}).status_code == 400
        assert client.post(base + 'submit').status_code == 302
        for value in (72, 73):
            payload = json.dumps([{'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': value}])
            assert client.post(base + 'save', data={'changes_json': payload}).status_code == 302
            with app.app_context():
                with engine.connect() as conn:
                    fingerprint = routes._individual_pending_fingerprint(conn, 1)
                assert not routes._individual_submission_matches(namespace, 1, fingerprint)
            if role == 'admin':
                assert client.post(base + 'approve').status_code == 302
                with engine.connect() as conn:
                    assert conn.execute(text('SELECT stat_mo FROM cdb_cyclist WHERE id=1')).scalar_one() == 70
            assert client.post(base + 'submit').status_code == 302
            with app.app_context():
                with engine.connect() as conn:
                    fingerprint = routes._individual_pending_fingerprint(conn, 1)
                assert routes._individual_submission_matches(namespace, 1, fingerprint)
                assert not routes._individual_submission_matches(SimpleNamespace(id=987654321), 1, fingerprint)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM stat_lists')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM stat_changes WHERE race_result_id IS NULL AND stat_list_id IS NULL')).scalar_one() == 1
        history = client.get('/riders/1')
        assert b'Individual' in history.data
        assert b'Test Rider' in history.data
        if role == 'admin':
            with engine.begin() as conn:
                conn.execute(text('UPDATE cdb_cyclist SET stat_mo=71 WHERE id=1'))
            assert client.post(base + 'approve').status_code == 302
            with engine.connect() as conn:
                assert conn.execute(text('SELECT stat_mo FROM cdb_cyclist WHERE id=1')).scalar_one() == 71
            with engine.begin() as conn:
                conn.execute(text('UPDATE cdb_cyclist SET stat_mo=70 WHERE id=1'))
            assert client.post(base + 'approve').status_code == 302
            with engine.connect() as conn:
                assert conn.execute(text('SELECT stat_mo FROM cdb_cyclist WHERE id=1')).scalar_one() == 73
            with engine.begin() as conn:
                conn.execute(text('UPDATE stat_changes SET release_id=7 WHERE cdb_cyclist_id=1'))
            payload = json.dumps([{'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': 74}])
            assert client.post(base + 'save', data={'changes_json': payload}).status_code == 302
            with engine.connect() as conn:
                released = conn.execute(text('SELECT final_value,is_approved FROM stat_changes WHERE release_id=7')).one()
                pending = conn.execute(text('SELECT original_value,final_value,is_approved FROM stat_changes WHERE release_id IS NULL')).one()
                assert tuple(released) == (73, True)
                assert tuple(pending) == (73, 74, False)
        else:
            assert client.post(base + 'approve').status_code == 403
        namespace.database_name = 'wrong_namespace'
        with pytest.raises(RuntimeError, match='expected namespace database wrong_namespace'):
            client.post(base + 'save', data={'changes_json': '[]'})
    finally:
        engine.dispose()
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()


def test_profile_editor_templates_parse():
    from pathlib import Path
    from jinja2 import Environment

    env = Environment()
    for name in ('rider_detail.html', 'stat_list_detail.html'):
        env.parse((Path(__file__).resolve().parents[1] / 'webapp/templates' / name).read_text(encoding='utf-8'))
