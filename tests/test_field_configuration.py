"""Namespace field settings govern imports, edits, and field-specific RBAC."""
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations.cdb import attribute_changes, cdb_import, get_field_mapping
from webapp.integrations.cdb import field_configuration as config
from webapp.models import AuditLog, Namespace, Role, RolePermission, Resource, Action, User
from webapp.models.ns.schema import NamespaceBase
from webapp.namespace.setup import NamespaceProvisioner
from webapp.rbac_seed import sync_rbac_metadata


@pytest.fixture
def field_namespaces(app):
    engines, urls, namespace_ids = [], [], []
    try:
        with app.app_context():
            for index in range(2):
                url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
                create_database(url)
                urls.append(url)
                engine = create_engine(url)
                engines.append(engine)
                NamespaceProvisioner().create_namespace_schema(url, 'PCM2026')
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO rider(id, first_name, birth_date, stat_mo, stat_fla)
                        VALUES (1, 'Test', 20000101, 70, 70)
                    """))
                    conn.execute(text("INSERT INTO team(id, team_name) VALUES (1, 'Test Team')"))
                ns = Namespace(name=uuid4().hex, slug='field-' + uuid4().hex,
                               database_name=url.database, pcm_version='PCM2026',
                               database_year=2026, status='active', gender='Men')
                db.session.add(ns)
                db.session.flush()
                namespace_ids.append(ns.id)
            db.session.commit()
            namespaces = [dict(id=ns.id, slug=ns.slug, database_name=ns.database_name)
                          for ns in [db.session.get(Namespace, id) for id in namespace_ids]]
        yield namespaces, engines
    finally:
        with app.app_context():
            for id in namespace_ids:
                AuditLog.query.filter_by(resource_type='namespace', resource_id=id).delete()
                namespace = db.session.get(Namespace, id)
                if namespace is not None:
                    db.session.delete(namespace)
            db.session.commit()
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)


def login(app, client, role_name):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role_name).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    with client.session_transaction() as session:
        session['_user_id'] = user_id
        session['_fresh'] = True
    return user_id


def field_form(name='birth_date', **overrides):
    return dict(object='rider', name=name, is_mapped_to_cdb='1',
                cdb_source='gene_i_birthdate', cdb_source_import_policy='preserve',
                cdb_source_import_null_handling='allow', is_editable='1',
                is_require_approval='1', **overrides)


def test_defaults_and_manual_upgrade_are_idempotent(app, field_namespaces):
    _, engines = field_namespaces
    engine = engines[0]
    with engine.begin() as conn:
        rows = config.stored_fields(conn)
        assert rows[('rider', 'birth_date')]['slug'] == 'rider:date_of_birth'
        assert {row['slug'] for (_, name), row in rows.items() if name.startswith('stat_')} == {'rider:stat'}
        assert rows[('rider', 'stat_mo')]['is_editable']
        assert rows[('rider', 'pcs_identity_verified_override')]['cdb_source'] is None
        conn.execute(text("UPDATE field_level_configuration SET is_editable=FALSE WHERE object='rider' AND name='stat_mo'"))
        config.seed_fields(conn, 'PCM2026')
        assert not config.stored_fields(conn)[('rider', 'stat_mo')]['is_editable']
        conn.execute(text('DROP TABLE field_level_configuration'))
        conn.execute(text('ALTER TABLE stat_changes DROP COLUMN is_applied_immediately'))
        migration = (Path(__file__).parents[1] / 'migrations/field_level_configuration_namespace.sql').read_text()
        # Exercise the operator script against an isolated namespace, including a second execution.
        conn.exec_driver_sql(migration)
    with engine.begin() as conn:
        conn.exec_driver_sql(migration)
        config.seed_fields(conn, 'PCM2026')
        assert config.stored_fields(conn)[('rider', 'birth_date')]['is_require_approval']


def test_namespace_ui_updates_audit_and_direct_permissions(app, client, field_namespaces):
    namespaces, engines = field_namespaces
    ns = namespaces[0]
    url = f"/admin/namespaces/{ns['id']}/fields"
    assert client.post(url, data=field_form()).status_code == 302
    login(app, client, 'release_editor')
    assert client.get(url).status_code == 200
    assert client.post(url, data=field_form()).status_code == 403
    login(app, client, 'admin')
    page = client.get(url)
    assert page.status_code == 200
    assert b'rider:date_of_birth' in page.data
    data = field_form()
    data.pop('is_require_approval')
    assert client.post(url, data=data).status_code == 302
    with engines[0].connect() as conn:
        assert not config.stored_fields(conn)[('rider', 'birth_date')]['is_require_approval']
    with engines[1].connect() as conn:
        assert config.stored_fields(conn)[('rider', 'birth_date')]['is_require_approval']
    with app.app_context():
        audit = AuditLog.query.filter_by(action='field_configuration_updated', resource_id=ns['id']).one()
        assert audit.details['before'] == {'is_require_approval': True}
        assert audit.details['after'] == {'is_require_approval': False}
    assert client.post(url, data=field_form(name='id')).status_code == 400
    assert client.post(url, data=field_form(name='pcs_identity_status')).status_code == 404
    invalid = field_form()
    invalid['cdb_source_import_policy'] = 'identity'
    assert client.post(url, data=invalid).status_code == 302
    with app.app_context():
        assert AuditLog.query.filter_by(action='field_configuration_updated', resource_id=ns['id']).count() == 1


def test_attribute_permissions_immediate_application_and_disabled_field(app, client, field_namespaces):
    namespaces, engines = field_namespaces
    login(app, client, 'admin')
    with engines[0].begin() as conn:
        conn.execute(text("""UPDATE field_level_configuration SET is_require_approval=FALSE
                             WHERE object='rider' AND name='birth_date'"""))
    for ns in namespaces:
        assert client.post(f"/n/{ns['slug']}/attributes/rider/1/propose",
                           data={'attribute_name': 'birth_date', 'value': '2001-02-03'}).status_code == 302
    with engines[0].connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20010203
        assert conn.scalar(text('SELECT approved_by_id FROM cdb_changes')) is None
        assert conn.scalar(text("SELECT COUNT(*) FROM cdb_change_events WHERE event_type='applied'")) == 1
    with engines[1].begin() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20000101
        conn.execute(text("UPDATE field_level_configuration SET is_editable=FALSE WHERE name='birth_date'"))
    ns = namespaces[1]
    assert client.post(f"/n/{ns['slug']}/attributes/rider/review/1/approve").status_code == 403
    login(app, client, 'stat_editor')
    assert client.post(f"/n/{ns['slug']}/attributes/rider/1/propose",
                       data={'attribute_name': 'birth_date', 'value': '2002-01-01'}).status_code == 403


def test_individual_and_mixed_list_stats_apply_only_configured_fields(app, client, field_namespaces):
    namespaces, engines = field_namespaces
    ns, engine = namespaces[0], engines[0]
    login(app, client, 'stat_editor')
    with engine.begin() as conn:
        conn.execute(text("UPDATE field_level_configuration SET is_require_approval=FALSE WHERE object='rider' AND name='stat_mo'"))
    payload = {'changes_json': json.dumps([
        {'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': 72},
        {'first_cycling_rider_id': 1, 'stat_name': 'fla', 'final_value': 73},
    ])}
    base = f"/n/{ns['slug']}"
    assert client.post(base + '/riders/1/stats/save', data=payload).status_code == 302
    with engine.begin() as conn:
        assert tuple(conn.execute(text('SELECT stat_mo,stat_fla FROM rider WHERE id=1')).one()) == (72, 70)
        assert list(conn.execute(text('SELECT stat_name,is_approved,is_applied_immediately FROM stat_changes ORDER BY id'))) == [
            ('mo', True, True), ('fla', False, False)]
        conn.execute(text("INSERT INTO rider_list(id,list_name,season_year,gender) VALUES(1,'Mixed',2026,'Men')"))
        conn.execute(text('INSERT INTO rider_list_item(rider_list_id,rider_id) VALUES(1,1)'))
    payload['changes_json'] = json.dumps([
        {'first_cycling_rider_id': 1, 'stat_name': 'mo', 'final_value': 74},
        {'first_cycling_rider_id': 1, 'stat_name': 'fla', 'final_value': 75},
    ])
    assert client.post(base + '/stats-editor/lists/1/save-stat-deltas', data=payload).status_code == 302
    with engine.connect() as conn:
        assert tuple(conn.execute(text('SELECT stat_mo,stat_fla FROM rider WHERE id=1')).one()) == (74, 70)
    assert client.post(base + '/stats-editor/lists/1/submit-for-approval').status_code == 302
    login(app, client, 'stat_editor_approver')
    assert client.post(base + '/stats-editor/lists/1/approve-stat-deltas').status_code == 302
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT stat_fla FROM rider WHERE id=1')) == 75
        assert conn.scalar(text('SELECT BOOL_AND(is_approved) FROM stat_changes WHERE stat_list_id=1')) is True
    with engines[1].connect() as conn:
        assert conn.scalar(text('SELECT stat_mo FROM rider WHERE id=1')) == 70


def make_cdb(path):
    with sqlite3.connect(path) as source:
        for kind in config.OBJECTS:
            mapping = get_field_mapping('PCM2026', kind)
            columns = dict.fromkeys(mapping['fields'].values())
            source.execute('CREATE TABLE "' + mapping['cdb_table_name'] + '" (' +
                           ', '.join('"' + name + '" TEXT' for name in columns) + ')')
            source.execute('INSERT INTO "' + mapping['cdb_table_name'] + '" ("' +
                           mapping['fields']['id'] + '") VALUES (1)')
        source.execute("UPDATE DYN_cyclist SET gene_sz_firstname='Incoming',gene_i_birthdate='20020203'")


def test_import_configuration_and_stale_review_rollback(app, field_namespaces, tmp_path):
    _, engines = field_namespaces
    engine = engines[0]
    path = tmp_path / 'mapping.sqlite'
    make_cdb(path)
    url = engine.url.render_as_string(hide_password=False)
    with cdb_import.CDBImporter(path, url, 'PCM2026', expected_database=engine.url.database) as importer:
        review = importer.analyze()
        with engine.begin() as conn:
            conn.execute(text("""UPDATE field_level_configuration SET cdb_source_import_policy='overwrite'
                                 WHERE object='rider' AND name='birth_date'"""))
        with pytest.raises(ValueError, match='stale'):
            importer.import_all_tables(expected_fingerprint=review['fingerprint'])
        importer.import_all_tables()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20020203
        assert conn.scalar(text('SELECT first_name FROM rider WHERE id=1')) == 'Test'
    with engine.begin() as conn:
        conn.execute(text("""UPDATE field_level_configuration SET cdb_source_import_policy='reject'
                             WHERE object='rider' AND name='birth_date'"""))
    with sqlite3.connect(path) as source:
        source.execute("UPDATE DYN_cyclist SET gene_i_birthdate='20030304'")
    with cdb_import.CDBImporter(path, url, 'PCM2026') as importer:
        with pytest.raises(ValueError, match='birth_date changed'):
            importer.import_all_tables()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20020203


@pytest.mark.parametrize('nulls,expected', [('ignore', 20000101), ('allow', None), ('reject', 20000101)])
def test_import_source_null_policy_and_rollback(app, field_namespaces, tmp_path, nulls, expected):
    _, engines = field_namespaces
    engine = engines[0]
    path = tmp_path / 'source.sqlite'
    make_cdb(path)
    with sqlite3.connect(path) as source:
        source.execute('ALTER TABLE DYN_cyclist ADD COLUMN custom_birth_date TEXT')
    with engine.begin() as conn:
        conn.execute(text('''UPDATE field_level_configuration
                             SET cdb_source='custom_birth_date', cdb_source_import_policy='overwrite',
                                 cdb_source_import_null_handling=:nulls
                             WHERE object='rider' AND name='birth_date' '''), {'nulls': nulls})
        conn.execute(text("""UPDATE field_level_configuration SET cdb_source_import_policy='overwrite'
                             WHERE object='rider' AND name='first_name'"""))
    url = engine.url.render_as_string(hide_password=False)
    with cdb_import.CDBImporter(path, url, 'PCM2026') as importer:
        if nulls == 'reject':
            with pytest.raises(ValueError, match='birth_date cannot be NULL'):
                importer.import_all_tables()
        else:
            importer.import_all_tables()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == expected
        assert conn.scalar(text('SELECT first_name FROM rider WHERE id=1')) == ('Test' if nulls == 'reject' else 'Incoming')


def test_unmapped_field_and_enabled_attribute_edit(app, client, field_namespaces, tmp_path):
    namespaces, engines = field_namespaces
    engine = engines[0]
    path = tmp_path / 'unmapped.sqlite'
    make_cdb(path)
    with engine.begin() as conn:
        conn.execute(text("""UPDATE field_level_configuration
                             SET is_mapped_to_cdb=FALSE, cdb_source=NULL,
                                 cdb_source_import_policy=NULL, cdb_source_import_null_handling=NULL,
                                 is_editable=TRUE, is_require_approval=FALSE
                             WHERE object='rider' AND name='birth_date'"""))
        conn.execute(text("""UPDATE field_level_configuration SET is_editable=TRUE
                             WHERE object='team' AND name='team_name'"""))
    with cdb_import.CDBImporter(path, engine.url.render_as_string(hide_password=False), 'PCM2026') as importer:
        importer.import_all_tables()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20000101
    login(app, client, 'admin')
    ns = namespaces[0]
    base = f"/n/{ns['slug']}"
    assert client.post(base + '/attributes/team/1/propose', data={
        'attribute_name': 'team_name', 'value': 'New Team',
    }).status_code == 302
    assert client.post(base + '/attributes/team/review/1/approve').status_code == 302
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT team_name FROM team WHERE id=1')) == 'New Team'
    # A connection to a different namespace with overlapping field and rider IDs is rejected.
    from webapp.web import routes
    from types import SimpleNamespace
    with engines[1].connect() as conn:
        with pytest.raises(RuntimeError, match='expected namespace database'):
            routes._ensure_namespace_database_connection(conn, SimpleNamespace(database_name=engine.url.database))


def test_initial_review_matches_seeded_namespace(app, tmp_path):
    url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(database='worlddb_test_' + uuid4().hex)
    create_database(url)
    try:
        NamespaceProvisioner().create_namespace_schema(url, 'PCM2026')
        path = tmp_path / 'initial.sqlite'
        make_cdb(path)
        review = cdb_import.analyze_initial(path, 'PCM2026')
        with cdb_import.CDBImporter(path, url.render_as_string(hide_password=False), 'PCM2026') as importer:
            importer.import_all_tables(expected_fingerprint=review['fingerprint'])
    finally:
        drop_database(url)


def test_field_grants_survive_rbac_sync_and_have_required_slug(app, client, field_namespaces):
    login(app, client, 'admin')
    with app.app_context():
        role = Role.query.filter_by(name='admin').one()
        resource = Resource.query.filter_by(name='rider:date_of_birth').one()
        action = Action.query.filter_by(name='update').one()
        permission = RolePermission.query.filter_by(role_id=role.id, resource_id=resource.id, action_id=action.id).one()
        ids = role.id, resource.id, action.id
        db.session.delete(permission)
        db.session.commit()
        sync_rbac_metadata()
        assert RolePermission.query.filter_by(role_id=ids[0], resource_id=ids[1], action_id=ids[2]).first() is None
    try:
        with app.test_request_context():
            from flask_login import login_user
            user = User.query.order_by(User.id.desc()).first()
            login_user(user)
            assert not user.has_permissions('rider:date_of_birth:update')
            assert not user.has_permission('rider:date_of_birth', 'update')
        ns = field_namespaces[0][0]
        assert client.post(f"/n/{ns['slug']}/attributes/rider/1/propose", data={
            'attribute_name': 'birth_date', 'value': '2001-02-03',
        }).status_code == 403
    finally:
        with app.app_context():
            db.session.add(RolePermission(role_id=ids[0], resource_id=ids[1], action_id=ids[2]))
            db.session.commit()


def test_committed_field_audit_retries_after_main_failure(app, client, field_namespaces, monkeypatch):
    namespaces, engines = field_namespaces
    ns = namespaces[0]
    login(app, client, 'admin')
    url = f"/admin/namespaces/{ns['id']}/fields"
    data = field_form()
    data.pop('is_require_approval')
    original_add = db.session.add

    def fail_main_audit(instance, *args, **kwargs):
        if isinstance(instance, AuditLog):
            raise RuntimeError('Main audit unavailable')
        return original_add(instance, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'add', fail_main_audit)
        assert client.post(url, data=data).status_code == 302
    with engines[0].connect() as conn:
        assert not config.stored_fields(conn)[('rider', 'birth_date')]['is_require_approval']
        assert conn.scalar(text('SELECT COUNT(*) FROM field_configuration_audit WHERE NOT forwarded')) == 1
    with app.app_context():
        assert AuditLog.query.filter_by(action='field_configuration_updated', resource_id=ns['id']).count() == 0
    # Visiting the main Audit Log retries delivery, and retries never duplicate events.
    assert client.get('/admin/audit-log').status_code == 200
    assert client.get(url).status_code == 200
    with app.app_context():
        assert AuditLog.query.filter_by(action='field_configuration_updated', resource_id=ns['id']).count() == 1
    with engines[0].connect() as conn:
        assert conn.scalar(text('SELECT BOOL_AND(forwarded) FROM field_configuration_audit')) is True


def test_role_field_permission_ui_and_stat_role_separation(app, client):
    login(app, client, 'admin')
    with app.app_context():
        role_id = Role.query.filter_by(name='viewer').one().id
    data = {'slug': 'rider:date_of_birth', 'action': 'update', 'decision': 'grant'}
    url = f'/admin/roles/{role_id}/field-permissions'
    assert client.get(f'/admin/roles/{role_id}').status_code == 200
    try:
        assert client.post(url, data=data).status_code == 302
        with app.app_context():
            sync_rbac_metadata()
        user_id = login(app, client, 'viewer')
        with app.test_request_context():
            from flask_login import login_user
            user = db.session.get(User, user_id)
            login_user(user)
            assert user.has_permissions('rider:date_of_birth:update')
            assert not user.has_permissions('rider:date_of_birth:approve')
        assert client.post(url, data={**data, 'decision': 'revoke'}).status_code == 403
    finally:
        login(app, client, 'admin')
        assert client.post(url, data={**data, 'decision': 'revoke'}).status_code == 302


@pytest.mark.parametrize('role_name,can_stats,can_approve', [
    ('database_editor', False, False), ('database_maintainer', True, True),
])
def test_database_roles_field_grants_and_routes(app, client, field_namespaces, role_name, can_stats, can_approve):
    ns = field_namespaces[0][0]
    user_id = login(app, client, role_name)
    with app.app_context():
        user = db.session.get(User, user_id)
        for slug in config.field_slugs():
            assert user.has_permission(slug, 'update') == (slug != 'rider:stat' or can_stats)
            assert user.has_permission(slug, 'approve') == can_approve
    base = f"/n/{ns['slug']}"
    assert client.get(base + '/riders/1').status_code == 200
    assert client.post(base + '/attributes/rider/1/propose', data={
        'attribute_name': 'birth_date', 'value': '2001-02-03',
    }).status_code == 302
    assert client.post(base + '/attributes/rider/review/1/approve').status_code == (302 if can_approve else 403)
    assert client.post(base + '/riders/1/stats/save', data={
        'changes_json': '[{"first_cycling_rider_id":1,"stat_name":"mo","final_value":72}]',
    }).status_code == (302 if can_stats else 403)
    assert client.post(base + '/riders/1/stats/approve').status_code == (302 if can_approve else 403)
    with field_namespaces[1][0].connect() as conn:
        assert conn.scalar(text('SELECT stat_mo FROM rider WHERE id=1')) == (72 if can_stats else 70)


def test_database_editor_attribute_lists_use_field_permissions(app, client, field_namespaces):
    ns, engines = field_namespaces[0][0], field_namespaces[1]
    login(app, client, 'database_editor')
    base = f"/n/{ns['slug']}"
    assert client.get(base + '/riders/bulk-editor/new?list_type=attribute').status_code == 200
    assert client.post(base + '/stats-editor/lists/create', data={
        'list_name': 'Fields', 'list_type': 'attribute', 'rider_id': '1',
    }).status_code == 302
    assert client.post(base + '/stats-editor/lists/1/rename', data={'list_name': 'Changed'}).status_code == 302
    assert client.post(base + '/attributes/rider/1/propose', data={
        'list_id': '1', 'attribute_name': 'birth_date', 'value': '2001-02-03',
    }).status_code == 302
    assert client.get(base + '/stats-editor/lists/1').status_code == 200
    assert client.post(base + '/stats-editor/lists/create', data={
        'list_name': 'Stats', 'list_type': 'stat', 'rider_id': '1',
    }).status_code == 403
    with engines[0].begin() as conn:
        conn.execute(text("INSERT INTO rider_list(id,list_name,list_type,season_year,gender) VALUES(99,'Stats','stat',2026,'Men')"))
    assert client.post(base + '/stats-editor/lists/99/rename', data={'list_name': 'Denied'}).status_code == 403
    assert client.post(base + '/stats-editor/lists/99/add-rider', data={'rider_id': '1'}).status_code == 403
    assert client.post(base + '/stats-editor/lists/99/toggle-rider', data={'rider_id': '1'}).status_code == 403


def test_logged_in_user_observes_field_policy_and_grant_changes_next_request(app, client, field_namespaces):
    ns, engines = field_namespaces[0][0], field_namespaces[1]
    login(app, client, 'database_editor')
    admin_client = app.test_client()
    login(app, admin_client, 'admin')
    base = f"/n/{ns['slug']}"
    propose_url = base + '/attributes/rider/1/propose'
    payload = {'attribute_name': 'birth_date', 'value': '2001-02-03'}
    assert client.get(base + '/riders/1').status_code == 200
    with app.app_context():
        role_id = Role.query.filter_by(name='database_editor').one().id
    grant_url = f'/admin/roles/{role_id}/field-permissions'
    data = {'slug': 'rider:date_of_birth', 'action': 'update', 'decision': 'revoke'}
    try:
        assert admin_client.post(grant_url, data=data).status_code == 302
        assert client.post(propose_url, data=payload).status_code == 403
    finally:
        assert admin_client.post(grant_url, data={**data, 'decision': 'grant'}).status_code == 302
    settings_url = f"/admin/namespaces/{ns['id']}/fields"
    settings = field_form()
    settings.pop('is_editable')
    assert admin_client.post(settings_url, data=settings).status_code == 302
    assert client.post(propose_url, data=payload).status_code == 403
    settings['is_editable'] = '1'
    assert admin_client.post(settings_url, data=settings).status_code == 302
    assert client.post(propose_url, data=payload).status_code == 302
    with engines[0].connect() as conn:
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20000101
