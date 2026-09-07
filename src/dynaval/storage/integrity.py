"""Fail-closed recovery checks for original queues, passes, and decision history."""

import sqlite3

from dynaval.domain.errors import SessionError
from dynaval.domain.models import Decision, SessionView
from dynaval.domain.review import draft_is_edited
from dynaval.storage.records import decision_from_row, valid_hash


def verify_session(connection: sqlite3.Connection, view: SessionView) -> None:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SessionError("SQLite integrity check failed. Restore a session backup.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SessionError("The session contains inconsistent record references.")
    rows, columns = view.dataset.rows, view.dataset.columns
    fields = view.settings.validation_columns
    if (
        not rows
        or not columns
        or any(len(row) != len(columns) for row in rows)
        or max(fields + view.settings.reference_columns) >= len(columns)
        or fields != sorted(fields)
        or len(view.steps) != len(rows) * len(fields)
        or [step.step_index for step in view.steps] != list(range(len(view.steps)))
    ):
        raise SessionError("The source schema and saved review queue are inconsistent.")
    expected = {(row, column) for row in range(len(rows)) for column in fields}
    if {(step.source_row, step.src_col) for step in view.steps} != expected:
        raise SessionError("The review queue does not cover each selected field exactly once.")
    for start in range(0, len(view.steps), len(fields)):
        group = view.steps[start : start + len(fields)]
        if (
            len({step.source_row for step in group}) != 1
            or [step.src_col for step in group] != fields
        ):
            raise SessionError("The saved queue no longer groups selected fields by source row.")
    passes = connection.execute("SELECT * FROM review_passes ORDER BY pass_id").fetchall()
    if [row["pass_id"] for row in passes] != list(range(1, view.pass_id + 1)):
        raise SessionError("Saved review passes are inconsistent.")
    latest: dict[int, Decision] = {}
    for review_pass in passes:
        pass_id = review_pass["pass_id"]
        scheduled = connection.execute(
            "SELECT position,step_index FROM pass_steps WHERE pass_id=? ORDER BY position",
            (pass_id,),
        ).fetchall()
        indexes = [row["step_index"] for row in scheduled]
        expected_steps = (
            list(range(len(view.steps)))
            if pass_id == 1
            else [
                index
                for index in range(len(view.steps))
                if latest[index].status in {"rejected", "skipped"}
            ]
        )
        if indexes != expected_steps or [row["position"] for row in scheduled] != list(
            range(len(indexes))
        ):
            raise SessionError("A review pass does not match its original unresolved field order.")
        submitted = {
            row["step_index"]: decision_from_row(row)
            for row in connection.execute(
                "SELECT * FROM decision_history WHERE pass_id=?", (pass_id,)
            )
        }
        cursor = review_pass["cursor"]
        if not 0 <= cursor <= len(indexes) or set(submitted) != set(indexes[:cursor]):
            raise SessionError("The saved cursor and committed review history disagree.")
        if pass_id < view.pass_id and cursor != len(indexes):
            raise SessionError("A follow-up pass exists before earlier work was finished.")
        for decision in submitted.values():
            _validate_historical(view, decision)
        latest.update(submitted)
    if latest != view.decisions:
        raise SessionError("Latest decisions and their saved history disagree.")
    if view.draft is not None:
        current = view.current_step
        if current is None or (view.draft.pass_id, view.draft.step_index) != (
            view.pass_id,
            current.step_index,
        ):
            raise SessionError("The saved draft does not belong to the current field.")
        original = rows[current.source_row][current.src_col]
        if not view.settings.allow_corrections and draft_is_edited(
            original, view.draft.text, view.draft.edit_active
        ):
            raise SessionError("The saved draft conflicts with the correction settings.")
    if view.state == "active" and view.current_step is None:
        raise SessionError("An active session has no remaining field in its saved pass.")
    if view.state in {"completed", "needs_attention"} and (
        view.current_step is not None
        or (view.state == "completed") != (view.resolved == len(view.steps))
    ):
        raise SessionError("The saved completion state disagrees with the review outcomes.")


def _validate_historical(view: SessionView, decision: Decision) -> None:
    if decision.status == "skipped":
        if not view.settings.allow_skipping:
            raise SessionError("A saved skip conflicts with the session settings.")
        return
    if len(decision.reference_sha256) != len(view.settings.reference_columns) or any(
        not valid_hash(digest) for digest in decision.reference_sha256
    ):
        raise SessionError("A saved decision is missing its source-image hashes.")
    if decision.status == "corrected":
        step = view.steps[decision.step_index]
        original = view.dataset.rows[step.source_row][step.src_col]
        if not view.settings.allow_corrections or not draft_is_edited(
            original, decision.correction or "", True
        ):
            raise SessionError("A saved correction conflicts with the original value or settings.")
