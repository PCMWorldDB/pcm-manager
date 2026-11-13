"""Individual scopes remain distinct from lists and race results in releases."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import AuditLog, FirstCyclingRace, RaceResultMap
from webapp.models.ns.base import NamespaceBase
from webapp.web import routes


def test_individual_release_grouping_and_overview(app, monkeypatch):
    schemas = ['individual_' + uuid4().hex for _ in range(2)]
    engines = []
    with app.app_context():
        race = FirstCyclingRace(race_id=876543210, season_year=2026, gender='Men', name='Test race')
        mapped = RaceResultMap(provider='firstcycling', external_race_id=str(race.race_id))
        db.session.add_all([race, mapped])
        db.session.commit()
        result_id = mapped.race_result_id
        race_id = race.id
        for schema in schemas:
            db.session.execute(text(f'CREATE SCHEMA {schema}'))
            db.session.commit()
            engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO rider (id,first_name,last_name,stat_mo) VALUES (1,'Full','Name',72),(2,'Pending','Rider',70)"))
                conn.execute(text("INSERT INTO rider_list (id,list_name,season_year,gender) VALUES (1,'Test list',2026,'Men')"))
                conn.execute(text('''
                    INSERT INTO stat_changes (race_result_id,stat_list_id,rider_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
                    VALUES (NULL,NULL,1,'mo',70,72,2,TRUE,FALSE),
                           (NULL,NULL,2,'mo',70,73,3,FALSE,FALSE),
                           (NULL,1,1,'mm',70,71,1,TRUE,FALSE),
                           (:result,NULL,1,'tt',70,71,1,TRUE,FALSE)
                '''), {'result': result_id})
    namespaces = [SimpleNamespace(id=100 + i, slug=f'individual-{i}', name='Test namespace',
                                 database_year=2026, gender='Men', database_name='worlddb_test',
                                 build_connection_string=lambda i=i: f'ns-{i}')
                  for i in range(2)]
    monkeypatch.setattr(routes, 'create_engine', lambda url: engines[int(url.removeprefix('ns-'))])
    monkeypatch.setattr(routes, '_load_namespace_roster_rider', lambda ns, rider_id: SimpleNamespace(name='Full Name'))
    captured = []
    event_ids = []
    monkeypatch.setattr(routes, '_create_release_text_artifact', lambda ns, name, content, mime: captured.append(content) or {})
    try:
        with app.test_request_context():
            with engines[1].begin() as conn:
                conn.execute(text('UPDATE stat_changes SET final_value=84'))
            log = routes._load_namespace_rider_stat_edits(namespaces[0], limit=101)
            assert {entry['source']['type'] for entry in log} == {'Individual', 'Stat List', 'Race Result'}
            assert len(log) == 4
            assert all(entry['final_value'] != 84 for entry in log)
            assert [entry['id'] for entry in log] == sorted((entry['id'] for entry in log), reverse=True)
            page = routes._load_namespace_rider_stat_edits(namespaces[0], limit=2, offset=2)
            assert [entry['id'] for entry in page] == [entry['id'] for entry in log[2:]]
            items = routes._load_namespace_approved_release_items(namespaces[0], 2026, 'Men')
            assert {item['type_label'] for item in items} == {'Individual', 'List', 'Race Result'}
            individual = next(item for item in items if item['scope_type'] == 'individual')
            assert individual['name'] == 'Full Name'
            assert individual['total_edits'] == individual['riders_edited'] == 1
            assert individual['item_key'] == 'individual:0:0:1'
            summary = routes._load_namespace_stats_release_summary(namespaces[0])
            assert summary['individuals_included'] == summary['stat_lists_included'] == summary['race_results_included'] == 1
            assert summary['total_edits'] == 3
            assert routes._sync_release_stats_change_type(namespaces[0], 7, True) == 3
            with engines[0].connect() as conn:
                assert conn.execute(text('SELECT release_id FROM stat_changes WHERE rider_id=2')).scalar_one() is None
            with engines[1].connect() as conn:
                assert conn.execute(text('SELECT COUNT(*) FROM stat_changes WHERE release_id IS NOT NULL')).scalar_one() == 0
            history = routes._load_namespace_rider_stat_edits(namespaces[0], 1)
            entry = next(edit for edit in history if edit['source']['type'] == 'Individual')
            assert entry['source']['label'] == 'Full Name'
            release = SimpleNamespace(id=7, release_name='Test release', version='v1', status='review', release_notes='Test')
            routes._build_release_overview_html_for_release(namespaces[0], release, 'test', [])
            assert '<td>Individual</td>' in captured[0]
            assert '<td>Race Result</td>' in captured[0]
            assert '<td>List</td>' in captured[0]
            with engines[0].begin() as conn:
                conn.execute(text('''
                    INSERT INTO stat_changes (stat_list_id,rider_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
                    VALUES (1,2,'mo',70,74,4,FALSE,FALSE)
                '''))
            event = AuditLog(username='Test', action='individual_stats_approved',
                             resource_type='namespace:100:individual', resource_id=1,
                             created_at=datetime(2026, 11, 2, 12, 30, 15))
            db.session.add(event)
            db.session.commit()
            event_ids.append(event.id)
            groups = routes._load_namespace_approved_release_items(namespaces[0], log_mode=True, limit=101)
            assert len(groups) == 4  # two individuals, one list, one race result
            assert groups[0]['scope_type'] == 'individual'
            assert groups[0]['approved_on'] == datetime(2026, 11, 2, 12, 30, 15)
            assert 'release_id=7' in groups[0]['releases'][0]['url']
            list_group = next(item for item in groups if item['scope_type'] == 'list')
            assert list_group['total_edits'] == list_group['riders_edited'] == 2
            assert not list_group['is_approved']
            assert not list_group['is_released']
            assert any(item['is_released'] for item in groups)
            page = routes._load_namespace_approved_release_items(namespaces[0], log_mode=True, limit=2, offset=2)
            assert [item['item_key'] for item in page] == [item['item_key'] for item in groups[2:]]
    finally:
        for engine in engines:
            engine.dispose()
        with app.app_context():
            AuditLog.query.filter(AuditLog.id.in_(event_ids)).delete(synchronize_session=False)
            db.session.delete(db.session.get(FirstCyclingRace, race_id))
            db.session.delete(db.session.get(RaceResultMap, result_id))
            for schema in schemas:
                db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()


def test_individual_scope_applies_only_selected_rider(app):
    schema = 'individual_apply_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        NamespaceBase.metadata.create_all(engine)
        try:
            with engine.begin() as conn:
                conn.execute(text('INSERT INTO rider (id,stat_mo,stat_tt) VALUES (1,70,70),(2,70,70)'))
                conn.execute(text("INSERT INTO stat_changes (stat_list_id,rider_id,stat_name,original_value,final_value,delta_value,is_approved,release_id,is_confilict_override) VALUES (NULL,1,'mo',70,72,2,FALSE,7,FALSE),(NULL,2,'mo',70,75,5,FALSE,NULL,FALSE),(2,1,'tt',70,71,1,FALSE,NULL,FALSE)"))
                assert routes._apply_scope_stat_changes_to_cdb(
                    conn, routes.INDIVIDUAL_STAT_SCOPE, {'rider_id': 1},
                ) == 1
            with engine.connect() as conn:
                assert conn.execute(text('SELECT id,stat_mo,stat_tt FROM rider ORDER BY id')).all() == [
                    (1, 72, 70), (2, 70, 70),
                ]
                assert conn.execute(text(
                    "SELECT release_id FROM stat_changes WHERE rider_id=1 AND stat_name='mo'"
                )).scalar_one() == 7
                assert conn.execute(text('SELECT COUNT(*) FROM stat_changes')).scalar_one() == 3
        finally:
            engine.dispose()
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()
