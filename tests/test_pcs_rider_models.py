"""Shared PCS storage preserves profile rows without namespace dependencies."""
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.extensions import db
from webapp.models import PcsRider, PcsRiderResult


@pytest.mark.parametrize('manual_sql', [False, True])
def test_pcs_rider_storage(app, manual_sql):
    schema = 'pcs_' + uuid4().hex
    with app.app_context():
        engine = db.engines['main']
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        isolated = create_engine(engine.url, connect_args={'options': f'-csearch_path={schema}'})
        try:
            if manual_sql:
                sql = (Path(__file__).resolve().parents[1] / 'webapp/tools/pcs_rider_tables_main.sql').read_text()
                with isolated.connect() as conn:
                    conn.exec_driver_sql(sql)
            else:
                db.metadatas['main'].create_all(isolated, tables=[PcsRider.__table__, PcsRiderResult.__table__])
            assert PcsRider.__table__.metadata is db.metadatas['main']
            with Session(isolated) as session:
                riders = [PcsRider(rider_slug=slug, profile_url=f'https://www.procyclingstats.com/rider/{slug}',
                                   display_name='Test Rider', birth_date=date(2000, 1, 1),
                                   nationality='Belgium', source_payload={'canonical_slug': slug})
                          for slug in ('test-rider', 'another-rider')]
                session.add_all(riders)
                session.flush()
                rider_ids = [rider.id for rider in riders]
                for rider in riders:
                    for row_type, result, status, confirmed in (
                        ('race_heading', '', 'not_confirmed', False),
                        ('stage_result', 'DNF', 'confirmed_participation', True),
                        ('one_day_result', 'DNS', 'dns_only', False),
                        ('classification', '12', 'classification_only', False),
                    ):
                        rider.results.append(PcsRiderResult(
                            source_result_key=row_type, race_id='example-2026',
                            race_url='https://www.procyclingstats.com/race/example-2026',
                            row_type=row_type, result_position=result,
                            participation_status=status, confirmed_participation=confirmed,
                            source_payload={'result': result},
                        ))
                session.commit()
                session.expire_all()
                rows = session.query(PcsRiderResult).filter_by(rider_id=rider_ids[0]).all()
                assert len(rows) == 4
                assert all(row.race_id == 'example-2026' and row.season is None for row in rows)
                assert {row.source_payload['result'] for row in rows} == {'', 'DNF', 'DNS', '12'}
                session.add(PcsRider(rider_slug='test-rider', profile_url='duplicate'))
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
                session.add(PcsRiderResult(
                    rider_id=rider_ids[0], source_result_key='stage_result', race_url='duplicate',
                    row_type='stage_result', participation_status='confirmed_participation',
                    confirmed_participation=True,
                ))
                with pytest.raises(IntegrityError):
                    session.commit()
                session.rollback()
                # Bulk delete exercises the database FK cascade, not ORM cascade.
                session.query(PcsRider).filter_by(id=rider_ids[0]).delete()
                session.commit()
                assert session.query(PcsRiderResult).filter_by(rider_id=rider_ids[0]).count() == 0
                assert session.query(PcsRiderResult).filter_by(rider_id=rider_ids[1]).count() == 4
        finally:
            isolated.dispose()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
