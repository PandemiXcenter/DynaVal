"""Typed records at the SQLite boundary."""

import json
import sqlite3

from dynaval.domain.models import Decision


def decision_from_row(row: sqlite3.Row) -> Decision:
    return Decision(
        step_index=row["step_index"],
        pass_id=row["pass_id"],
        status=row["status"],
        correction=row["correction"],
        reviewed_at=row["reviewed_at"],
        reference_sha256=json.loads(row["reference_sha256"]),
    )


def valid_hash(value: str | None) -> bool:
    return (
        value is not None
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
