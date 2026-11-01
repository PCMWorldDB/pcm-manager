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
