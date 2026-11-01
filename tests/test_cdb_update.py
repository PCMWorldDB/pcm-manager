"""CDB namespace updates replace only mapped CDB tables atomically."""
import os
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp.integrations.cdb import get_field_mapping
from webapp.models.ns.schema import NamespaceBase
from webapp.namespace.setup import CDBImporter


def _make_cdb(path, rider_id, *, omit_cyclist=False):
    mappings = {
        name: get_field_mapping("PCM2026", name)
        for name in ("country", "division", "region", "team", "cyclist")
    }
    with sqlite3.connect(path) as connection:
        for kind, mapping in mappings.items():
            if omit_cyclist and kind == "cyclist":
                continue
            table_name = mapping["cdb_table_name"]
            columns = list(dict.fromkeys(mapping["fields"].values()))
            quoted_columns = ", ".join(f'"{column}" TEXT' for column in columns)
            connection.execute(f'CREATE TABLE "{table_name}" ({quoted_columns})')
            if kind == "cyclist":
                values = [None] * len(columns)
                values[columns.index(mapping["fields"]["id"])] = rider_id
                values[columns.index(mapping["fields"]["first_name"])] = "Updated"
                values[columns.index(mapping["fields"]["last_name"])] = "Rider"
                placeholders = ", ".join("?" for _ in columns)
                quoted = ", ".join(f'"{column}"' for column in columns)
                connection.execute(
                    f'INSERT INTO "{table_name}" ({quoted}) VALUES ({placeholders})',
                    values,
                )


def test_cdb_update_replaces_mapped_data_and_rolls_back_invalid_import(app, tmp_path):
    base_url = make_url(os.environ["WORLDDB_TEST_DATABASE_URL"])
    url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    create_database(url)
    engine = create_engine(url)
    try:
        NamespaceBase.metadata.create_all(engine)
        first_cdb = tmp_path / "first.sqlite"
        second_cdb = tmp_path / "second.sqlite"
        invalid_cdb = tmp_path / "invalid.sqlite"
        _make_cdb(first_cdb, 101)
        _make_cdb(second_cdb, 202)
        _make_cdb(invalid_cdb, 303, omit_cyclist=True)

        with CDBImporter(str(first_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
            importer.import_all_tables(replace_existing=True)
        with CDBImporter(str(second_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
            importer.import_all_tables(replace_existing=True)
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, first_name FROM cdb_cyclist ORDER BY id"
            )).all()
            assert rows == [(202, "Updated")]

        with pytest.raises(sqlite3.OperationalError):
            with CDBImporter(str(invalid_cdb), url.render_as_string(hide_password=False), "PCM2026") as importer:
                importer.import_all_tables(replace_existing=True)
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT id, first_name FROM cdb_cyclist ORDER BY id"
            )).all()
            assert rows == [(202, "Updated")]
    finally:
        engine.dispose()
        drop_database(url)
