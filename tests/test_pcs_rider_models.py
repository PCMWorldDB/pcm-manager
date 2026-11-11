"""Shared PCS storage preserves profile rows without namespace dependencies."""
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.extensions import db
from webapp.models import PcsRider, PcsRiderResult
from webapp.integrations.rider_checks import _store_pcs_profile


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
                                   nationality='Belgium', source_payload={'canonical_slug': slug},
                                   latest_participation_date=date(2026, 5, 1),
                                   latest_participation_race_name='Example race',
                                   latest_participation_confirmed=True)
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
                stored = session.get(PcsRider, rider_ids[0])
                assert stored.latest_participation_date == date(2026, 5, 1)
                assert stored.latest_participation_race_name == 'Example race'
                assert stored.latest_participation_confirmed is True
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


@pytest.mark.parametrize('latest_fields', [
    {}, {'latest_participation': None}, {'latest_participation': {}},
])
def test_profile_upsert_replaces_and_preserves_latest_participation(app, latest_fields):
    slug = 'latest-' + uuid4().hex
    latest = dict(
        year=2022, date='2022-05-07', race_name='Clásica de Pascua - Ruta Xacobea',
        race_category='NAT', race_type='non-UCI', race_id='clasica-de-pascua-2022',
        race_url='https://www.procyclingstats.com/race/clasica-de-pascua-2022-result',
        stage_number=None, stage_name='', row_type='one_day_result', result='102',
        participation_status='confirmed_participation', confirmed_participation=True,
    )
    payload = dict(profile={'display_name': 'Adrián Lozano'}, rows=[latest], latest_participation=latest)
    checked_at = datetime(2026, 5, 20, 12)
    with app.app_context():
        try:
            _store_pcs_profile(slug, payload, checked_at)
            db.session.commit()
            rider = PcsRider.query.filter_by(rider_slug=slug).one()
            rider_id = rider.id
            assert {
                'year': rider.latest_participation_year, 'date': rider.latest_participation_date,
                'race_name': rider.latest_participation_race_name,
                'race_category': rider.latest_participation_race_category,
                'race_type': rider.latest_participation_race_type,
                'race_id': rider.latest_participation_race_id,
                'race_url': rider.latest_participation_race_url,
                'stage_number': rider.latest_participation_stage_number,
                'stage_name': rider.latest_participation_stage_name,
                'row_type': rider.latest_participation_row_type,
                'result': rider.latest_participation_result_position,
                'participation_status': rider.latest_participation_status,
                'confirmed_participation': rider.latest_participation_confirmed,
            } == {**latest, 'date': date(2022, 5, 7)}

            newer = {**latest, 'date': '2023-06-08', 'year': 2023, 'stage_number': 2,
                     'stage_name': 'Stage 2', 'row_type': 'stage_result', 'result': 'DNF'}
            _store_pcs_profile(slug, {**payload, 'rows': [latest, newer], 'latest_participation': newer},
                               checked_at + timedelta(days=1))
            db.session.commit()
            db.session.refresh(rider)
            assert rider.id == rider_id
            assert rider.latest_participation_date == date(2023, 6, 8)
            assert (rider.latest_participation_stage_number, rider.latest_participation_result_position) == (2, 'DNF')
            assert PcsRider.query.filter_by(rider_slug=slug).count() == 1
            assert PcsRiderResult.query.filter_by(rider_id=rider_id).count() == 2

            # A failure after the profile upsert rolls back the summary and result rows together.
            with pytest.raises(KeyError), db.session.begin_nested():
                _store_pcs_profile(slug, {**payload, 'rows': [{}]}, checked_at + timedelta(days=2))
            db.session.refresh(rider)
            assert rider.latest_participation_date == date(2023, 6, 8)

            summary_fields = [column.name for column in PcsRider.__table__.columns
                              if column.name.startswith('latest_participation_')]
            saved_summary = {field: getattr(rider, field) for field in summary_fields}
            empty_payload = dict(profile={'display_name': 'Updated profile'}, rows=[], **latest_fields)
            _store_pcs_profile(slug, empty_payload, checked_at + timedelta(days=3))
            db.session.commit()
            db.session.refresh(rider)
            assert {field: getattr(rider, field) for field in summary_fields} == saved_summary
            assert rider.display_name == 'Updated profile'
            assert rider.last_synced_at == checked_at + timedelta(days=3)
        finally:
            db.session.rollback()
            PcsRider.query.filter_by(rider_slug=slug).delete()
            db.session.commit()
