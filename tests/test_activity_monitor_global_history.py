"""Global Activity Monitor history is isolated by namespace and linked by WorldDB ID."""
import os
import hashlib
from datetime import datetime
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.models.ns.schema import CDBCyclist, CDBTeam
from webapp.tools.activity_monitor_global_history import (
    archive_namespace_history,
    install_global_history_schema,
    restore_worlddb_history,
    sync_activity_monitor_roster,
)


def test_history_archive_dry_run_and_worlddb_link_are_idempotent(app):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    created = []
    engines = []
    namespace_slugs = [f"history-{uuid4().hex[:12]}", f"history-{uuid4().hex[:12]}"]
    try:
        for slug, worlddb_id in zip(namespace_slugs, (905, 906)):
            url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
            create_database(url)
            created.append(url)
            source_engine = create_engine(url)
            engines.append(source_engine)
            ActivityMonitorBase.metadata.create_all(source_engine)
            now = datetime.utcnow()
            with source_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO activity_monitor_riders
                        (id, identity_key, source_worlddb_rider_id, identity_generation,
                         first_name, last_name, team_name, identity_status, identity_hold,
                         eligible, present_in_cdb,
                         created_at, updated_at)
                    VALUES (1, :key, :worlddb_id, 0, 'Test', 'Rider', 'Free Agents',
                            'unverified', FALSE, TRUE, TRUE, :now, :now)
                """), {"key": f"worlddb:{worlddb_id}:g0", "worlddb_id": worlddb_id, "now": now})
                connection.execute(text("""
                    INSERT INTO activity_monitor_source_checks
                        (id, rider_id, source_worlddb_rider_id, identity_generation,
                         source, source_status, coverage_status, identity_result, checked_at)
                    VALUES (1, 1, :worlddb_id, 0, 'pcs', 'success', 'partial', 'verified', :now)
                """), {"worlddb_id": worlddb_id, "now": now})
                connection.execute(text("""
                    INSERT INTO activity_monitor_tasks
                        (id, rider_id, source_worlddb_rider_id, identity_generation,
                         state, source_scope, reason, reference_date, outcome,
                         created_at, updated_at)
                    VALUES (1, 1, :worlddb_id, 0, 'completed', '[]', 'test',
                            CURRENT_DATE, '', :now, :now)
                """), {"worlddb_id": worlddb_id, "now": now})
                connection.execute(text("""
                    INSERT INTO activity_monitor_task_events
                        (id, task_id, new_state, notes, occurred_at)
                    VALUES (1, 1, 'completed', '', :now)
                """), {"now": now})

        with app.app_context():
            main_engine = db.engines["main"]
            install_global_history_schema(main_engine)
            preview = archive_namespace_history(
                engines[0], main_engine, namespace_slugs[0], apply=False
            )
            assert preview["records"] == 3
            with main_engine.connect() as connection:
                assert connection.execute(text(
                    "SELECT COUNT(*) FROM activity_monitor_global_history WHERE namespace_slug = :slug"
                ), {"slug": namespace_slugs[0]}).scalar_one() == 0

            archive_namespace_history(engines[0], main_engine, namespace_slugs[0], apply=True)
            archive_namespace_history(engines[1], main_engine, namespace_slugs[1], apply=True)
            archive_namespace_history(engines[0], main_engine, namespace_slugs[0], apply=True)
            with main_engine.connect() as connection:
                linked = connection.execute(text("""
                    SELECT namespace_slug, record_data->>'source_worlddb_rider_id'
                    FROM activity_monitor_global_history
                    WHERE worlddb_rider_id = 905 AND record_type = 'activity_monitor_source_checks'
                """)).all()
                assert linked == [(namespace_slugs[0], '905')]
                assert connection.execute(text("""
                    SELECT COUNT(*) FROM activity_monitor_global_history
                    WHERE worlddb_rider_id = 905 AND record_type = 'activity_monitor_source_checks'
                """)).scalar_one() == 1
                assert connection.execute(text("""
                    SELECT COUNT(*) FROM activity_monitor_global_history
                    WHERE worlddb_rider_id = 906 AND record_type = 'activity_monitor_source_checks'
                """)).scalar_one() == 1
                assert connection.execute(text("""
                    SELECT COUNT(*) FROM activity_monitor_global_history
                    WHERE worlddb_rider_id = 905 AND record_type = 'activity_monitor_task_events'
                """)).scalar_one() == 1
                connection.execute(text(
                    "DELETE FROM activity_monitor_global_history WHERE namespace_slug = ANY(:slugs)"
                ), {"slugs": namespace_slugs})
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(created):
            drop_database(url)


def test_history_restores_by_worlddb_and_remaps_check_foreign_key(app):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    source_url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    destination_url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    source_engine = create_engine(source_url)
    destination_engine = create_engine(destination_url)
    source_slug = f"recovery-{uuid4().hex[:12]}"
    rider_worlddb_id = 1905
    now = datetime.utcnow()
    try:
        create_database(source_url)
        create_database(destination_url)
        ActivityMonitorBase.metadata.create_all(source_engine)
        ActivityMonitorBase.metadata.create_all(destination_engine)
        CDBTeam.__table__.create(destination_engine)
        CDBCyclist.__table__.create(destination_engine)
        with destination_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO cdb_cyclist (id, first_cycling_id, first_name, last_name, team_id)
                VALUES (1905, 77, 'Test', 'Rider', NULL), (1906, 78, 'Other', 'Rider', NULL)
            """))
        for engine, rider_id, worlddb_id in ((source_engine, 1, rider_worlddb_id),):
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO activity_monitor_riders
                        (id, identity_key, source_worlddb_rider_id, identity_generation,
                         first_name, last_name, team_name, identity_status, identity_hold,
                         eligible, present_in_cdb, created_at, updated_at)
                    VALUES (:id, :key, :worlddb_id, :generation, 'Test', 'Rider', 'Free Agents',
                            'unverified', FALSE, TRUE, TRUE, :now, :now)
                """), {"id": rider_id, "key": f"worlddb:{worlddb_id}:g{0 if rider_id == 1 else 3}",
                       "worlddb_id": worlddb_id, "generation": 0 if rider_id == 1 else 3, "now": now})
        with source_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_source_checks
                    (id, source_record_key, rider_id, source_worlddb_rider_id,
                     identity_generation, first_cycling_id, season, source, source_status,
                     coverage_status, identity_result, checked_at)
                    VALUES (1, 'source-check-1', 1, :worlddb_id, 0, 77, 2025, 'pcs', 'success',
                        'complete', 'verified', :now)
                """), {"now": now, "worlddb_id": rider_worlddb_id})
            connection.execute(text("""
                INSERT INTO activity_monitor_results
                    (id, source_result_key, rider_id, source_worlddb_rider_id,
                     identity_generation, first_cycling_id, season, race_name, race_category,
                     race_url, stage_name, row_type, result_position, gc_position,
                     participation_status, source, source_check_id, identity_verified, imported_at)
                    VALUES (1, 'source-result-1', 1, :worlddb_id, 0, 77, 2025, 'Test Race', '', '', '',
                        'stage', '', '', 'finished', 'pcs', 1, TRUE, :now)
                """), {"now": now, "worlddb_id": rider_worlddb_id})

        with app.app_context():
            main_engine = db.engines["main"]
            install_global_history_schema(main_engine)
            archive_namespace_history(source_engine, main_engine, source_slug, apply=True)
            with main_engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO activity_monitor_global_history
                        (namespace_slug, worlddb_rider_id, record_type, source_record_key,
                         occurred_at, record_data)
                    SELECT :second_slug, worlddb_rider_id, record_type, source_record_key,
                           occurred_at, record_data
                    FROM activity_monitor_global_history WHERE namespace_slug = :source_slug
                """), {"second_slug": f"{source_slug}-other", "source_slug": source_slug})
            synced_ids = sync_activity_monitor_roster(destination_engine)
            assert synced_ids == [1905, 1906]
            local_check_key = "archive:" + hashlib.sha256(
                f"{source_slug}\0activity_monitor_source_checks\0source-check-1".encode()
            ).hexdigest()
            with destination_engine.begin() as connection:
                destination_rider_id = connection.execute(text("""
                    SELECT id FROM activity_monitor_riders WHERE source_worlddb_rider_id = :worlddb_id
                """), {"worlddb_id": rider_worlddb_id}).scalar_one()
                connection.execute(text("""
                    INSERT INTO activity_monitor_source_checks
                        (source_record_key, rider_id, source_worlddb_rider_id,
                         identity_generation, source, source_status, coverage_status,
                         identity_result, checked_at, message)
                        VALUES (:key, :rider_id, :worlddb_id, 0, 'pcs', 'failure', 'partial',
                            'verified', :now, 'newer local check')
                    """), {"key": local_check_key, "rider_id": destination_rider_id,
                           "worlddb_id": rider_worlddb_id, "now": now})
            first = restore_worlddb_history(main_engine, destination_engine, synced_ids)
            second = restore_worlddb_history(main_engine, destination_engine, synced_ids)
            assert first["restored"]["activity_monitor_source_checks"] == 1
            assert first["restored"]["activity_monitor_results"] == 2
            assert second["restored"]["activity_monitor_source_checks"] == 0
            assert second["restored"]["activity_monitor_results"] == 0
            with destination_engine.connect() as connection:
                check_ids = connection.execute(text("""
                    SELECT c.id FROM activity_monitor_source_checks c
                    JOIN activity_monitor_riders r ON r.id = c.rider_id
                    WHERE r.source_worlddb_rider_id = :worlddb_id ORDER BY c.id
                """), {"worlddb_id": rider_worlddb_id}).scalars().all()
                result_rows = connection.execute(text("""
                    SELECT r.source_worlddb_rider_id, a.source_check_id, a.identity_generation
                    FROM activity_monitor_results a
                    JOIN activity_monitor_riders r ON r.id = a.rider_id
                    ORDER BY a.id
                """)).all()
                assert len(check_ids) == 2
                assert len({row.source_check_id for row in result_rows}) == 2
                assert all(row.source_worlddb_rider_id == rider_worlddb_id and row.identity_generation == 0
                           for row in result_rows)
                assert {row.source_check_id for row in result_rows} == set(check_ids)
                assert connection.execute(text("""
                    SELECT message FROM activity_monitor_source_checks
                    WHERE source_record_key = :key
                """), {"key": local_check_key}).scalar_one() == "newer local check"
                assert connection.execute(text("""
                    SELECT COUNT(*) FROM activity_monitor_source_checks
                    WHERE source_worlddb_rider_id = 1906
                """)).scalar_one() == 0
            with main_engine.begin() as connection:
                connection.execute(text("""
                    DELETE FROM activity_monitor_global_history
                    WHERE namespace_slug = ANY(:slugs)
                """), {"slugs": [source_slug, f"{source_slug}-other"]})
    finally:
        source_engine.dispose()
        destination_engine.dispose()
        drop_database(destination_url)
        drop_database(source_url)


def test_roster_sync_starts_new_generation_after_reintroduction(app):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    namespace_url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    engine = create_engine(namespace_url)
    now = datetime.utcnow()
    try:
        create_database(namespace_url)
        ActivityMonitorBase.metadata.create_all(engine)
        CDBTeam.__table__.create(engine)
        CDBCyclist.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (identity_key, identity_generation, first_name, last_name, team_name,
                     identity_status, identity_hold, eligible, present_in_cdb,
                     created_at, updated_at)
                VALUES ('manual:unlinked', 0, 'Manual', 'Rider', '', 'unverified',
                        FALSE, TRUE, TRUE, :now, :now)
            """), {"now": now})
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (identity_key, source_worlddb_rider_id, identity_generation,
                     first_name, last_name, team_name, identity_status, identity_hold,
                     eligible, present_in_cdb, created_at, updated_at)
                VALUES ('worlddb:2001:g0', 2001, 0, 'Returning', 'Rider', '',
                        'verified', FALSE, TRUE, TRUE, :now, :now)
            """), {"now": now})
            connection.execute(text("""
                INSERT INTO cdb_cyclist (id, first_name, last_name)
                VALUES (2001, 'Returning', 'Rider')
            """))
        assert sync_activity_monitor_roster(engine) == [2001]
        with engine.connect() as connection:
            assert connection.execute(text("""
                SELECT present_in_cdb FROM activity_monitor_riders
                WHERE source_worlddb_rider_id IS NULL
            """)).scalar_one() is True
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM cdb_cyclist WHERE id = 2001"))
        assert sync_activity_monitor_roster(engine) == []
        with engine.begin() as connection:
            absent_rows = connection.execute(text("""
                SELECT source_worlddb_rider_id, identity_generation, present_in_cdb
                FROM activity_monitor_riders ORDER BY id
            """)).all()
            assert absent_rows == [(None, 0, True), (2001, 0, False)]
            connection.execute(text("""
                INSERT INTO cdb_cyclist (id, first_name, last_name)
                VALUES (2001, 'Returning', 'Rider')
            """))
        assert sync_activity_monitor_roster(engine) == [2001]
        with engine.connect() as connection:
            generations = connection.execute(text("""
                SELECT identity_generation, present_in_cdb
                FROM activity_monitor_riders
                WHERE source_worlddb_rider_id = 2001 ORDER BY identity_generation
            """)).all()
            assert generations == [(0, False), (1, True)]
            assert connection.execute(text("""
                SELECT present_in_cdb FROM activity_monitor_riders
                WHERE source_worlddb_rider_id IS NULL
            """)).scalar_one() is True
    finally:
        engine.dispose()
        drop_database(namespace_url)
