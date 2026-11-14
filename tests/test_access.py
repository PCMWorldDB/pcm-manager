"""Exercise real authentication and seeded permissions against PostgreSQL."""
from uuid import uuid4

import pytest
from sqlalchemy import text

from webapp.extensions import db
from webapp.models import Role, User


def test_startup_initializes_postgres_and_roles(app, client):
    assert client.get("/health").json == {"status": "healthy"}
    with app.app_context():
        assert db.engine.dialect.name == "postgresql"
        assert Role.query.filter_by(name="stat_editor").one()
        assert db.session.execute(text("SELECT current_database()")).scalar() == "worlddb_test"


@pytest.mark.parametrize("path", [
    "/admin/namespaces/create",
    "/races/999/approve-stat-deltas",
    "/stats-editor/lists/999/approve-stat-deltas",
    "/stats-editor/lists/999/submit-for-approval",
])
def test_anonymous_cannot_post_protected_actions(client, path):
    response = client.post(path)
    assert response.status_code == 302
    assert "/login" in response.location


@pytest.mark.parametrize("role_name", ["user_manager"])
def test_user_manager_cannot_administer(app, client, role_name):
    with app.app_context():
        user = User(username=f"test-{uuid4().hex}", is_active=True)
        user.roles.append(Role.query.filter_by(name=role_name).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        assert not user.has_permission("activity-monitor", "read")
    try:
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        for path in (
            "/admin/namespaces/create",
            "/races/999/approve-stat-deltas",
            "/stats-editor/lists/999/approve-stat-deltas",
        ):
            assert client.post(path).status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize("path", [
    "/activity-monitor",
    "/activity-monitor/history-transfer",
    "/activity-monitor/riders",
    "/activity-monitor/problems",
    "/activity-monitor/automation",
    "/activity-monitor/run-checks/schedule-status",
    "/activity-monitor/run-checks",
    "/activity-monitor/run-checks/queue",
    "/activity-monitor/run-checks/999/stop",
    "/activity-monitor/run-checks/999/resume",
    "/activity-monitor/riders/999",
    "/activity-monitor/riders/999/research-checks",
    "/activity-monitor/riders/999/pcs-profile",
    "/activity-monitor/riders/999/pcs-profile/approve",
    "/activity-monitor/riders/999/pcs-profile/clear",
])
@pytest.mark.parametrize("method", ["get", "post"])
def test_removed_activity_monitor_endpoints_return_not_found(client, path, method):
    assert getattr(client, method)(path).status_code == 404


def test_activity_monitor_is_not_registered_as_a_panel(app):
    from webapp.web.routes import APP_PANELS

    assert 'activity_monitor' not in APP_PANELS
    assert not any(rule.rule.startswith('/activity-monitor') for rule in app.url_map.iter_rules())


@pytest.mark.parametrize(('role_name', 'can_view_activity'), [
    ('user_manager', False),
    ('release_editor', True),
])
def test_rider_activity_keeps_seeded_permissions_without_monitor_panel(app, role_name, can_view_activity):
    from flask_login import login_user
    from webapp.web.routes import _can_view_rider_activity

    with app.test_request_context():
        user = User(username=f'test-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name=role_name).one())
        db.session.add(user)
        db.session.commit()
        try:
            login_user(user)
            assert _can_view_rider_activity() is can_view_activity
        finally:
            db.session.delete(user)
            db.session.commit()


def test_superadmin_navigation_has_no_activity_monitor_links(app, client):
    with app.app_context():
        user = User(username=f'test-{uuid4().hex}', is_active=True, is_superadmin=True)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        for path in ('/dashboard', '/namespaces'):
            response = client.get(path)
            assert response.status_code == 200
            assert b'/activity-monitor' not in response.data
            assert b'Activity Monitor' not in response.data
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
