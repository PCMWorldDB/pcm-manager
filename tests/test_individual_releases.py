"""Individual scopes remain distinct from lists and race results in releases."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models import FirstCyclingRace, RaceResultMap
from webapp.models.ns.schema import NamespaceBase
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
        for schema in schemas:
            db.session.execute(text(f'CREATE SCHEMA {schema}'))
            db.session.commit()
            engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO cdb_cyclist (id,first_name,last_name,stat_mo) VALUES (1,'Full','Name',72),(2,'Pending','Rider',70)"))
                conn.execute(text("INSERT INTO stat_lists (id,list_name,season_year,gender) VALUES (1,'Test list',2026,'Men')"))
                conn.execute(text('''
                    INSERT INTO stat_changes (race_result_id,stat_list_id,cdb_cyclist_id,stat_name,original_value,final_value,delta_value,is_approved,is_confilict_override)
                    VALUES (NULL,NULL,1,'mo',70,72,2,TRUE,FALSE),
                           (NULL,NULL,2,'mo',70,73,3,FALSE,FALSE),
                           (NULL,1,1,'mm',70,71,1,TRUE,FALSE),
                           (:result,NULL,1,'tt',70,71,1,TRUE,FALSE)
                '''), {'result': result_id})
    namespaces = [SimpleNamespace(id=100 + i, slug=f'individual-{i}', name='Test namespace',
                                 database_year=2026, gender='Men', build_connection_string=lambda i=i: f'ns-{i}')
                  for i in range(2)]
    monkeypatch.setattr(routes, 'create_engine', lambda url: engines[int(url.removeprefix('ns-'))])
    monkeypatch.setattr(routes, '_load_namespace_roster_rider', lambda ns, rider_id: SimpleNamespace(name='Full Name'))
    captured = []
    monkeypatch.setattr(routes, '_create_release_text_artifact', lambda ns, name, content, mime: captured.append(content) or {})
    try:
        with app.test_request_context():
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
                assert conn.execute(text('SELECT release_id FROM stat_changes WHERE cdb_cyclist_id=2')).scalar_one() is None
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
    finally:
        for engine in engines:
            engine.dispose()
        with app.app_context():
            db.session.delete(db.session.get(FirstCyclingRace, race.id))
            db.session.delete(db.session.get(RaceResultMap, result_id))
            for schema in schemas:
                db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()


def test_profile_list_conversion_preserves_approval_and_release(app):
    schema = 'conversion_' + uuid4().hex
    with app.app_context():
        db.session.execute(text(f'CREATE SCHEMA {schema}'))
        db.session.commit()
        engine = create_engine(db.engine.url, connect_args={'options': f'-csearch_path={schema}'})
        NamespaceBase.metadata.create_all(engine)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO stat_lists (id,list_name,season_year,gender) VALUES (1,'Rider profile #1',2026,'Men'),(2,'Ordinary list',2026,'Men')"))
                conn.execute(text('INSERT INTO stat_list_items (stat_list_id,cdb_cyclist_id) VALUES (1,1),(2,1)'))
                conn.execute(text("INSERT INTO stat_changes (stat_list_id,cdb_cyclist_id,stat_name,original_value,final_value,delta_value,is_approved,release_id) VALUES (1,1,'mo',70,72,2,TRUE,7),(2,1,'tt',70,71,1,FALSE,NULL)"))
            sql = (Path(__file__).resolve().parents[1] / 'migrations/convert_profile_lists_to_individual.sql').read_text()
            raw = engine.raw_connection()
            try:
                with raw.cursor() as cursor:
                    cursor.execute(sql)
                raw.commit()
            finally:
                raw.close()
            with engine.connect() as conn:
                row = conn.execute(text("SELECT stat_list_id,is_approved,release_id,final_value FROM stat_changes WHERE stat_name='mo'")).one()
                assert tuple(row) == (None, True, 7, 72)
                assert conn.execute(text('SELECT id FROM stat_lists')).scalars().all() == [2]
        finally:
            engine.dispose()
            db.session.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            db.session.commit()
