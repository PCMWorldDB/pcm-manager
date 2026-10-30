"""Workflow audit entries persist actors and namespace-qualified resources."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import AuditLog, FirstCyclingRace, Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


@pytest.fixture
def actor(app, client):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    yield user_id
    with app.app_context():
        AuditLog.query.filter_by(user_id=user_id).delete()
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()


def test_release_audit_lifecycle(app, client, actor, monkeypatch):
    namespace = SimpleNamespace(id=101)
    release = SimpleNamespace(id=1, version='1.0', release_name='Audit', status='draft',
                              published_by_id=None, published_at=None)
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, '_next_release_version', lambda _: '1.0')
    monkeypatch.setattr(routes, '_create_namespace_release', lambda *a: release)
    monkeypatch.setattr(routes, '_load_namespace_releases', lambda _: [release])
    monkeypatch.setattr(routes, '_update_namespace_release_fields', lambda *a: None)
    monkeypatch.setattr(routes, '_build_review_artifacts_for_release', lambda *a: None)
    assert client.post('/releases?tab=create', data={
        'included_change_types': 'stats_editor', 'release_name': 'Audit',
    }).status_code == 302
    path = '/releases?tab=overview&release_id=1'
    # Invalid transition and denied approval must not produce audit entries.
    assert client.post(path, data={'status_target': 'published'}).status_code == 302
    for status in ('review', 'draft', 'review', 'approved', 'published'):
        assert client.post(path, data={'status_target': status}).status_code == 302
    with app.app_context():
        logs = AuditLog.query.filter_by(user_id=actor).order_by(AuditLog.id).all()
        assert [row.action for row in logs] == ['release_created'] + ['release_status_changed'] * 5
        assert logs[0].details == {'version': '1.0', 'status': 'draft'}
        assert [row.details for row in logs[1:]] == [
            {'from_status': old, 'to_status': new}
            for old, new in zip(('draft', 'review', 'draft', 'review', 'approved'),
                                ('review', 'draft', 'review', 'approved', 'published'))
        ]
        assert all(row.resource_type == 'namespace:101:release' and row.resource_id == 1 for row in logs)
        user = db.session.get(User, actor)
        user.roles = [Role.query.filter_by(name='viewer').one()]
        db.session.commit()
    release.status = 'review'
    assert client.post(path, data={'status_target': 'approved'}).status_code == 403
    with app.app_context():
        assert AuditLog.query.filter_by(user_id=actor).count() == 6


def test_race_save_audit_includes_resets_and_namespace(app, client, actor, monkeypatch):
    schema = 'audit_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        NamespaceBase.metadata.create_all(engine)
        race = FirstCyclingRace(race_id=987123456, season_year=2026, name='Audit race')
        db.session.add(race)
        db.session.commit()
        race_pk = race.id
    namespace = SimpleNamespace(id=201, build_connection_string=lambda: 'audit-namespace')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, 'create_engine', lambda url: engine if url == 'audit-namespace' else pytest.fail('Wrong namespace'))
    monkeypatch.setattr(routes, '_get_firstcycling_race_result_map', lambda *a: SimpleNamespace(race_result_id=1))
    try:
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO cdb_cyclist (id, first_cycling_id, stat_mo) VALUES (1, 123, 70)'))
        path = '/stats-editor/races/987123456/save-stat-deltas'
        for ns_id, value in ((201, 75), (202, 70)):
            namespace.id = ns_id
            assert client.post(path, data={'changes_json': json.dumps([{
                'first_cycling_rider_id': 123, 'stat_name': 'mo', 'final_value': value,
            }])}).status_code == 302
        assert client.post(path, data={'changes_json': 'invalid'}).status_code == 302
        with app.app_context():
            logs = AuditLog.query.filter_by(user_id=actor, action='race_stats_saved').order_by(AuditLog.id).all()
            assert len(logs) == 2
            assert [row.resource_type for row in logs] == ['namespace:201:race', 'namespace:202:race']
            assert [row.details['changes'][0]['final_value'] for row in logs] == [75, 70]
            assert all(row.resource_id == 987123456 and row.details['changes'][0]['original_value'] == 70 for row in logs)
    finally:
        engine.dispose()
        with app.app_context():
            db.session.delete(db.session.get(FirstCyclingRace, race_pk))
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()
