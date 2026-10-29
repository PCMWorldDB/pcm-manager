"""Generate real release files from a synthetic CDB; mock only data selection."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from webapp.integrations.cdb import apply_updates_to_sqlite, get_field_mapping
from webapp.web import routes


@pytest.fixture
def cdb(tmp_path):
    mapping = get_field_mapping("PCM2026", "cyclist")
    table = mapping["cdb_table_name"]
    fields = mapping["fields"]
    path = tmp_path / "baseline.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(f'CREATE TABLE "{table}" ("{fields["id"]}" INTEGER PRIMARY KEY, "{fields["stat_mo"]}" INTEGER, untouched BLOB)')
        conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?)', [(1, 70, b"preserve"), (2, 65, b"other")])
        conn.execute("CREATE TABLE unknown_game_table (payload TEXT)")
        conn.execute("INSERT INTO unknown_game_table VALUES ('keep me')")
    return path, table, fields


def test_cdb_update_rolls_back_on_invalid_column(cdb):
    path, table, fields = cdb
    with pytest.raises(sqlite3.OperationalError):
        apply_updates_to_sqlite(path, table, fields["id"], {
            (1, fields["stat_mo"]): 80,
            (1, "nonexistent_stat"): 75,
        })
    with sqlite3.connect(path) as conn:
        assert conn.execute(f'SELECT "{fields["stat_mo"]}" FROM "{table}" WHERE "{fields["id"]}" = 1').fetchone() == (70,)


def test_release_generates_three_artifacts_and_preserves_cdb(app, monkeypatch, cdb):
    path, table, fields = cdb
    baseline = path.read_bytes()
    namespace = SimpleNamespace(slug="test-release", name="Test Namespace", database_year=2026, gender="Men")
    release = SimpleNamespace(id=1, release_name="Regression Release", version="1.0", status="draft", release_notes="<test>", include_stats_editor_changes=False)
    monkeypatch.setattr(routes, "_resolve_release_base_sqlite_path", lambda *_: str(path))
    monkeypatch.setattr(routes, "_load_release_stat_updates", lambda *_: {
        "table_name": table, "id_column": fields["id"], "updates": {(1, fields["stat_mo"]): 80},
    })
    monkeypatch.setattr(routes, "_load_release_dyn_cyclist_rows", lambda *_: [{"id": 1, "first_name": "Fixture", "last_name": "Rider", "stat_mo": 80}])
    monkeypatch.setattr(routes, "_load_namespace_approved_release_items", lambda *a, **kw: [{"is_selected": True, "name": "Included race", "total_edits": 1}])
    recorded = []
    monkeypatch.setattr(routes, "_replace_namespace_release_files", lambda ns, rid, files: recorded.extend(files))
    with app.app_context():
        result = routes._build_review_artifacts_for_release(namespace, release)
    assert {item["file_type"] for item in recorded} == {"cdb_sqlite", "rider_stats_sql", "release_overview_html"}
    assert result["applied_changes"] == 1
    files = {item["file_type"]: Path("filestore/namespaces/test-release") / item["relative_path"] for item in recorded}
    assert all(file.is_file() for file in files.values())
    with sqlite3.connect(files["cdb_sqlite"]) as conn:
        assert conn.execute(f'SELECT * FROM "{table}" ORDER BY "{fields["id"]}"').fetchall() == [(1, 80, b"preserve"), (2, 65, b"other")]
        assert conn.execute("SELECT * FROM unknown_game_table").fetchall() == [("keep me",)]
    assert path.read_bytes() == baseline
    sql = files["rider_stats_sql"].read_text(encoding="utf-8")
    assert "INSERT INTO `dyn_cyclist`" in sql
    assert "Fixture" in sql and "'80'" in sql
    overview = files["release_overview_html"].read_text(encoding="utf-8")
    assert "Included race" in overview and "&lt;test&gt;" in overview
