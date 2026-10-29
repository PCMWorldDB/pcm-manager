"""Use separate PostgreSQL databases with overlapping rider IDs."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.models import Namespace
from webapp.models.ns.schema import NamespaceBase
from webapp.web.routes import _ensure_namespace_database_connection, _load_namespace_roster_rider


def test_rider_reads_and_writes_stay_in_selected_namespace(app):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    namespaces = []
    engines = []
    urls = []
    try:
        for label in ("Alpha", "Beta"):
            name = f"worlddb_test_{uuid4().hex}"
            url = base_url.set(database=name)
            create_database(url)
            urls.append(url)
            engine = create_engine(url)
            engines.append(engine)
            NamespaceBase.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO cdb_cyclist (id, first_name, last_name, stat_mo) "
                    "VALUES (1, :name, 'Rider', 70)"
                ), {"name": label})
            namespaces.append(Namespace(name=label, slug=label.lower(), database_name=name))

        with engines[0].begin() as conn:
            _ensure_namespace_database_connection(conn, namespaces[0])
            with pytest.raises(RuntimeError, match="expected namespace database"):
                _ensure_namespace_database_connection(conn, namespaces[1])
            conn.execute(text("UPDATE cdb_cyclist SET stat_mo = 80 WHERE id = 1"))

        with app.app_context():
            alpha = _load_namespace_roster_rider(namespaces[0], 1)
            beta = _load_namespace_roster_rider(namespaces[1], 1)
            assert alpha is not None and beta is not None
            assert alpha.first_name == "Alpha" and alpha.stat_mo == 80
            assert beta.first_name == "Beta" and beta.stat_mo == 70
        with engines[1].connect() as conn:
            assert conn.execute(text("SELECT stat_mo FROM cdb_cyclist WHERE id = 1")).scalar() == 70
    finally:
        for engine in engines:
            engine.dispose()
        for url in reversed(urls):
            drop_database(url)
