"""Exercise the CDB inspection command with a synthetic SQLite file."""
import sqlite3
import subprocess
import sys
from pathlib import Path


def run_cli(*args):
    return subprocess.run(
        [sys.executable, '-m', 'webapp.integrations.cdb.cli', *map(str, args)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )


def test_table_inspection_preserves_file(tmp_path):
    cdb_path = tmp_path / 'sample.cdb'
    with sqlite3.connect(cdb_path) as connection:
        connection.execute('CREATE TABLE DYN_team (IDteam INTEGER PRIMARY KEY, name TEXT)')
        connection.execute("INSERT INTO DYN_team VALUES (1, 'Test Team')")
    original = cdb_path.read_bytes()

    result = run_cli(cdb_path, '--table', 'DYN_team')

    assert result.returncode == 0, result.stderr
    assert '[DYN_team] rows=1' in result.stdout
    assert 'Test Team' in result.stdout
    assert cdb_path.read_bytes() == original

    result = run_cli(cdb_path, '--table', 'missing')
    assert result.returncode != 0
    assert "No table matching 'missing' found." in result.stderr
    assert cdb_path.read_bytes() == original


def test_missing_file_is_not_created(tmp_path):
    cdb_path = tmp_path / 'missing.cdb'
    result = run_cli(cdb_path)
    assert result.returncode != 0
    assert 'File not found:' in result.stderr
    assert not cdb_path.exists()
