"""CDB history storage supports ORM initialization and the manual namespace SQL."""
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.extensions import db
from webapp.models.ns.cdb import CDBChange, CDBChangeEvent, CDB_TABLES
from webapp.models.ns.schema import NamespaceBase, Release, get_table_by_name


@pytest.mark.parametrize('manual_sql', [False, True])
def test_cdb_history_storage(app, manual_sql):
    schema = 'cdb_history_' + uuid4().hex
    with app.app_context():
        engine = db.engines['main']
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        isolated = create_engine(engine.url, connect_args={'options': f'-csearch_path={schema}'})
        try:
            Release.__table__.create(isolated)
            if manual_sql:
                sql = (Path(__file__).resolve().parents[1]
                       / 'webapp/tools/cdb_change_history_namespace.sql').read_text()
                with isolated.connect() as conn:
                    conn.exec_driver_sql(sql)
            else:
                NamespaceBase.metadata.create_all(
                    isolated, tables=[CDBChange.__table__, CDBChangeEvent.__table__])
            assert get_table_by_name('cdb_changes') is CDBChange
            assert CDBChange not in CDB_TABLES
            assert CDBChangeEvent not in CDB_TABLES
            with Session(isolated) as session:
                change = CDBChange(entity_type='rider', entity_id=1, operation='update',
                                   source='editor', attribute_name='team_id',
                                   before_value=None, after_value='2')
                session.add(change)
                session.add(CDBChange(entity_type='rider', entity_id=1, operation='update',
                                      source='editor', attribute_name='stat_mo',
                                      before_value='70', after_value='71', race_result_id=42,
                                      batch_id='submission-1'))
                session.flush()
                session.add(CDBChangeEvent(change_id=change.id, event_type='created'))
                session.commit()
                session.expire_all()
                assert change.attribute_name == 'team_id'
                assert change.before_value is None
                assert change.after_value == '2'
                stat_change = session.query(CDBChange).filter_by(attribute_name='stat_mo').one()
                assert (stat_change.before_value, stat_change.after_value) == ('70', '71')
                assert stat_change.race_result_id == 42
                assert stat_change.stat_list_id is None
                stat_change.race_result_id = None
                stat_change.stat_list_id = 7
                session.add(CDBChangeEvent(
                    change_id=stat_change.id, event_type='conflict_overridden', actor_id=10,
                    context={'reviewed_current_value': '72', 'reason': 'Reviewed correction'}))
                session.commit()
                session.expire_all()
                assert stat_change.stat_list_id == 7
                override = session.query(CDBChangeEvent).filter_by(event_type='conflict_overridden').one()
                assert override.actor_id == 10
                assert override.context['reviewed_current_value'] == '72'
                assert change.status == 'pending'
                assert change.created_at.tzinfo is not None
                event = session.query(CDBChangeEvent).filter_by(event_type='created').one()
                assert event.change_id == change.id
                assert event.occurred_at.tzinfo is not None
                session.delete(change)
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
                session.add(CDBChange(entity_type='team', entity_id=1, attribute_name='budget',
                                      operation='invalid', source='editor'))
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
                stat_change.race_result_id = 42
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
                assert session.query(CDBChange).count() == 2
        finally:
            isolated.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
