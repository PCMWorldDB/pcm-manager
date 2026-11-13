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
def test_user_manager_cannot_access_activity_monitor_or_administer(app, client, role_name):
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
        assert client.get("/activity-monitor").status_code == 403
        assert client.get("/activity-monitor/riders").status_code == 403
        for path in (
            "/admin/namespaces/create",
            "/races/999/approve-stat-deltas",
            "/stats-editor/lists/999/approve-stat-deltas",
            "/activity-monitor/history-transfer",
        ):
            assert client.post(path).status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_anonymous_cannot_view_activity_monitor(client):
    response = client.get("/activity-monitor")
    assert response.status_code == 302
    assert "/login" in response.location


def test_anonymous_cannot_open_activity_monitor_history_transfer(client):
    response = client.get("/activity-monitor/history-transfer")
    assert response.status_code == 302
    assert "/login" in response.location


def test_namespace_read_only_role_cannot_transfer_activity_monitor_history(app, client):
    with app.app_context():
        user = User(username=f"test-{uuid4().hex}", is_active=True)
        user.roles.append(Role.query.filter_by(name="release_editor").one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        assert user.has_permission("activity-monitor", "read")
        assert user.has_permission("namespaces", "read")
        assert not user.has_permission("namespaces", "create")
        assert not user.has_permission("namespaces", "update")
    try:
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        assert client.get("/activity-monitor/history-transfer").status_code == 403
        assert client.post("/activity-monitor/history-transfer", data={"action": "export"}).status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
