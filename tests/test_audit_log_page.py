"""Audit history is permission-protected, filterable, escaped, and paginated."""
from datetime import datetime
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.models import AuditLog, Role, User


@pytest.fixture
def audit_user(app, client):
    users = []

    def login(role):
        with app.app_context():
            user = User(username=uuid4().hex, is_active=True)
            user.roles.append(Role.query.filter_by(name=role).one())
            db.session.add(user)
            db.session.commit()
            users.append(user.id)
        with client.session_transaction() as session:
            session['_user_id'] = str(users[-1])
            session['_fresh'] = True
        return users[-1]

    yield login
    with app.app_context():
        for user_id in users:
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
        db.session.commit()


@pytest.mark.parametrize('role', [
    'viewer', 'stat_editor', 'stat_editor_approver', 'release_manager',
    'release_editor', 'user_manager', 'integrations_manager',
])
def test_audit_page_denies_non_admins(app, client, audit_user, role):
    audit_user(role)
    assert client.get('/admin/audit-log').status_code == 403
    assert b'href="/admin/audit-log"' not in client.get('/settings').data


def test_audit_page_requires_login(client):
    response = client.get('/admin/audit-log')
    assert response.status_code == 302
    assert '/login' in response.location


def test_audit_page_filters_and_paginates(app, client, audit_user):
    user_id = audit_user('admin')
    prefix = uuid4().hex
    action = prefix + '_saved'
    username = prefix + '_actor'
    other_action = prefix + '_approved'
    with app.app_context():
        for i in range(51):
            db.session.add(AuditLog(
                user_id=user_id, username=username, action=action,
                resource_type=f'namespace:{i % 2 + 1}:race', resource_id=1,
                resource_name=f'fixture-{i:02d}', created_at=datetime(2026, 1, 1),
                details={'payload': '<script>alert(1)</script>'},
                user_agent='<script>agent</script>',
            ))
        db.session.add_all([
            AuditLog(user_id=user_id, username=username, action=other_action, resource_name='excluded-action'),
            AuditLog(user_id=user_id, username=prefix + '_other', action=action, resource_name='excluded-user'),
        ])
        db.session.commit()
    assert b'href="/admin/audit-log"' in client.get('/settings').data
    query = {'action': action, 'user': username}
    response = client.get('/admin/audit-log', query_string=query)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '51 matching entries' in html
    assert 'fixture-50' in html and 'fixture-00' not in html
    assert html.index('fixture-50') < html.index('fixture-49')
    assert 'excluded-action' not in html and 'excluded-user' not in html
    assert 'namespace:1:race #1' in html and 'namespace:2:race #1' in html
    assert '<script>alert(1)</script>' not in html
    assert '<script>agent</script>' not in html
    assert f'action={action}' in html and f'user={username}' in html
    page_two = client.get('/admin/audit-log', query_string={**query, 'page': 2}).get_data(as_text=True)
    assert 'fixture-00' in page_two and 'fixture-01' not in page_two
    empty = client.get('/admin/audit-log', query_string={**query, 'action': prefix + '_missing'})
    assert b'No audit entries match these filters.' in empty.data
    for page in ('bad', '-2'):
        assert client.get('/admin/audit-log', query_string={**query, 'page': page}).status_code == 200
    assert client.get('/admin/audit-log', query_string={'user': username}).status_code == 200
    assert client.get('/admin/audit-log', query_string={'action': other_action}).status_code == 200
