"""PCS result dates suppress FirstCycling rows in the rider detail response."""
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy_utils import create_database, drop_database
from sqlalchemy.engine import make_url

from webapp.extensions import db
from webapp.models import Namespace, Role, User
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.models.ns.schema import NamespaceBase


def test_rider_detail_uses_pcs_by_date_and_keeps_fc_on_other_dates(app, client):
    with app.app_context():
        base_url = make_url(db.engine.url)
    namespace_url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    create_database(namespace_url)
    namespace_engine = create_engine(namespace_url)
    namespace = None
    user_id = None
    namespace_id = None
    try:
        ActivityMonitorBase.metadata.create_all(namespace_engine)
        NamespaceBase.metadata.create_all(namespace_engine)
        today = date(2026, 1, 10)
        with namespace_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (id, identity_key, source_worlddb_rider_id, identity_generation,
                     first_cycling_id, first_name, last_name, team_name,
                     identity_status, identity_hold, eligible, present_in_cdb,
                     created_at, updated_at)
                VALUES (1, 'worlddb:991:g0', 991, 0, 991, 'Test', 'Rider', 'Team',
                        'verified', FALSE, TRUE, TRUE, :now, :now)
            """), {"now": datetime.utcnow()})
            rows = [
                ("pcs-same-day", "pcs", today, "Tour Example", "stage_result", 1),
                ("fc-same-day", "firstcycling", today, "Different Race", "classification", 2),
                ("fc-other-day", "firstcycling", date(2026, 1, 9), "FC Only Race", "classification", 3),
            ]
            for key, source, result_date, race, row_type, rid in rows:
                connection.execute(text("""
                    INSERT INTO activity_monitor_results
                        (source_result_key, rider_id, source_worlddb_rider_id,
                         identity_generation, first_cycling_id, season, race_name,
                         race_category, race_url, result_date, stage_number, stage_name,
                         row_type, result_position, gc_position, participation_status,
                         source, identity_verified, imported_at)
                    VALUES (:key, 1, 991, 0, 991, 2026, :race, '', '', :result_date,
                            CASE WHEN :source = 'pcs' THEN 1 ELSE NULL END,
                            CASE WHEN :source = 'pcs' THEN 'Stage 1' ELSE '' END,
                            :row_type, CAST(:rid AS text), '', 'finished', :source,
                            TRUE, :now)
                """), {"key": key, "source": source, "result_date": result_date,
                       "race": race, "row_type": row_type, "rid": rid,
                       "now": datetime.utcnow()})

        with app.app_context():
            namespace = Namespace(
                name=f"Date precedence {uuid4().hex[:8]}",
                slug=f"date-precedence-{uuid4().hex[:8]}",
                database_name=namespace_url.database,
                status="active",
                is_template=False,
            )
            user = User(username=f"date-precedence-{uuid4().hex}", is_active=True)
            user.roles.append(Role.query.filter_by(name="viewer").one())
            db.session.add_all([namespace, user])
            db.session.commit()
            namespace_id, user_id = namespace.id, user.id

        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
            session["selected_namespace_id"] = namespace_id
        response = client.get("/activity-monitor/riders/1")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Tour Example" in body
        assert "Different Race" not in body
        assert "FC Only Race" in body
    finally:
        namespace_engine.dispose()
        with app.app_context():
            if user_id is not None:
                db.session.delete(db.session.get(User, user_id))
            if namespace_id is not None:
                db.session.delete(db.session.get(Namespace, namespace_id))
            db.session.commit()
        drop_database(namespace_url)
