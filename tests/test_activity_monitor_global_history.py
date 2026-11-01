"""Global Activity Monitor history is isolated by namespace and linked by WorldDB ID."""
import os
from datetime import datetime
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.tools.activity_monitor_global_history import (
    archive_namespace_history,
    install_global_history_schema,
    restore_worlddb_history,
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
    now = datetime.utcnow()
    try:
        create_database(source_url)
        create_database(destination_url)
        ActivityMonitorBase.metadata.create_all(source_engine)
        ActivityMonitorBase.metadata.create_all(destination_engine)
        for engine, rider_id, worlddb_id in ((source_engine, 1, 905), (destination_engine, 55, 905)):
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
                VALUES (1, 'source-check-1', 1, 905, 0, 77, 2025, 'pcs', 'success',
                        'complete', 'verified', :now)
            """), {"now": now})
            connection.execute(text("""
                INSERT INTO activity_monitor_results
                    (id, source_result_key, rider_id, source_worlddb_rider_id,
                     identity_generation, first_cycling_id, season, race_name, race_category,
                     race_url, stage_name, row_type, result_position, gc_position,
                     participation_status, source, source_check_id, identity_verified, imported_at)
                VALUES (1, 'source-result-1', 1, 905, 0, 77, 2025, 'Test Race', '', '', '',
                        'stage', '', '', 'finished', 'pcs', 1, TRUE, :now)
            """), {"now": now})

        with app.app_context():
            main_engine = db.engines["main"]
            install_global_history_schema(main_engine)
            archive_namespace_history(source_engine, main_engine, source_slug, apply=True)
            first = restore_worlddb_history(main_engine, destination_engine, [905, 906])
            second = restore_worlddb_history(main_engine, destination_engine, [905, 906])
            assert first["restored"]["activity_monitor_source_checks"] == 1
            assert first["restored"]["activity_monitor_results"] == 1
            assert second["restored"]["activity_monitor_source_checks"] == 0
            with destination_engine.connect() as connection:
                check_id = connection.execute(text("""
                    SELECT id FROM activity_monitor_source_checks
                    WHERE rider_id = 55 AND source_worlddb_rider_id = 905
                """)).scalar_one()
                assert connection.execute(text("""
                    SELECT rider_id, source_check_id, identity_generation
                    FROM activity_monitor_results
                """)).one() == (55, check_id, 3)
                assert connection.execute(text("""
                    SELECT COUNT(*) FROM activity_monitor_source_checks
                    WHERE source_worlddb_rider_id = 906
                """)).scalar_one() == 0
    finally:
        source_engine.dispose()
        destination_engine.dispose()
        drop_database(destination_url)
        drop_database(source_url)
