"""Non-stat proposals preserve approval, namespace, and transaction boundaries."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations.cdb import attribute_changes as changes
from webapp.models import Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


@pytest.fixture
def attribute_namespaces(app, monkeypatch):
    urls, engines, namespaces = [], [], []
    try:
        for index, slug in enumerate(('attribute-alpha', 'attribute-beta')):
            url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO rider (id,first_name,birth_date,stat_mo) VALUES (1,'Test',20000101,70)"))
                conn.execute(text("INSERT INTO team (id,team_name) VALUES (1,'Original')"))
            namespaces.append(SimpleNamespace(
                id=9900 + index, name=slug, slug=slug, database_name=url.database,
                database_year=2026, gender='Men',
                build_connection_string=lambda url=url: url))
        monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: namespaces)
        yield namespaces, engines
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)


def login(app, client, role):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        username = user.username
    with client.session_transaction() as session:
        session['_user_id'] = user_id
        session['_fresh'] = True
    return username


def test_attribute_proposal_review_and_namespace_isolation(app, client, attribute_namespaces):
    namespaces, engines = attribute_namespaces
    username = login(app, client, 'admin')
    page = client.get('/n/attribute-alpha/riders/1')
    assert page.status_code == 200
    assert b'name="attribute_name" value="birth_date"' in page.data
    assert b'type="date" name="value" value="2000-01-01"' in page.data
    assert b'aria-label="Edit Birth Date"' in page.data
    assert b'aria-label="Save Birth Date for review"' in page.data
    for ns in namespaces:
        result = client.post(f'/n/{ns.slug}/attributes/rider/1/propose',
                             data={'attribute_name': 'birth_date', 'value': '2001-02-03'})
        assert result.status_code == 302
        assert f'/n/{ns.slug}/riders/1' in result.location
    for engine in engines:
        with engine.begin() as conn:
            assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
            conn.execute(text("UPDATE cdb_changes SET created_at='2026-11-07 02:08:02.831958+00:00'"))
    page = client.get('/n/attribute-alpha/riders/review')
    assert page.status_code == 200
    assert b'birth_date' in page.data and b'20010203' in page.data
    assert b'Approve' in page.data
    assert b'<th>Rider</th>' in page.data and b'<th>Submitted On</th>' in page.data
    assert b'href="/n/attribute-alpha/riders/1">Test</a>' in page.data
    assert username.encode() in page.data
    assert b'2026-11-07 02:08:02' in page.data and b'831958' not in page.data
    profile = client.get('/n/attribute-alpha/riders/1')
    assert b'aria-label="Birth Date pending approval"' in profile.data
    assert b'aria-label="Edit Birth Date"' not in profile.data
    context = None
    with app.test_request_context():
        # The actual form reads namespace data rather than enriched display values.
        with engines[0].connect() as conn:
            context = changes.editor_fields(conn, 'rider', 1)
    assert any(field['name'] == 'birth_date' and field['value'] == '2000-01-01' for field in context)
    assert not any(field['name'].startswith('stat_') for field in context)
    assert client.post('/n/attribute-beta/attributes/rider/review/1/approve').status_code == 302
    history = client.get('/n/attribute-beta/riders/change-log')
    assert history.status_code == 200
    assert b'<td>Approved</td>' in history.data and b'<td>Applied</td>' in history.data
    assert b'birth_date' in history.data and b'20010203' in history.data
    assert username.encode() in history.data
    assert b'href="/n/attribute-beta/riders/1">Test</a>' in history.data
    alpha_history = client.get('/n/attribute-alpha/riders/change-log')
    assert b'<td>Created</td>' in alpha_history.data
    assert b'<td>Applied</td>' not in alpha_history.data
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
    with engines[1].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20010203
        assert conn.execute(text('SELECT status FROM cdb_changes WHERE id=1')).scalar() == 'approved'
        assert set(conn.execute(text('SELECT event_type FROM cdb_change_events')).scalars()) == {'created', 'approved', 'applied'}
    assert client.post('/n/attribute-alpha/attributes/rider/review/1/reject').status_code == 302
    with engines[0].connect() as conn:
        assert conn.execute(text('SELECT birth_date FROM rider WHERE id=1')).scalar() == 20000101
        assert conn.execute(text('SELECT status FROM cdb_changes WHERE id=1')).scalar() == 'rejected'
    assert client.post('/n/missing/attributes/rider/1/propose',
                       data={'attribute_name': 'birth_date', 'value': '2001-02-03'}).status_code == 404


@pytest.mark.parametrize('role', ['stat_editor', 'stat_editor_approver', 'viewer'])
def test_stats_roles_cannot_write_or_approve_attributes(app, client, role):
    login(app, client, role)
    assert client.post('/attributes/rider/1/propose').status_code == 403
    assert client.post('/attributes/rider/review/1/approve').status_code == 403


def test_attribute_conflicts_validation_and_rollback(app, attribute_namespaces, monkeypatch):
    _, engines = attribute_namespaces
    engine = engines[0]
    with engine.begin() as conn:
        for field, value in [('id', '2'), ('stat_mo', '71'), ('first_name', 'Changed')]:
            with pytest.raises(PermissionError):
                changes.propose(conn, 'rider', 1, field, value, 1)
        with pytest.raises(ValueError):
            changes.propose(conn, 'rider', 1, 'birth_date', '2001-02-30', 1)
        change_id = changes.propose(conn, 'rider', 1, 'birth_date', '2001-02-03', 1)
        with pytest.raises(changes.AttributeConflict):
            changes.propose(conn, 'rider', 1, 'birth_date', '2002-02-03', 1)
        conn.execute(text('UPDATE rider SET birth_date=19990101 WHERE id=1'))
    with pytest.raises(changes.AttributeConflict):
        with engine.begin() as conn:
            changes.decide(conn, 'rider', change_id, 'approve', 2)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM cdb_changes WHERE id=:id'), {'id': change_id}).scalar() == 'pending'
        assert conn.execute(text('SELECT count(*) FROM cdb_change_events')).scalar() == 1
    with engine.begin() as conn:
        changes.decide(conn, 'rider', change_id, 'reject', 2)
        with pytest.raises(changes.AttributeConflict):
            changes.decide(conn, 'rider', change_id, 'approve', 2)
    # Team fields share the same workflow when explicitly enabled.
    monkeypatch.setattr(changes, 'get_editable_fields', lambda entity: {'team_name'} if entity == 'team' else set())
    with engine.begin() as conn:
        team_change = changes.propose(conn, 'team', 1, 'team_name', 'Updated', 1)
        changes.decide(conn, 'team', team_change, 'approve', 2)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT team_name FROM team WHERE id=1')).scalar() == 'Updated'
    with pytest.raises(LookupError):
        changes.object_table('race')
