#!/usr/bin/env python3
"""Inspect a PCM CDB SQLite table schema using the CDB integration."""

import sys
from pathlib import Path

# Support invocation by file path from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.integrations.cdb.api import main  # noqa: E402


if __name__ == '__main__':
    main()
