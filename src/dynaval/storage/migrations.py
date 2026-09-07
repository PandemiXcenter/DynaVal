"""Versioned schemas. New upgrades must be registered and backed up first."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from dynaval.domain.errors import SessionError
from dynaval.storage.database import transaction

SCHEMA_VERSION = 1
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

SCHEMA = """
CREATE TABLE session (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    metadata TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','paused','needs_attention','completed')),
    active_pass INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE source_columns (
    src_col INTEGER PRIMARY KEY CHECK(src_col >= 0),
    name TEXT NOT NULL UNIQUE CHECK(length(trim(name)) > 0)
);
CREATE TABLE source_rows (
    source_row INTEGER PRIMARY KEY CHECK(source_row >= 0),
    cells TEXT NOT NULL
);
CREATE TABLE review_steps (
    step_index INTEGER PRIMARY KEY CHECK(step_index >= 0),
    source_row INTEGER NOT NULL REFERENCES source_rows(source_row),
    src_col INTEGER NOT NULL REFERENCES source_columns(src_col),
    UNIQUE(source_row, src_col)
);
CREATE TABLE review_passes (
    pass_id INTEGER PRIMARY KEY CHECK(pass_id > 0),
    cursor INTEGER NOT NULL CHECK(cursor >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE pass_steps (
    pass_id INTEGER NOT NULL REFERENCES review_passes(pass_id),
    position INTEGER NOT NULL CHECK(position >= 0),
    step_index INTEGER NOT NULL REFERENCES review_steps(step_index),
    PRIMARY KEY(pass_id, position),
    UNIQUE(pass_id, step_index)
);
CREATE TABLE decision_history (
    pass_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed','corrected','rejected','skipped')),
    correction TEXT,
    reviewed_at TEXT NOT NULL,
    reference_sha256 TEXT NOT NULL,
    PRIMARY KEY(pass_id, step_index),
    FOREIGN KEY(pass_id, step_index) REFERENCES pass_steps(pass_id, step_index),
    CHECK((status = 'corrected' AND correction IS NOT NULL)
       OR (status != 'corrected' AND correction IS NULL))
);
CREATE TABLE decisions (
    step_index INTEGER PRIMARY KEY REFERENCES review_steps(step_index),
    pass_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed','corrected','rejected','skipped')),
    correction TEXT,
    reviewed_at TEXT NOT NULL,
    reference_sha256 TEXT NOT NULL,
    FOREIGN KEY(pass_id, step_index) REFERENCES decision_history(pass_id, step_index),
    CHECK((status = 'corrected' AND correction IS NOT NULL)
       OR (status != 'corrected' AND correction IS NULL))
);
CREATE TABLE drafts (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    step_index INTEGER NOT NULL,
    pass_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    edit_active INTEGER NOT NULL CHECK(edit_active IN (0,1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(pass_id, step_index) REFERENCES pass_steps(pass_id, step_index)
);
CREATE TABLE media (
    source_row INTEGER NOT NULL REFERENCES source_rows(source_row),
    reference_col INTEGER NOT NULL REFERENCES source_columns(src_col),
    record TEXT NOT NULL,
    PRIMARY KEY(source_row, reference_col)
);
CREATE TABLE mapping_changes (
    change_id INTEGER PRIMARY KEY,
    previous TEXT NOT NULL,
    replacement TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE exports (
    export_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    exported_at TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def migrate(connection: sqlite3.Connection, directory: Path) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 1 or version > SCHEMA_VERSION:
        raise SessionError(
            f"Unsupported session schema {version}. Open with a compatible DynaVal version."
        )
    if version == SCHEMA_VERSION:
        return
    if any(target not in MIGRATIONS for target in range(version + 1, SCHEMA_VERSION + 1)):
        raise SessionError("No supported migration exists for this session; keep its files intact.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = directory / f"session.schema-{version}.{stamp}.backup.sqlite3"
    destination = sqlite3.connect(backup)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    with transaction(connection):
        for target in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[target](connection)
            connection.execute(f"PRAGMA user_version = {target:d}")
