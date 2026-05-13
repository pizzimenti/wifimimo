"""Same-day history-CSV rotation when HISTORY_COLUMNS changes.

Regression test for the in-day upgrade path: if the daemon restarts after
a schema change and finds today's file with an old-shape header, it must
rotate the old file aside instead of appending new-shape rows under it.
"""

from __future__ import annotations

import csv
import importlib.util
import os
from pathlib import Path

import pytest

import wifimimo_core


def _load_daemon():
    spec = importlib.util.spec_from_file_location(
        "wifimimo_daemon", Path(__file__).parent.parent / "wifimimo-daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_history_rotates_on_header_mismatch(tmp_path):
    daemon_module = _load_daemon()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    today = "2026-05-13"
    existing = history_dir / f"{today}.csv"
    # Old-shape header (drop the trailing column we added in this PR).
    old_header = wifimimo_core.HISTORY_COLUMNS[:-1]
    existing.write_text(",".join(old_header) + "\n", encoding="utf-8")

    daemon = daemon_module.WifimimoDaemon(
        "wlp1s0", tmp_path / "state", history_dir
    )
    daemon._open_history(today)
    daemon._close_history()

    rotated = list(history_dir.glob(f"{today}.pre-*.csv"))
    assert len(rotated) == 1, "old-shape file should have been rotated aside"
    # New file at the canonical name, with the current header.
    new_path = history_dir / f"{today}.csv"
    with new_path.open(encoding="utf-8") as f:
        new_header = next(csv.reader(f))
    assert new_header == wifimimo_core.HISTORY_COLUMNS


def test_history_keeps_file_when_header_matches(tmp_path):
    daemon_module = _load_daemon()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    today = "2026-05-13"
    existing = history_dir / f"{today}.csv"
    existing.write_text(
        ",".join(wifimimo_core.HISTORY_COLUMNS) + "\nfake,row\n", encoding="utf-8"
    )

    daemon = daemon_module.WifimimoDaemon(
        "wlp1s0", tmp_path / "state", history_dir
    )
    daemon._open_history(today)
    daemon._close_history()

    rotated = list(history_dir.glob(f"{today}.pre-*.csv"))
    assert rotated == [], "matching schema must not rotate"
    # Original file is intact and not re-headered.
    with existing.open(encoding="utf-8") as f:
        first = next(csv.reader(f))
    assert first == wifimimo_core.HISTORY_COLUMNS
