"""Published namespace clones preserve IDs without copying pending work."""
from datetime import datetime
import hashlib
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy_utils import database_exists

from webapp.config import get_namespace_path
from webapp.extensions import db
from webapp.integrations.cdb import get_field_mapping
from webapp.models import AuditLog, Namespace, Role, User
from webapp.models.ns.cdb import CDBChange, CDBChangeEvent
from webapp.models.ns.base import Release, ReleaseFile, StatChange, StatChangeSnapshot
from webapp.namespace.clone import CATEGORIES, clone_namespace, load_clone_plan
from webapp.namespace.setup import CDBImporter, NamespaceProvisioner


def _release_sqlite(path):
    with sqlite3.connect(path) as conn:
        for kind in ('country', 'division', 'region', 'team', 'race_class', 'race', 'stage', 'rider'):
            mapping = get_field_mapping('PCM2026', kind)
            columns = list(dict.fromkeys(mapping['fields'].values()))
            definitions = ', '.join(f'"{column}" TEXT' for column in columns)
            conn.execute(f'CREATE TABLE "{mapping["cdb_table_name"]}" ({definitions})')
            values = {name: None for name in columns}
            values[mapping['fields']['id']] = 41
            if kind == 'rider':
                values.update({source: 70 for field, source in mapping['fields'].items()
                               if field.startswith('stat_')})
                values[mapping['fields']['first_name']] = 'Released'
                values[mapping['fields']['last_name']] = 'Rider'
            quoted = ', '.join(f'"{name}"' for name in values)
            conn.execute(f'INSERT INTO "{mapping["cdb_table_name"]}" ({quoted}) '
                         f'VALUES ({", ".join("?" for _ in values)})', list(values.values()))
        conn.execute('CREATE TABLE untouched (payload BLOB)')
        conn.execute("INSERT INTO untouched VALUES (X'0001FF')")


@pytest.fixture
def clone_source(app, monkeypatch):
    with app.app_context():
        provisioner = NamespaceProvisioner()
        source = Namespace(name=uuid4().hex, slug=uuid4().hex, database_name=f'worlddb_test_{uuid4().hex}',
                           pcm_version='PCM2026', status='active', gender='Men')
        db.session.add(source)
        db.session.commit()
        provisioner.provision_namespace(source.database_name, source.pcm_version)
        root = Path(get_namespace_path(source.slug))
        (root / 'releases').mkdir(parents=True)
        artifact = root / 'releases' / 'published.sqlite'
        _release_sqlite(artifact)
        source_engine = create_engine(source.build_connection_string())
        destinations = []
        with CDBImporter(str(artifact), source.build_connection_string(), source.pcm_version) as importer:
            importer.import_all_tables()
        with source_engine.begin() as conn:
            conn.execute(text("UPDATE rider SET first_name='Unreleased', stat_mo=99 WHERE id=41"))
            conn.execute(Release.__table__.insert(), [
                dict(id=12, version='V12', status='published', published_at=datetime(2026, 1, 1)),
                dict(id=22, version='V22', status='published', published_at=datetime(2026, 2, 1)),
                dict(id=99, version='V99', status='draft', published_at=None),
            ])
            conn.execute(text("INSERT INTO rider_list (id, list_name, list_type, season_year, gender) "
                              "VALUES (51, 'Stats', 'stat', 2026, 'Men'), (52, 'Attributes', 'attribute', 2026, 'Men')"))
            conn.execute(text("INSERT INTO rider_list_item (id, rider_list_id, rider_id) VALUES (61,51,41),(62,52,41)"))
            conn.execute(StatChange.__table__.insert(), [
                dict(id=71, rider_id=41, stat_name='mo', original_value=68, final_value=70,
                     delta_value=2, is_approved=True, release_id=12, stat_list_id=51),
                dict(id=72, rider_id=41, stat_name='mm', original_value=70, final_value=99,
                     delta_value=29, is_approved=False, release_id=None, stat_list_id=None),
                dict(id=73, rider_id=41, stat_name='tt', original_value=70, final_value=99,
                     delta_value=29, is_approved=True, release_id=None, stat_list_id=None),
            ])
            snapshot = {column.name: 68 for column in StatChangeSnapshot.__table__.columns
                        if column.name.startswith('stat_')}
            snapshot.update(id=81, rider_id=41, stat_list_id=51, discord_thread_id='source-thread')
            conn.execute(StatChangeSnapshot.__table__.insert().values(**snapshot))
            for index, (kind, attr) in enumerate([
                ('rider', 'stat_mo'), ('rider', 'first_name'), ('team', 'team_name'),
                ('race', 'race_name'), ('stage', 'day'), ('race_class', 'max_teams'),
            ], 101):
                conn.execute(CDBChange.__table__.insert().values(
                    id=index, entity_type=kind, entity_id=41, attribute_name=attr, operation='update',
                    source='test', status='approved', before_value='old', after_value='new',
                    created_at=datetime(2026, 1, 2), applied_at=datetime(2026, 1, 3),
                    stat_list_id=52 if index == 102 else None,
                ))
                conn.execute(CDBChangeEvent.__table__.insert().values(
                    id=index + 100, change_id=index, event_type='approved', occurred_at=datetime(2026, 1, 3)))
            for index, status, applied in [(107, 'pending', None), (108, 'approved', datetime(2026, 3, 1))]:
                conn.execute(CDBChange.__table__.insert().values(
                    id=index, entity_type='rider', entity_id=41, attribute_name='first_name', operation='update',
                    source='test', status=status, created_at=datetime(2026, 1, 2), applied_at=applied))
            conn.execute(CDBChangeEvent.__table__.insert().values(
                id=301, change_id=101, event_type='released', release_id=12, occurred_at=datetime(2026, 1, 4)))
            conn.execute(CDBChange.__table__.insert().values(
                id=109, entity_type='team', entity_id=41, attribute_name='team_name', operation='update',
                source='test', status='rejected', created_at=datetime(2026, 1, 2)))
            conn.execute(CDBChangeEvent.__table__.insert().values(
                id=302, change_id=109, event_type='rejected', occurred_at=datetime(2026, 1, 3)))
            for index, (kind, name) in enumerate([
                ('cdb_sqlite', 'published.sqlite'), ('rider_stats_sql', 'riders.sql'),
                ('release_overview_html', 'overview.html'),
            ], 401):
                path = root / 'releases' / name
                if kind != 'cdb_sqlite':
                    path.write_text(f'original {kind}')
                conn.execute(ReleaseFile.__table__.insert().values(
                    id=index, release_id=22, file_type=kind, display_name=name,
                    relative_path=f'releases/{name}', sha256_checksum=hashlib.sha256(path.read_bytes()).hexdigest()))

        def destination():
            item = Namespace(name=uuid4().hex, slug=uuid4().hex,
                             database_name=f'worlddb_test_{uuid4().hex}', pcm_version='PCM2026', gender='Men')
            destinations.append(item)
            return item

        try:
            yield source, source_engine, destination
        finally:
            source_engine.dispose()
            for item in destinations + [source]:
                if database_exists(item.build_connection_string()):
                    provisioner.drop_namespace_database(item.database_name)
                shutil.rmtree(get_namespace_path(item.slug), ignore_errors=True)
                if item.id is not None:
                    AuditLog.query.filter_by(resource_type='namespace', resource_id=item.id).delete()
                    db.session.delete(item)
            db.session.commit()


@pytest.mark.parametrize('categories,expected', [
    (CATEGORIES, [101, 102, 103, 104, 105, 106, 109]),
    ([], []), (['rider_stats'], [101]), (['rider_attributes'], [102]),
    (['teams'], [103, 109]), (['races'], [104, 105, 106]),
])
def test_clone_released_data_history_dependencies_and_sequences(clone_source, categories, expected):
    source, source_engine, new = clone_source
    plan = load_clone_plan(source, categories)
    destination = new()
    with clone_namespace(source, destination, categories, plan['fingerprint']):
        assert destination.id is None  # no registration until the caller commits
    engine = create_engine(destination.build_connection_string())
    try:
        with engine.begin() as conn:
            assert conn.execute(text('SELECT id, first_name, stat_mo FROM rider')).one() == (41, 'Released', 70)
            assert conn.execute(text('SELECT id FROM cdb_changes ORDER BY id')).scalars().all() == expected
            assert conn.execute(text("SELECT COUNT(*) FROM cdb_changes WHERE status='pending'")).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM stat_changes WHERE NOT is_approved OR release_id IS NULL')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM rider WHERE has_pending_stat_changes OR has_pending_attribute_changes')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM cdb_change_events e LEFT JOIN cdb_changes c ON c.id=e.change_id WHERE c.id IS NULL')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM cdb_change_events e LEFT JOIN releases r ON r.id=e.release_id WHERE e.release_id IS NOT NULL AND r.id IS NULL')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM stat_changes')).scalar_one() == int('rider_stats' in categories)
            if 'rider_stats' in categories:
                assert conn.execute(text('SELECT id, stat_list_id, release_id FROM stat_changes')).one() == (71, 51, 12)
                assert conn.execute(text('SELECT discord_thread_id FROM stat_change_snapshot')).scalar_one() is None
            assert conn.execute(text("INSERT INTO rider (first_name) VALUES ('New') RETURNING id")).scalar_one() == 42
            assert conn.execute(Release.__table__.insert().values(version='new').returning(Release.id)).scalar_one() == 23
            files = conn.execute(text('SELECT relative_path, sha256_checksum FROM release_files')).all()
            assert len(files) == 3
            for path, checksum in files:
                assert hashlib.sha256((Path(get_namespace_path(destination.slug)) / path).read_bytes()).hexdigest() == checksum
            for table in ('stat_changes', 'stat_change_snapshot', 'cdb_changes', 'cdb_change_events',
                          'rider_list', 'rider_list_item', 'release_files'):
                highest = conn.execute(text(f'SELECT MAX(id) FROM {table}')).scalar_one()
                assert conn.execute(text("SELECT nextval(pg_get_serial_sequence(:table, 'id'))"),
                                    {'table': table}).scalar_one() == (highest + 1 if highest else 1)
        with sqlite3.connect(Path(get_namespace_path(destination.slug)) / destination.sqlite_file_path) as conn:
            assert conn.execute('SELECT payload FROM untouched').fetchone()[0] == b'\x00\x01\xff'
        with source_engine.connect() as conn:
            assert conn.execute(text('SELECT first_name, stat_mo FROM rider')).one() == ('Unreleased', 99)
    finally:
        engine.dispose()


def test_stale_preview_and_existing_database_leave_source_untouched(clone_source):
    source, engine, new = clone_source
    plan = load_clone_plan(source, CATEGORIES)
    destination = new()
    with engine.begin() as conn:
        conn.execute(text("UPDATE releases SET release_notes='changed' WHERE id=22"))
    with pytest.raises(ValueError, match='preview changed'):
        with clone_namespace(source, destination, CATEGORIES, plan['fingerprint']):
            pytest.fail('stale clone ran')
    assert not Path(get_namespace_path(destination.slug)).exists()
    plan = load_clone_plan(source, CATEGORIES)
    NamespaceProvisioner().provision_namespace(destination.database_name, 'PCM2026')
    with pytest.raises(ValueError, match='already exists'):
        with clone_namespace(source, destination, CATEGORIES, plan['fingerprint']):
            pytest.fail('existing database used')
    assert database_exists(destination.build_connection_string())
    assert not Path(get_namespace_path(destination.slug)).exists()


def test_clone_cleans_up_on_registration_failure(clone_source):
    source, _, new = clone_source
    destination = new()
    plan = load_clone_plan(source, CATEGORIES)
    with pytest.raises(RuntimeError, match='registration'):
        with clone_namespace(source, destination, CATEGORIES, plan['fingerprint']):
            raise RuntimeError('registration failed')
    assert not database_exists(destination.build_connection_string())
    assert not Path(get_namespace_path(destination.slug)).exists()


def test_clone_cleans_up_invalid_release_import(clone_source):
    source, source_engine, new = clone_source
    artifact = Path(get_namespace_path(source.slug)) / 'releases' / 'published.sqlite'
    with sqlite3.connect(artifact) as conn:
        conn.execute('DROP TABLE DYN_cyclist')
    with source_engine.begin() as conn:
        conn.execute(text("UPDATE release_files SET sha256_checksum=NULL WHERE file_type='cdb_sqlite'"))
    destination = new()
    plan = load_clone_plan(source, CATEGORIES)
    with pytest.raises(sqlite3.OperationalError):
        with clone_namespace(source, destination, CATEGORIES, plan['fingerprint']):
            pytest.fail('invalid import ran')
    assert not database_exists(destination.build_connection_string())
    assert not Path(get_namespace_path(destination.slug)).exists()


def test_clone_preserves_existing_storage_directory(clone_source):
    source, _, new = clone_source
    destination = new()
    root = Path(get_namespace_path(destination.slug))
    root.mkdir(parents=True)
    (root / 'keep.txt').write_text('existing')
    plan = load_clone_plan(source, [])
    with pytest.raises(FileExistsError):
        with clone_namespace(source, destination, [], plan['fingerprint']):
            pytest.fail('existing storage reused')
    assert (root / 'keep.txt').read_text() == 'existing'
    assert not database_exists(destination.build_connection_string())


def test_clone_rejects_wrong_database_and_missing_artifacts(clone_source, monkeypatch):
    source, _, new = clone_source
    other = new()
    NamespaceProvisioner().provision_namespace(other.database_name, 'PCM2026')
    with monkeypatch.context() as patch:
        patch.setattr(source, 'build_connection_string', other.build_connection_string)
        with pytest.raises(ValueError, match='isolated namespace'):
            load_clone_plan(source, CATEGORIES)
    (Path(get_namespace_path(source.slug)) / 'releases' / 'riders.sql').unlink()
    with pytest.raises(ValueError, match='missing'):
        load_clone_plan(source, CATEGORIES)


@pytest.mark.parametrize('problem', ['unpublished', 'checksum', 'path'])
def test_clone_rejects_invalid_source_release(clone_source, problem):
    source, engine, _ = clone_source
    with engine.begin() as conn:
        if problem == 'unpublished':
            conn.execute(text("UPDATE releases SET status='draft'"))
        elif problem == 'checksum':
            conn.execute(text("UPDATE release_files SET sha256_checksum='invalid' WHERE id=401"))
        else:
            conn.execute(text("UPDATE release_files SET relative_path='../outside.sqlite' WHERE id=401"))
    with pytest.raises(ValueError):
        load_clone_plan(source, CATEGORIES)


def test_packed_release_conversion_and_independent_next_release(clone_source, monkeypatch):
    from types import SimpleNamespace
    from webapp.web import routes

    source, engine, new = clone_source
    root = Path(get_namespace_path(source.slug))
    original = root / 'releases' / 'published.sqlite'
    packed = root / 'releases' / 'published.cdb'
    original.rename(packed)
    with engine.begin() as conn:
        conn.execute(text("UPDATE release_files SET file_type='cdb', relative_path='releases/published.cdb' WHERE id=401"))
    conversions = []

    def unpack(source_path, target_path):
        conversions.append(source_path)
        shutil.copy2(source_path, target_path)

    monkeypatch.setattr('webapp.namespace.clone.convert_cdb_to_sqlite', unpack)
    destination = new()
    plan = load_clone_plan(source, CATEGORIES)
    with clone_namespace(source, destination, CATEGORIES, plan['fingerprint']):
        pass
    assert len(conversions) == 1
    assert Path(conversions[0]).is_relative_to(Path(get_namespace_path(destination.slug)))
    packed.unlink()  # The destination must no longer depend on source files.
    monkeypatch.setattr(routes, '_convert_release_sqlite_to_cdb', shutil.copy2)
    result = routes._build_review_sqlite_for_release(
        destination, SimpleNamespace(id=23, version='V23'), 'next-release')
    with sqlite3.connect(Path(get_namespace_path(destination.slug)) / result['relative_path']) as conn:
        assert conn.execute('SELECT payload FROM untouched').fetchone()[0] == b'\x00\x01\xff'
        assert conn.execute('SELECT charac_i_mountain FROM DYN_cyclist').fetchone()[0] == '70'


@pytest.mark.parametrize('role,allowed', [('admin', True), ('release_manager', True),
                                        ('viewer', False), ('stat_editor', False)])
def test_clone_route_permissions_and_confirmation(clone_source, client, role, allowed):
    source, _, new = clone_source
    user = User(username=uuid4().hex, is_active=True)
    user.roles.append(Role.query.filter_by(name=role).one())
    db.session.add(user)
    db.session.commit()
    destination = new()
    data = dict(creation_method='clone', source_namespace=str(source.id), name=destination.name,
                pcm_version='PCM2026', database_year='2026', gender='Men', status='draft',
                copy_rider_stats='y', copy_rider_attributes='y', copy_teams='y', copy_races='y')
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
        url = '/admin/namespaces/create'
        if allowed:
            from bs4 import BeautifulSoup
            form = BeautifulSoup(client.get(url).data, 'html.parser')
            for kind in CATEGORIES:
                assert form.find(id=f'copy_{kind}').has_attr('checked')
            assert form.find(id='clone-required').has_attr('disabled')
            assert form.find(id='clone-required').has_attr('checked')
        response = client.post(url, data=data)
        assert response.status_code == (200 if allowed else 403)
        if allowed:
            assert b'Namespace clone preview' in response.data
            assert Namespace.query.filter_by(name=destination.name).first() is None
            changed = dict(data, database_year='2027', confirm_clone='yes')
            assert client.post(url, data=changed).status_code == 400
            response = client.post(url, data={**data, 'confirm_clone': 'yes'})
            assert response.status_code == 302
            registered = Namespace.query.filter_by(name=destination.name).one()
            # Route generates the destination slug from name and gender.
            destination.slug = registered.slug
            destination.database_name = registered.database_name
            db.session.delete(registered)
            db.session.commit()
        else:
            assert client.post(url, data={**data, 'confirm_clone': 'yes'}).status_code == 403
    finally:
        AuditLog.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()


def test_clone_route_denies_inaccessible_source_and_anonymous_requests(clone_source, client):
    from flask import g

    source, _, _ = clone_source
    url = '/admin/namespaces/create'
    assert client.post(url, data={'creation_method': 'clone'}).status_code == 302
    user = User(username=uuid4().hex, is_active=True)
    user.roles.append(Role.query.filter_by(name='admin').one())
    source.status = 'draft'
    db.session.add(user)
    db.session.commit()
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
        g.pop('_login_user', None)
        assert client.post(url, data={'creation_method': 'clone', 'source_namespace': str(source.id),
                                      'confirm_clone': 'yes'}).status_code == 403
    finally:
        db.session.delete(user)
        db.session.commit()


@pytest.mark.parametrize('permission', [('namespaces', 'read'), ('releases', 'read'), ('releases', 'download')])
def test_clone_requires_source_read_and_download_permissions(clone_source, client, monkeypatch, permission):
    source, _, _ = clone_source
    user = User(username=uuid4().hex, is_active=True)
    user.roles.append(Role.query.filter_by(name='admin').one())
    db.session.add(user)
    db.session.commit()
    original = User.has_permission

    def restricted(self, resource, action):
        return (resource, action) != permission and original(self, resource, action)

    monkeypatch.setattr(User, 'has_permission', restricted)
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
        assert client.post('/admin/namespaces/create', data={
            'creation_method': 'clone', 'source_namespace': str(source.id),
            'confirm_clone': 'yes',
        }).status_code == 403
    finally:
        db.session.delete(user)
        db.session.commit()
