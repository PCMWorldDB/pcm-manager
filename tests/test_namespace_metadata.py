"""Namespace identity survives changes to its current season and game."""
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.models import AuditLog, Namespace, Role, User
from webapp.web.routes import _build_namespace_slug


def test_namespace_slug_has_no_season_or_pcm_version():
    assert _build_namespace_slug(' World DB! ', 'Men') == 'world-db-men'
    assert _build_namespace_slug('World DB', 'Women') == 'world-db-women'


@pytest.mark.parametrize('role', ['admin', 'viewer', 'stat_editor'])
def test_metadata_update_preserves_identity_and_checks_permissions(app, client, role):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        namespace = Namespace(name=uuid4().hex, slug=uuid4().hex,
                              database_name=uuid4().hex, database_year=2026,
                              pcm_version='PCM2025', sqlite_file_path='cdb/import/original.sqlite')
        db.session.add_all([user, namespace])
        db.session.commit()
        user_id, namespace_id = user.id, namespace.id
        identity = (namespace.slug, namespace.database_name, namespace.sqlite_file_path)
        version = app.config['PCM_VERSIONS'][-1][0]
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        url = f'/admin/namespaces/{namespace_id}/metadata'
        response = client.post(url, data={'database_year': '2027', 'pcm_version': version})
        assert response.status_code == (302 if role == 'admin' else 403)
        with app.app_context():
            namespace = db.session.get(Namespace, namespace_id)
            assert (namespace.slug, namespace.database_name, namespace.sqlite_file_path) == identity
            assert namespace.database_year == (2027 if role == 'admin' else 2026)
            assert namespace.pcm_version == (version if role == 'admin' else 'PCM2025')
        if role == 'admin':
            for data in ({'database_year': 'bad', 'pcm_version': version},
                         {'database_year': '2028', 'pcm_version': 'unknown'}):
                assert client.post(url, data=data).status_code == 400
            with app.app_context():
                assert db.session.get(Namespace, namespace_id).database_year == 2027
    finally:
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(Namespace, namespace_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
