"""Single-writer session lifecycle, durable review, and exact continuation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

from dynaval import __version__
from dynaval.domain.errors import SessionError
from dynaval.domain.models import (
    Cell,
    Dataset,
    Draft,
    MediaRecord,
    Outcome,
    ParserOptions,
    PathMapping,
    ReviewStep,
    SessionSettings,
    SessionSummary,
    SessionView,
)
from dynaval.domain.queue import create_queue
from dynaval.domain.review import draft_is_edited
from dynaval.storage.database import connect, transaction
from dynaval.storage.integrity import verify_session
from dynaval.storage.locking import SessionLock
from dynaval.storage.migrations import initialize, migrate
from dynaval.storage.records import decision_from_row, valid_hash

T = TypeVar("T")
QUEUE_VERSION = 1
NORMALIZATION_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _normalized(dataset: Dataset) -> dict[str, object]:
    return {
        "columns": dataset.columns,
        "rows": [[cell.model_dump() for cell in row] for row in dataset.rows],
    }


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT metadata FROM session WHERE singleton=1").fetchone()
    if row is None:
        raise SessionError("Session metadata is missing. Restore a backup of this session.")
    result = json.loads(row[0])
    if not isinstance(result, dict):
        raise SessionError("Session metadata is damaged. Restore a backup of this session.")
    return result


class SessionManager:
    """Create immutable snapshots and discover independent saved sessions."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        for directory in self.root.iterdir():
            try:
                if str(UUID(directory.name)) != directory.name or not directory.is_dir():
                    continue
            except ValueError:
                continue
            try:
                connection = connect(directory / "session.sqlite3", readonly=True)
                try:
                    metadata = _metadata(connection)
                    session = connection.execute("SELECT * FROM session").fetchone()
                    settings = SessionSettings.model_validate(metadata["settings"])
                    columns = dict(connection.execute("SELECT src_col,name FROM source_columns"))
                    total = connection.execute("SELECT count(*) FROM review_steps").fetchone()[0]
                    resolved = connection.execute(
                        "SELECT count(*) FROM decisions WHERE status IN ('confirmed','corrected')"
                    ).fetchone()[0]
                    summaries.append(
                        SessionSummary(
                            session_id=directory.name,
                            name=metadata["name"],
                            sha256=metadata["sha256"],
                            state=session["state"],
                            seed=settings.seed,
                            total=total,
                            resolved=resolved,
                            updated_at=session["updated_at"],
                            columns=[columns[index] for index in settings.validation_columns],
                        )
                    )
                finally:
                    connection.close()
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, SessionError):
                summaries.append(
                    SessionSummary(
                        session_id=directory.name,
                        name="Unreadable session",
                        sha256="",
                        state="paused",
                        seed=0,
                        total=0,
                        resolved=0,
                        updated_at="",
                        columns=[],
                        error="Session files are damaged or unsupported. Keep them for recovery.",
                    )
                )
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)

    def matching(self, sha256: str) -> list[SessionSummary]:
        return [summary for summary in self.list_sessions() if summary.sha256 == sha256]

    def open(self, session_id: str) -> SessionStore:
        try:
            if str(UUID(session_id)) != session_id:
                raise ValueError
        except ValueError as exc:
            raise SessionError("Invalid saved-session identifier.") from exc
        directory = self.root / session_id
        if not directory.is_dir() or directory.is_symlink():
            raise SessionError("This saved session is missing. Restore its complete directory.")
        database = directory / "session.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise SessionError("The session database is missing or invalid. Restore a backup.")
        return SessionStore(directory)

    def create(self, dataset: Dataset, settings: SessionSettings) -> SessionStore:
        if not dataset.source_path:
            raise SessionError("Stage the original source file before starting a session.")
        if not dataset.columns or not dataset.rows:
            raise SessionError("The dataset must contain columns and rows.")
        if (
            len(set(dataset.columns)) != len(dataset.columns)
            or any(not name.strip() for name in dataset.columns)
            or any(len(row) != len(dataset.columns) for row in dataset.rows)
        ):
            raise SessionError("The normalized dataset has an invalid schema or record width.")
        if max(settings.reference_columns + settings.validation_columns) >= len(dataset.columns):
            raise SessionError("Selected columns are outside the imported schema.")
        settings = settings.model_copy(
            update={
                "reference_columns": sorted(settings.reference_columns),
                "validation_columns": sorted(settings.validation_columns),
            }
        )
        session_id = str(uuid4())
        staging = self.root / f".staging-{session_id}"
        directory = self.root / session_id
        staging.mkdir()
        try:
            suffix = Path(dataset.name).suffix.lower()
            source_name = "source" + (
                suffix if suffix in {".csv", ".tsv", ".jsonl", ".ndjson", ".json"} else ".data"
            )
            source_path = staging / source_name
            with Path(dataset.source_path).open("rb") as source, source_path.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if _file_digest(source_path) != dataset.sha256:
                raise SessionError(
                    "The input changed after import. Import it again before starting."
                )
            steps = create_queue(len(dataset.rows), settings.validation_columns, settings.seed)
            metadata = {
                "session_id": session_id,
                "name": dataset.name,
                "sha256": dataset.sha256,
                "parser": dataset.parser.model_dump(),
                "settings": settings.model_dump(),
                "source_name": source_name,
                "original_source_path": str(Path(dataset.source_path).resolve()),
                "normalization_version": NORMALIZATION_VERSION,
                "queue_version": QUEUE_VERSION,
                "app_version": __version__,
                "python_version": platform.python_version(),
                "normalized_sha256": _digest(_normalized(dataset)),
                "queue_sha256": _digest([step.model_dump() for step in steps]),
            }
            connection = connect(staging / "session.sqlite3")
            try:
                initialize(connection)
                stamp = _now()
                with transaction(connection):
                    connection.execute(
                        "INSERT INTO session VALUES(1,?,'active',1,?,?)",
                        (_json(metadata), stamp, stamp),
                    )
                    connection.executemany(
                        "INSERT INTO source_columns VALUES(?,?)", enumerate(dataset.columns)
                    )
                    connection.executemany(
                        "INSERT INTO source_rows VALUES(?,?)",
                        (
                            (index, _json([cell.model_dump() for cell in row]))
                            for index, row in enumerate(dataset.rows)
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO review_steps VALUES(?,?,?)",
                        ((step.step_index, step.source_row, step.src_col) for step in steps),
                    )
                    connection.execute("INSERT INTO review_passes VALUES(1,0,?)", (stamp,))
                    connection.executemany(
                        "INSERT INTO pass_steps VALUES(1,?,?)",
                        ((step.step_index, step.step_index) for step in steps),
                    )
            finally:
                connection.close()
            _sync_directory(staging)
            staging.rename(directory)
            _sync_directory(self.root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        store = self.open(session_id)
        try:
            store.resume()
        except BaseException:
            store.close()
            raise
        return store


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SessionStore:
    """All connection access runs on one dedicated worker, including reads."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.session_id = directory.name
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dynaval-session")
        self._lock = SessionLock(self.directory / "writer.lock")
        self._closed = False
        try:
            self._call(self._open)
        except BaseException:
            self._executor.shutdown(wait=True)
            self._closed = True
            raise

    def _call(self, action: Callable[[], T]) -> T:
        if self._closed:
            raise SessionError("This session is closed. Reopen it from Continue Dataset.")
        try:
            return self._executor.submit(action).result()
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise SessionError(
                "The session could not be read or saved. Check available disk space "
                "and session files; retry without deleting them."
            ) from exc

    def _open(self) -> None:
        self._lock.acquire()
        try:
            self._connection = connect(self.directory / "session.sqlite3")
            migrate(self._connection, self.directory)
            self._load_source()
            verify_session(self._connection, self._read_view())
            # Persisted hashes remain evidence, but opaque image registrations
            # belong to the current process and must be loaded again on resume.
            self._ready_rows: set[int] = set()
            # An active session on disk has no surviving owner and was interrupted.
            if self._read_view().state == "active":
                with transaction(self._connection):
                    self._set_state("paused")
        except BaseException:
            if hasattr(self, "_connection"):
                self._connection.close()
            self._lock.release()
            raise

    def _load_source(self) -> None:
        metadata = _metadata(self._connection)
        source_name = metadata["source_name"]
        if Path(source_name).name != source_name or not source_name.startswith("source."):
            raise SessionError("The saved source-file location is invalid.")
        if metadata["session_id"] != self.session_id:
            raise SessionError("Saved-session identity does not match its directory.")
        if (
            metadata["normalization_version"] != NORMALIZATION_VERSION
            or metadata["queue_version"] != QUEUE_VERSION
        ):
            raise SessionError("This session uses an unsupported data or queue version.")
        source_path = self.directory / source_name
        if source_path.is_symlink() or _file_digest(source_path) != metadata["sha256"]:
            raise SessionError("The saved source snapshot is damaged. Restore the session backup.")
        column_rows = self._connection.execute(
            "SELECT * FROM source_columns ORDER BY src_col"
        ).fetchall()
        source_rows = self._connection.execute(
            "SELECT * FROM source_rows ORDER BY source_row"
        ).fetchall()
        if [row["src_col"] for row in column_rows] != list(range(len(column_rows))) or [
            row["source_row"] for row in source_rows
        ] != list(range(len(source_rows))):
            raise SessionError("Original row or column indexes are inconsistent.")
        self._dataset = Dataset(
            name=metadata["name"],
            sha256=metadata["sha256"],
            columns=[row["name"] for row in column_rows],
            rows=[
                [Cell.model_validate(cell) for cell in json.loads(row["cells"])]
                for row in source_rows
            ],
            parser=ParserOptions.model_validate(metadata["parser"]),
            source_path=str(source_path),
        )
        if _digest(_normalized(self._dataset)) != metadata["normalized_sha256"]:
            raise SessionError("The normalized original data is damaged. Restore a session backup.")
        self._settings = SessionSettings.model_validate(metadata["settings"])
        self._steps = [
            ReviewStep(**dict(row))
            for row in self._connection.execute("SELECT * FROM review_steps ORDER BY step_index")
        ]
        if _digest([step.model_dump() for step in self._steps]) != metadata["queue_sha256"]:
            raise SessionError("The saved review order is damaged. Restore a session backup.")
        self._original_source_path = metadata.get("original_source_path")

    def _read_view(self) -> SessionView:
        session = self._connection.execute("SELECT * FROM session WHERE singleton=1").fetchone()
        pass_id = session["active_pass"]
        review_pass = self._connection.execute(
            "SELECT * FROM review_passes WHERE pass_id=?", (pass_id,)
        ).fetchone()
        draft = self._connection.execute("SELECT * FROM drafts WHERE singleton=1").fetchone()
        return SessionView(
            session_id=self.session_id,
            dataset=self._dataset,
            settings=self._settings,
            state=session["state"],
            steps=self._steps,
            decisions={
                row["step_index"]: decision_from_row(row)
                for row in self._connection.execute("SELECT * FROM decisions")
            },
            pass_id=pass_id,
            pass_steps=[
                row[0]
                for row in self._connection.execute(
                    "SELECT step_index FROM pass_steps WHERE pass_id=? ORDER BY position",
                    (pass_id,),
                )
            ],
            cursor=review_pass["cursor"],
            draft=None
            if draft is None
            else Draft(
                **{
                    key: draft[key]
                    for key in ("step_index", "pass_id", "text", "edit_active", "updated_at")
                }
            ),
            created_at=session["created_at"],
            updated_at=session["updated_at"],
            original_source_path=self._original_source_path,
        )

    def view(self) -> SessionView:
        return self._call(self._read_view)

    def _set_state(self, state: str) -> None:
        self._connection.execute(
            "UPDATE session SET state=?,updated_at=? WHERE singleton=1", (state, _now())
        )

    def _current(self, step_index: int, pass_id: int) -> SessionView:
        view = self._read_view()
        if view.state != "active":
            raise SessionError("Resume this session before reviewing a field.")
        if view.current_step is None or (view.pass_id, view.current_step.step_index) != (
            pass_id,
            step_index,
        ):
            raise SessionError(
                "This field is no longer current. Refresh the saved session before retrying."
            )
        return view

    def mark_media(self, source_row: int, records: list[MediaRecord]) -> None:
        self._call(lambda: self._mark_media(source_row, records))

    def _mark_media(self, source_row: int, records: list[MediaRecord]) -> None:
        if not 0 <= source_row < len(self._dataset.rows):
            raise SessionError("The reference images belong to an unknown source row.")
        columns = [record.reference_col for record in records]
        if len(set(columns)) != len(columns) or not set(columns) <= set(
            self._settings.reference_columns
        ):
            raise SessionError("Reference images do not match the configured image columns.")
        for record in records:
            if record.reference != self._dataset.rows[source_row][record.reference_col].text:
                raise SessionError("The loaded reference does not match this source record.")
        with transaction(self._connection):
            self._connection.execute("DELETE FROM media WHERE source_row=?", (source_row,))
            self._connection.executemany(
                "INSERT INTO media VALUES(?,?,?)",
                (
                    (source_row, record.reference_col, record.model_dump_json())
                    for record in records
                ),
            )
        self._ready_rows.add(source_row)

    def _references(self, source_row: int) -> list[str | None]:
        if source_row not in self._ready_rows:
            raise SessionError("Load every reference image again before continuing this session.")
        records = {
            row["reference_col"]: MediaRecord.model_validate_json(row["record"])
            for row in self._connection.execute(
                "SELECT * FROM media WHERE source_row=?", (source_row,)
            )
        }
        hashes: list[str | None] = []
        for column in self._settings.reference_columns:
            record = records.get(column)
            if record is None or record.error or record.changed or not valid_hash(record.sha256):
                raise SessionError(
                    "Load every reference image before deciding. "
                    "Retry, pause to repair paths, or skip if allowed."
                )
            hashes.append(record.sha256)
        return hashes

    def save_draft(self, draft: Draft) -> None:
        self._call(lambda: self._save_draft(draft))

    def _save_draft(self, draft: Draft) -> None:
        view = self._current(draft.step_index, draft.pass_id)
        step = self._steps[draft.step_index]
        original = self._dataset.rows[step.source_row][step.src_col]
        if not view.settings.allow_corrections and draft_is_edited(
            original, draft.text, draft.edit_active
        ):
            raise SessionError("Corrections are disabled for this session.")
        with transaction(self._connection):
            self._connection.execute(
                "INSERT OR REPLACE INTO drafts VALUES(1,?,?,?,?,?)",
                (draft.step_index, draft.pass_id, draft.text, int(draft.edit_active), _now()),
            )
            self._connection.execute("UPDATE session SET updated_at=?", (_now(),))

    def submit(
        self,
        step_index: int,
        pass_id: int,
        status: Outcome,
        correction: str | None = None,
        edit_active: bool = False,
    ) -> SessionView:
        return self._call(
            lambda: self._submit(step_index, pass_id, status, correction, edit_active)
        )

    def _submit(
        self,
        step_index: int,
        pass_id: int,
        status: Outcome,
        correction: str | None,
        edit_active: bool,
    ) -> SessionView:
        existing = self._connection.execute(
            "SELECT * FROM decision_history WHERE pass_id=? AND step_index=?", (pass_id, step_index)
        ).fetchone()
        if existing is not None:
            if (existing["status"], existing["correction"]) != (status, correction):
                raise SessionError("This field was already saved with a different decision.")
            return self._read_view()
        view = self._current(step_index, pass_id)
        step = self._steps[step_index]
        original = self._dataset.rows[step.source_row][step.src_col]
        if status not in {"confirmed", "corrected", "rejected", "skipped"}:
            raise SessionError("Unknown review outcome.")
        if status == "skipped" and not self._settings.allow_skipping:
            raise SessionError("Skipping is disabled for this session.")
        if status == "corrected":
            if not self._settings.allow_corrections:
                raise SessionError("Corrections are disabled for this session.")
            activated = edit_active or bool(view.draft and view.draft.edit_active)
            if correction is None or not draft_is_edited(original, correction, activated):
                raise SessionError(
                    "Enter a changed value before saving a correction. "
                    "Activate editing for an empty null/missing replacement."
                )
            if view.draft is not None and view.draft.text != correction:
                raise SessionError(
                    "The saved draft differs from this correction. "
                    "Save the current draft and retry."
                )
        else:
            if correction is not None:
                raise SessionError("Only Save Correction accepts replacement text.")
            if view.draft is not None and draft_is_edited(
                original, view.draft.text, view.draft.edit_active
            ):
                raise SessionError(
                    "Save or revert the edited draft before choosing another action."
                )
        hashes = [] if status == "skipped" else self._references(step.source_row)
        stamp = _now()
        with transaction(self._connection):
            values = (pass_id, step_index, status, correction, stamp, _json(hashes))
            self._connection.execute("INSERT INTO decision_history VALUES(?,?,?,?,?,?)", values)
            self._connection.execute(
                "INSERT OR REPLACE INTO decisions(step_index,pass_id,status,correction,"
                "reviewed_at,reference_sha256) VALUES(?,?,?,?,?,?)",
                (step_index, pass_id, status, correction, stamp, _json(hashes)),
            )
            self._connection.execute("DELETE FROM drafts WHERE singleton=1")
            self._connection.execute(
                "UPDATE review_passes SET cursor=cursor+1 WHERE pass_id=?", (pass_id,)
            )
            self._after_decision()
            updated = self._read_view()
            state = "active"
            if updated.current_step is None:
                state = "completed" if updated.resolved == len(updated.steps) else "needs_attention"
            self._set_state(state)
        return self._read_view()

    def _after_decision(self) -> None:
        """Fault-injection seam used to verify rollback before cursor publication."""

    def pause(self) -> SessionView:
        return self._call(self._pause)

    def _pause(self) -> SessionView:
        with transaction(self._connection):
            self._set_state("paused")
        return self._read_view()

    def resume(self) -> SessionView:
        return self._call(self._resume)

    def _resume(self) -> SessionView:
        view = self._read_view()
        state = (
            "active"
            if view.current_step
            else ("completed" if view.resolved == len(view.steps) else "needs_attention")
        )
        with transaction(self._connection):
            self._set_state(state)
        return self._read_view()

    def follow_up(self) -> SessionView:
        return self._call(self._follow_up)

    def _follow_up(self) -> SessionView:
        view = self._read_view()
        if view.current_step is not None:
            raise SessionError("Finish the current pass before continuing unresolved fields.")
        unresolved = [
            step.step_index
            for step in view.steps
            if view.decisions[step.step_index].status in {"rejected", "skipped"}
        ]
        if not unresolved:
            raise SessionError("All selected fields are already resolved.")
        with transaction(self._connection):
            pass_id = view.pass_id + 1
            self._connection.execute("INSERT INTO review_passes VALUES(?,0,?)", (pass_id, _now()))
            self._connection.executemany(
                "INSERT INTO pass_steps VALUES(?,?,?)",
                ((pass_id, position, index) for position, index in enumerate(unresolved)),
            )
            self._connection.execute("UPDATE session SET active_pass=?", (pass_id,))
            self._set_state("active")
        return self._read_view()

    def repair_paths(
        self, reference_base: str | None, path_mappings: list[PathMapping]
    ) -> SessionView:
        return self._call(lambda: self._repair_paths(reference_base, path_mappings))

    def _repair_paths(
        self, reference_base: str | None, path_mappings: list[PathMapping]
    ) -> SessionView:
        if self._read_view().state != "paused":
            raise SessionError("Pause validation before repairing image paths.")
        replacement = SessionSettings.model_validate(
            {
                **self._settings.model_dump(),
                "reference_base": reference_base,
                "path_mappings": [mapping.model_dump() for mapping in path_mappings],
            }
        )
        metadata = _metadata(self._connection)
        metadata["settings"] = replacement.model_dump()
        with transaction(self._connection):
            self._connection.execute(
                "INSERT INTO mapping_changes(previous,replacement,changed_at) VALUES(?,?,?)",
                (self._settings.model_dump_json(), replacement.model_dump_json(), _now()),
            )
            self._connection.execute(
                "UPDATE session SET metadata=?,updated_at=?", (_json(metadata), _now())
            )
            self._connection.execute("DELETE FROM media")
        self._settings = replacement
        self._ready_rows.clear()
        return self._read_view()

    def record_export(self, path: Path) -> None:
        def record() -> None:
            with transaction(self._connection):
                self._connection.execute(
                    "INSERT INTO exports(path,exported_at) VALUES(?,?)",
                    (str(path.resolve()), _now()),
                )

        self._call(record)

    def close(self) -> None:
        if self._closed:
            return
        try:

            def release() -> None:
                try:
                    self._connection.close()
                finally:
                    self._lock.release()

            self._call(release)
        finally:
            self._closed = True
            self._executor.shutdown(wait=True)

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
