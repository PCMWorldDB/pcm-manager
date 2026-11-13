"""Convert PCM CDB files to and from SQLite."""

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path


DEFAULT_CONVERTER_CLI = "/opt/cdb-converter/node_modules/cdb-converter/dist/cli.mjs"


def validate_sqlite_cdb(sqlite_path):
    """Ensure an uploaded or converted CDB is a readable, intact SQLite file."""
    path = Path(sqlite_path).resolve()
    if not path.is_file():
        raise ValueError("The converted CDB file was not created.")

    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("The SQLite CDB failed its integrity check.")

            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
            if not tables:
                raise ValueError("The SQLite CDB contains no tables.")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("The file is not a readable SQLite CDB.") from exc


def convert_cdb_to_sqlite(source_path, sqlite_path):
    """Convert a PCM .cdb file using the same converter as Activity Monitor."""
    node = shutil.which(os.getenv("CDB_CONVERTER_NODE", "node"))
    converter_cli = Path(
        os.getenv("CDB_CONVERTER_CLI", DEFAULT_CONVERTER_CLI)
    )
    if not node or not converter_cli.is_file():
        raise RuntimeError(
            "The PCM CDB converter is not installed in this application image."
        )

    source = Path(source_path).resolve()
    target = Path(sqlite_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [node, str(converter_cli), str(source), str(target), "--precise-types"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("CDB conversion timed out after 10 minutes.") from exc

    if result.returncode:
        details = (result.stderr or result.stdout or "").strip()[-1000:]
        raise ValueError("CDB conversion failed" + (f": {details}" if details else "."))

    validate_sqlite_cdb(target)


def convert_sqlite_to_cdb(sqlite_path, cdb_path):
    """Convert a SQLite CDB into PCM's packed .cdb format."""
    node = shutil.which(os.getenv("CDB_CONVERTER_NODE", "node"))
    converter_cli = Path(
        os.getenv("CDB_CONVERTER_CLI", DEFAULT_CONVERTER_CLI)
    )
    if not node or not converter_cli.is_file():
        raise RuntimeError(
            "The PCM CDB converter is not installed in this application image."
        )

    source = Path(sqlite_path).resolve()
    target = Path(cdb_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [node, str(converter_cli), str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("CDB conversion timed out after 10 minutes.") from exc

    if result.returncode:
        details = (result.stderr or result.stdout or "").strip()[-1000:]
        raise ValueError("CDB conversion failed" + (f": {details}" if details else "."))
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError("The converted CDB file was not created correctly.")
