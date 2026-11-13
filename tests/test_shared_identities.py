"""Provider-only maps, namespace-local pointers, and transactional publication."""
import os
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations import identity as service
from webapp.models import AuditLog, Namespace, Role, User
from webapp.models.ns.base import NamespaceBase
from webapp.models.ns.identity import IdentitySyncOutbox
from webapp.web.race_catalog import filter_races, unified_races


@pytest.fixture
def catalog(app):
    engine = create_engine(os.environ['WORLDDB_TEST_DATABASE_URL'])
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                for model in service.MODELS.values():
                    conn.execute(model.__table__.delete())
                yield conn
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_main_contains_only_provider_identity_data(catalog, kind):
    model = service.MODELS[kind]
    assert model.__bind_key__ == 'main'
    assert set(model.__table__.c.keys()) == {'id', 'firstcycling_id', 'pcs_slug', 'created_at', 'updated_at'}
    assert not model.__table__.foreign_keys
    for key in ('wdbo_race_id', 'wdbo_rider_id', 'namespace_id', 'race_id', 'rider_id', 'entity_id', 'name'):
        with pytest.raises(ValueError, match='FC and PCS IDs only'):
            service.identity_values(kind, {'firstcycling_id': 1, key: 42})


@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_pair_creation_is_idempotent_and_merges_compatible_fragments(catalog, kind):
    table = service.MODELS[kind].__table__
    first = service.merge_pair(catalog, kind, 10, 'example')
    assert service.merge_pair(catalog, kind, 10, 'example') == first
    fc_only = catalog.execute(table.insert().values(firstcycling_id=20).returning(table.c.id)).scalar_one()
    catalog.execute(table.insert().values(pcs_slug='another'))
    assert service.merge_pair(catalog, kind, 20, 'another') == fc_only
    assert len(catalog.execute(select(table)).all()) == 2


@pytest.mark.parametrize('kind', ['race', 'rider'])
@pytest.mark.parametrize('pair', [(10, 'different'), (20, 'example'), (10, 'another')])
def test_conflicting_namespace_pairs_never_replace_main(catalog, kind, pair):
    service.merge_pair(catalog, kind, 10, 'example')
    service.merge_pair(catalog, kind, 20, 'another')
    before = catalog.execute(select(service.MODELS[kind].__table__)).all()
    with pytest.raises(ValueError, match='preserved for review'):
        service.merge_pair(catalog, kind, *pair)
    assert catalog.execute(select(service.MODELS[kind].__table__)).all() == before


@pytest.mark.parametrize('values', [
    {}, {'firstcycling_id': -1}, {'firstcycling_id': True}, {'firstcycling_id': 1.5},
    {'pcs_slug': 'race/example/2026'}, {'pcs_slug': '  '},
    {'pcs_slug': 'https://www.procyclingstats.com/race/example'},
])
def test_invalid_shared_identity_is_rejected(values):
    with pytest.raises(ValueError):
        service.identity_values('race', values)


@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_database_constraints_reject_invalid_and_duplicate_sources(catalog, kind):
    table = service.MODELS[kind].__table__
    catalog.execute(table.insert().values(firstcycling_id=1))
    for values in ({'firstcycling_id': 1}, {}, {'firstcycling_id': -1}, {'pcs_slug': 'bad/path'}):
        with pytest.raises(IntegrityError), catalog.begin_nested():
            catalog.execute(table.insert().values(**values))


@pytest.fixture
def provider_namespaces(app):
    main_url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL'])
    token = uuid4().hex
    base_fc = 2000000000 + int(token[:6], 16)
    prefix = 'sync-' + token
    engines, urls, namespaces = [], [], []
    try:
        for index, label in enumerate(('Alpha', 'Beta')):
            url = main_url.set(database=f'worlddb_test_{uuid4().hex}')
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            namespaces.append(Namespace(id=2147000000 + index, slug=label.lower(), name=label,
                                        database_name=url.database, database_year=2026,
                                        gender='Men', pcm_version='PCM2026'))
        yield SimpleNamespace(engines=engines, urls=urls, namespaces=namespaces, fc=base_fc, slug=prefix)
    finally:
        main = create_engine(main_url)
        try:
            with main.begin() as conn:
                for model in service.MODELS.values():
                    table = model.__table__
                    conn.execute(table.delete().where((table.c.firstcycling_id.between(base_fc, base_fc + 999))
                                                      | table.c.pcs_slug.like(prefix + '%')))
        finally:
            main.dispose()
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)


def _publish(data, index=0, **kwargs):
    return service.publish_namespace_pairs(data.urls[index], **kwargs)


def _main_pair(app, kind, fc):
    with app.app_context():
        row = service.MODELS[kind].query.filter_by(firstcycling_id=fc).one_or_none()
        return (row.firstcycling_id, row.pcs_slug) if row else None


@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_namespace_commit_and_rollback_control_publication(app, provider_namespaces, kind):
    data = provider_namespaces
    for index, engine in enumerate(data.engines):
        with engine.begin() as conn:
            conn.execute(text(f'INSERT INTO {kind} (id, first_cycling_id, pcs_slug) VALUES (42, :fc, :slug)'),
                         {'fc': data.fc + index, 'slug': data.slug + f'-{index}'})
    assert _main_pair(app, kind, data.fc) is None
    assert _publish(data)['published'] == 1
    assert _main_pair(app, kind, data.fc) == (data.fc, data.slug + '-0')
    assert _main_pair(app, kind, data.fc + 1) is None
    assert _publish(data, 1)['published'] == 1
    # Overlapping local CDB IDs never relate records in different namespaces.
    with data.engines[1].connect() as conn:
        assert conn.scalar(text(f'SELECT first_cycling_id FROM {kind} WHERE id=42')) == data.fc + 1
    with data.engines[0].connect() as conn:
        tx = conn.begin()
        conn.execute(text(f'UPDATE {kind} SET first_cycling_id=:fc, pcs_slug=:slug WHERE id=42'),
                     {'fc': data.fc + 2, 'slug': data.slug + '-rolled-back'})
        tx.rollback()
    assert _publish(data)['published'] == 0
    assert _main_pair(app, kind, data.fc + 2) is None
    # Replay after main commit / before outbox acknowledgement is harmless.
    with data.engines[0].begin() as conn:
        conn.execute(text("UPDATE identity_sync_outbox SET status='pending'"))
    assert _publish(data)['published'] == 1
    assert _publish(data)['published'] == 0


@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_publication_conflicts_are_local_and_retryable(app, provider_namespaces, kind):
    data = provider_namespaces
    with data.engines[0].begin() as conn:
        conn.execute(text(f'INSERT INTO {kind} (id, first_cycling_id, pcs_slug) VALUES (42, :fc, :slug)'),
                     {'fc': data.fc, 'slug': data.slug + '-old'})
    assert _publish(data)['published'] == 1
    with data.engines[0].begin() as conn:
        conn.execute(text(f'UPDATE {kind} SET pcs_slug=:slug WHERE id=42'), {'slug': data.slug + '-new'})
    assert _publish(data)['conflicts'] == 1
    assert _main_pair(app, kind, data.fc) == (data.fc, data.slug + '-old')
    with data.engines[0].connect() as conn:
        assert conn.scalar(text("SELECT status FROM identity_sync_outbox WHERE pcs_slug=:slug"),
                           {'slug': data.slug + '-new'}) == 'conflict'
    with app.app_context(), db.engines['main'].begin() as conn:
        model = service.MODELS[kind]
        shared_id = conn.scalar(select(model.id).where(model.firstcycling_id == data.fc))
        service.save_identity(conn, kind, {'firstcycling_id': data.fc, 'pcs_slug': data.slug + '-new'},
                              identity_id=shared_id)
    assert _publish(data, retry_conflicts=True)['published'] == 1


def test_main_outage_retains_the_namespace_outbox(app, provider_namespaces, monkeypatch):
    data = provider_namespaces
    with data.engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race (id, first_cycling_id, pcs_slug) VALUES (42, :fc, :slug)'),
                     {'fc': data.fc, 'slug': data.slug})
    original = service.main_engine

    @contextmanager
    def unavailable():
        raise OperationalError('main unavailable', None, None)
        yield
    monkeypatch.setattr(service, 'main_engine', unavailable)
    assert _publish(data)['failed']
    with data.engines[0].connect() as conn:
        assert conn.scalar(text('SELECT status FROM identity_sync_outbox')) == 'pending'
    monkeypatch.setattr(service, 'main_engine', original)
    assert _publish(data)['published'] == 1


def test_assumed_rider_slugs_never_pollute_main(app, provider_namespaces):
    from webapp.integrations.cdb import attribute_changes
    data = provider_namespaces
    with data.engines[0].begin() as conn:
        conn.execute(text('''INSERT INTO rider (id, first_cycling_id, pcs_slug, pcs_slug_is_assumed)
                             VALUES (42, :fc, :slug, TRUE)'''), {'fc': data.fc, 'slug': data.slug})
    assert _publish(data)['published'] == 0
    assert _main_pair(app, 'rider', data.fc) is None
    with data.engines[0].begin() as conn:
        change_id = attribute_changes.propose(conn, 'rider', 42, 'pcs_slug', data.slug, 1)
    assert _publish(data)['published'] == 0
    with data.engines[0].begin() as conn:
        attribute_changes.decide(conn, 'rider', change_id, 'approve', 2)
    assert _publish(data)['published'] == 1


def test_only_applied_approved_identity_changes_contribute(app, provider_namespaces):
    from webapp.integrations.cdb import attribute_changes
    data = provider_namespaces
    with data.engines[0].begin() as conn:
        conn.execute(text('INSERT INTO rider (id, first_cycling_id) VALUES (42, :fc)'), {'fc': data.fc})
        proposal = attribute_changes.propose(conn, 'rider', 42, 'pcs_slug', data.slug, 1)
    assert _publish(data)['published'] == 0
    with data.engines[0].begin() as conn:
        attribute_changes.decide(conn, 'rider', proposal, 'approve', 2)
    assert _publish(data)['published'] == 1
    assert _main_pair(app, 'rider', data.fc) == (data.fc, data.slug)


def _identity(id=1, fc=10, pcs='example'):
    return SimpleNamespace(id=id, firstcycling_id=fc, pcs_slug=pcs)


def _cdb(id=42, fc=10, pcs='example'):
    return dict(id=id, first_cycling_id=fc, pcs_slug=pcs, race_name='CDB Example', number_stages=1)


def _fc(id=100, fc=10):
    return SimpleNamespace(id=id, race_id=fc, name='FC Example', classification='1.1',
                           nation_name='France', start_date=date(2026, 6, 1),
                           end_date=date(2026, 6, 1), stage_count=1, race_stages=[], race_results=[])


def test_union_deduplicates_exact_sources_and_keeps_unmatched_records():
    rows = unified_races([_cdb(), _cdb(43, None, None)], [_fc()],
                         [_identity(), _identity(2, 11, 'minor')])
    assert len(rows) == 3
    merged = next(row for row in rows if row.race_id == 10)
    assert (merged.cdb_id, merged.firstcycling_id, merged.pcs_slug) == (42, 10, 'example')
    assert any(row.cdb_id is None and row.pcs_slug == 'minor' for row in rows)


def test_union_never_matches_local_numeric_ids_or_names():
    rows = unified_races([_cdb(42, None, None)], [_fc()], [_identity()])
    assert len(rows) == 2
    assert next(row for row in rows if row.cdb_id == 42).race_id is None


@pytest.mark.parametrize('cdb', [[_cdb(), _cdb(43)], [_cdb(pcs='another')], [_cdb(fc=20)]])
def test_ambiguous_namespace_mappings_remain_separate(cdb):
    rows = unified_races(cdb, [_fc()], [_identity(), _identity(2, 20, 'another')])
    assert all(row.mapping_conflict for row in rows if row.cdb_id)
    assert next(row for row in rows if row.race_id == 10).cdb_id is None


def test_date_filters_keep_unknown_editions_and_search_cdb_aliases():
    rows = unified_races([_cdb(43, None, None)], [_fc()], [])
    filters = dict(race_name='', nation='', is_stage_race='',
                   start_date_from='2026-07-01', start_date_to='2026-12-31')
    assert filter_races(rows, filters, str.casefold)[0].cdb_id == 43
    rows = unified_races([_cdb()], [_fc()], [_identity()])
    filters.update(race_name='CDB Example', start_date_from='', start_date_to='')
    assert len(filter_races(rows, filters, str.casefold)) == 1


@pytest.mark.parametrize('role,allowed', [('admin', True), ('stat_editor', False), ('database_editor', False)])
@pytest.mark.parametrize('kind', ['race', 'rider'])
def test_shared_map_permissions_and_corrections_have_no_namespace_pointer(app, client, kind, role, allowed):
    slug = 'edit-' + uuid4().hex
    fc = 2100000000 + int(uuid4().hex[:5], 16)
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        conn = db.session.connection(bind_arguments={'mapper': service.MODELS[kind]})
        identity_id = service.merge_pair(conn, kind, fc, slug)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        assert client.get(f'/identities/{kind}').status_code == (200 if allowed else 403)
        settings = client.get('/settings')
        assert settings.status_code == 200
        assert (f'href="/identities/{kind}"'.encode() in settings.data) == allowed
        response = client.post(f'/identities/{kind}', data={
            'identity_id': identity_id, 'firstcycling_id': fc, 'pcs_slug': slug + '-corrected',
        })
        assert response.status_code == (302 if allowed else 403)
        if allowed:
            assert client.post(f'/identities/{kind}', data={'pcs_slug': 'new-entry'}).status_code == 400
        with app.app_context():
            row = db.session.get(service.MODELS[kind], identity_id)
            assert row.pcs_slug == (slug + '-corrected' if allowed else slug)
            if allowed:
                audit = AuditLog.query.filter_by(user_id=user_id, action='identity_updated').one()
                assert set(audit.details['after']) == {'firstcycling_id', 'pcs_slug'}
    finally:
        with app.app_context():
            service.MODELS[kind].query.filter_by(id=identity_id).delete()
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_identity_routes_require_login(client):
    for kind in ('race', 'rider'):
        assert '/login' in client.get(f'/identities/{kind}').location
        assert '/login' in client.post(f'/identities/{kind}/retry').location


def test_intermediate_namespace_pairs_do_not_reach_main(app, provider_namespaces):
    data = provider_namespaces
    with data.engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race (id, first_cycling_id, pcs_slug) VALUES (42, :fc, :slug)'),
                     {'fc': data.fc, 'slug': data.slug + '-intermediate'})
        conn.execute(text('UPDATE race SET pcs_slug=:slug WHERE id=42'), {'slug': data.slug + '-final'})
    result = _publish(data)
    assert result['published'] == 1 and result['superseded'] == 1
    assert _main_pair(app, 'race', data.fc) == (data.fc, data.slug + '-final')


def test_external_only_seed_pairs_are_published_without_cdb_rows(app, provider_namespaces, tmp_path):
    from webapp.models.ns.cdb import Race
    from webapp.seeds.identity import seed_namespace
    data = provider_namespaces
    path = tmp_path / 'race_identity.yaml'
    path.write_text(f'''races:
  - race_id: null
    race_name: External only
    firstcycling_id: {data.fc}
    pcs_slug: {data.slug}
''', encoding='utf-8')
    with data.engines[0].begin() as conn:
        seed_namespace(conn, Race.__table__, path, 'race')
        assert conn.scalar(text('SELECT count(*) FROM race')) == 0
    assert _publish(data)['published'] == 1
    assert _main_pair(app, 'race', data.fc) == (data.fc, data.slug)


def test_invalid_namespace_seed_rolls_back_contributions(app, provider_namespaces, tmp_path):
    from webapp.models.ns.cdb import Race
    from webapp.seeds.identity import seed_namespace
    data = provider_namespaces
    path = tmp_path / 'invalid.yaml'
    path.write_text(f'''races:
  - race_id: null
    firstcycling_id: {data.fc}
    pcs_slug: {data.slug}
  - race_id: null
    firstcycling_id: -1
    pcs_slug: invalid
''', encoding='utf-8')
    with pytest.raises(ValueError, match='firstcycling_id'), data.engines[0].begin() as conn:
        seed_namespace(conn, Race.__table__, path, 'race')
    assert _publish(data)['published'] == 0
    assert _main_pair(app, 'race', data.fc) is None


def test_race_mapping_routes_keep_proposals_local_and_publish_after_approval(app, client, provider_namespaces, monkeypatch):
    from webapp.web import routes
    data = provider_namespaces
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: data.namespaces)
    for engine, label in zip(data.engines, ('Alpha', 'Beta')):
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO race (id, race_name) VALUES (42, :name)'), {'name': label})
    user_ids = []
    with app.app_context():
        for role in ('stat_editor', 'database_editor', 'database_maintainer'):
            user = User(username=uuid4().hex, is_active=True)
            user.roles.append(Role.query.filter_by(name=role).one())
            db.session.add(user)
            db.session.flush()
            user_ids.append(user.id)
        db.session.commit()

    def login(index):
        with client.session_transaction() as session:
            session['_user_id'] = str(user_ids[index])
            session['_fresh'] = True
    try:
        login(0)
        path = '/n/alpha/attributes/race/42/propose'
        assert client.post(path, data={'attribute_name': 'pcs_slug', 'value': data.slug}).status_code == 403
        login(1)
        response = client.get('/n/alpha/races/cdb/42')
        assert response.status_code == 200 and b'attribute_name' in response.data
        assert client.post(path, data={'attribute_name': 'first_cycling_id', 'value': data.fc}).status_code == 302
        assert client.post(path, data={'attribute_name': 'pcs_slug', 'value': data.slug}).status_code == 302
        assert _main_pair(app, 'race', data.fc) is None
        with data.engines[0].connect() as conn:
            changes = conn.execute(text('SELECT id FROM cdb_changes ORDER BY id')).scalars().all()
            assert conn.scalar(text('SELECT first_cycling_id FROM race WHERE id=42')) is None
        assert client.post(f'/n/alpha/attributes/race/review/{changes[0]}/approve').status_code == 403
        login(2)
        for change_id in changes:
            assert client.post(f'/n/alpha/attributes/race/review/{change_id}/approve').status_code == 302
        assert _main_pair(app, 'race', data.fc) == (data.fc, data.slug)
        with data.engines[1].connect() as conn:
            assert conn.scalar(text('SELECT first_cycling_id FROM race WHERE id=42')) is None
            assert conn.scalar(text('SELECT count(*) FROM identity_sync_outbox')) == 0
        response = client.get('/n/alpha/races')
        assert response.status_code == 200 and data.slug in response.get_data(as_text=True)
        assert f'/n/alpha/races/cdb/42' in response.get_data(as_text=True)
    finally:
        with app.app_context():
            AuditLog.query.filter(AuditLog.user_id.in_(user_ids)).delete()
            for user_id in user_ids:
                db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_conflict_review_page_and_retry_preserve_main_until_corrected(app, client, provider_namespaces, monkeypatch):
    from webapp.web import routes
    data = provider_namespaces
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: data.namespaces)
    with data.engines[0].begin() as conn:
        conn.execute(text('INSERT INTO race (id, first_cycling_id, pcs_slug) VALUES (42, :fc, :slug)'),
                     {'fc': data.fc, 'slug': data.slug + '-new'})
    with app.app_context():
        conn = db.session.connection(bind_arguments={'mapper': service.MODELS['race']})
        shared_id = service.merge_pair(conn, 'race', data.fc, data.slug + '-old')
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        assert _publish(data)['conflicts'] == 1
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.get('/identities/race')
        assert response.status_code == 200
        assert data.slug + '-new' in response.get_data(as_text=True)
        assert b'conflict' in response.data
        payload = {'namespace_id': data.namespaces[0].id}
        assert client.post('/identities/race/retry', data=payload).status_code == 302
        assert _main_pair(app, 'race', data.fc) == (data.fc, data.slug + '-old')
        assert client.post('/identities/race/retry', data={'namespace_id': -1}).status_code == 404
        assert client.post('/identities/race', data={
            'identity_id': shared_id, 'firstcycling_id': data.fc, 'pcs_slug': data.slug + '-new',
        }).status_code == 302
        assert client.post('/identities/race/retry', data=payload).status_code == 302
        with data.engines[0].connect() as conn:
            assert conn.scalar(text('SELECT status FROM identity_sync_outbox')) == 'published'
        with app.app_context():
            user = db.session.get(User, user_id)
            for role in user.roles.all():
                user.roles.remove(role)
            db.session.commit()
        assert client.post('/identities/race/retry', data=payload).status_code == 403
    finally:
        with app.app_context():
            AuditLog.query.filter_by(user_id=user_id).delete()
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
