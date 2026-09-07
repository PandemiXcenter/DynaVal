"""Durability contracts use synthetic data and never require source images/network."""

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from dynaval.domain.errors import SessionError
from dynaval.domain.models import (
    Cell,
    Dataset,
    Draft,
    MediaRecord,
    ParserOptions,
    PathMapping,
    SessionSettings,
)
from dynaval.services.sessions import SessionManager, SessionStore
from dynaval.storage import migrations
from dynaval.storage.database import connect


@contextmanager
def database_connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection


@pytest.fixture
def dataset(tmp_path: Path) -> Dataset:
    source = tmp_path / "sample.csv"
    source.write_text(
        "image$value$other\npicture.png$001$NA\npicture.png$FALSE$ \n", encoding="utf-8"
    )
    return Dataset(
        name=source.name,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        columns=["image", "value", "other"],
        rows=[
            [Cell(text="picture.png"), Cell(text="001"), Cell(text="NA")],
            [Cell(text="picture.png"), Cell(text="FALSE"), Cell(text=" ")],
        ],
        parser=ParserOptions(format="csv", delimiter="$"),
        source_path=str(source),
    )


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "sessions")


def settings(**changes: object) -> SessionSettings:
    return SessionSettings.model_validate(
        {"reference_columns": [0], "validation_columns": [1, 2], "seed": 2**64 - 1, **changes}
    )


def ready(store: SessionStore) -> None:
    view = store.view()
    assert view.current_step is not None
    source_row = view.current_step.source_row
    store.mark_media(
        source_row,
        [
            MediaRecord(
                reference_col=column,
                reference=view.dataset.rows[source_row][column].text,
                sha256="a" * 64,
                media_id="synthetic",
            )
            for column in view.settings.reference_columns
        ],
    )


def decide(store: SessionStore, status: str, correction: str | None = None) -> None:
    ready(store)
    view = store.view()
    assert view.current_step is not None
    store.submit(view.current_step.step_index, view.pass_id, status, correction)  # type: ignore[arg-type]


def test_snapshot_seed_full_order_and_matching(manager: SessionManager, dataset: Dataset) -> None:
    with manager.create(dataset, settings(validation_columns=[2, 1])) as store:
        view = store.view()
        assert view.state == "active"
        assert view.settings.seed == 2**64 - 1
        assert view.settings.validation_columns == [1, 2]
        assert view.dataset.source_path != dataset.source_path
        assert Path(view.dataset.source_path).read_bytes() == Path(dataset.source_path).read_bytes()
        assert view.original_source_path == dataset.source_path
        assert len(view.steps) == 4
        assert {(step.source_row, step.src_col) for step in view.steps} == {
            (0, 1),
            (0, 2),
            (1, 1),
            (1, 2),
        }
        assert manager.matching(dataset.sha256)[0].session_id == store.session_id
        assert manager.matching("unknown") == []
        saved = view.steps
        session_id = store.session_id
    Path(dataset.source_path).unlink()
    with manager.open(session_id) as store:
        assert store.view().state == "paused"
        assert store.resume().steps == saved
        assert store.view().settings.seed == 2**64 - 1


def test_atomic_creation_discards_changed_source(manager: SessionManager, dataset: Dataset) -> None:
    Path(dataset.source_path).write_text("changed")
    with pytest.raises(SessionError, match="changed"):
        manager.create(dataset, settings())
    assert list(manager.root.iterdir()) == []


def test_empty_and_bad_schema_rejected(manager: SessionManager, dataset: Dataset) -> None:
    with pytest.raises(SessionError, match="columns and rows"):
        manager.create(dataset.model_copy(update={"rows": []}), settings())
    with pytest.raises(SessionError, match="schema"):
        manager.create(
            dataset.model_copy(update={"columns": ["image", "same", "same"]}), settings()
        )
    with pytest.raises(SessionError, match="outside"):
        manager.create(dataset, settings(validation_columns=[5]))


@pytest.mark.parametrize("corrections", [True, False])
@pytest.mark.parametrize("skipping", [True, False])
def test_service_settings_and_missing_media(
    manager: SessionManager, dataset: Dataset, corrections: bool, skipping: bool
) -> None:
    with manager.create(
        dataset, settings(allow_corrections=corrections, allow_skipping=skipping)
    ) as store:
        for status in ("confirmed", "rejected"):
            with pytest.raises(SessionError, match="reference"):
                store.submit(0, 1, status)  # type: ignore[arg-type]
        if skipping:
            assert store.submit(0, 1, "skipped").decisions[0].valid is None
        else:
            with pytest.raises(SessionError, match="Skipping"):
                store.submit(0, 1, "skipped")
        ready(store)
        step = store.view().current_step
        assert step is not None
        if corrections:
            assert (
                store.submit(step.step_index, 1, "corrected", "replacement")
                .decisions[step.step_index]
                .valid
                is False
            )
        else:
            with pytest.raises(SessionError, match="Corrections"):
                store.submit(step.step_index, 1, "corrected", "replacement")
            assert (
                store.submit(step.step_index, 1, "rejected").decisions[step.step_index].valid
                is False
            )


def test_draft_cannot_be_discarded_empty_correction_survives(
    manager: SessionManager, dataset: Dataset
) -> None:
    with manager.create(dataset, settings(allow_skipping=True)) as store:
        ready(store)
        store.save_draft(Draft(step_index=0, pass_id=1, text="", edit_active=True))
        for status in ("confirmed", "rejected", "skipped"):
            with pytest.raises(SessionError, match="draft"):
                store.submit(0, 1, status)  # type: ignore[arg-type]
        with pytest.raises(SessionError, match="differs"):
            store.submit(0, 1, "corrected", "different")
        store.pause()
        session_id = store.session_id
    with manager.open(session_id) as store:
        assert store.view().draft.text == ""
        store.resume()
        with pytest.raises(SessionError, match="again"):
            store.submit(0, 1, "corrected", "", edit_active=True)
        ready(store)
        view = store.submit(0, 1, "corrected", "", edit_active=True)
        assert view.decisions[0].correction == ""
        assert view.decisions[0].valid is False
        assert view.draft is None


@pytest.mark.parametrize("kind,text", [("null", "null"), ("missing", "")])
def test_null_missing_require_explicit_empty_activation(
    manager: SessionManager, dataset: Dataset, kind: str, text: str
) -> None:
    rows = [[row[0], Cell(text=text, kind=kind), row[2]] for row in dataset.rows]  # type: ignore[arg-type]
    with manager.create(
        dataset.model_copy(update={"rows": rows}), settings(validation_columns=[1])
    ) as store:
        ready(store)
        with pytest.raises(SessionError, match="Activate"):
            store.submit(0, 1, "corrected", "")
        assert store.submit(0, 1, "corrected", "", edit_active=True).decisions[0].correction == ""


def test_unchanged_and_reverted_drafts(manager: SessionManager, dataset: Dataset) -> None:
    with manager.create(dataset, settings()) as store:
        view = store.view()
        step = view.current_step
        original = view.dataset.rows[step.source_row][step.src_col].text
        ready(store)
        with pytest.raises(SessionError, match="changed value"):
            store.submit(0, 1, "corrected", original, edit_active=True)
        store.save_draft(Draft(step_index=0, pass_id=1, text=original, edit_active=True))
        assert store.submit(0, 1, "confirmed").decisions[0].valid is True


def test_duplicate_concurrent_retry_and_stale_submission(
    manager: SessionManager, dataset: Dataset
) -> None:
    with manager.create(dataset, settings()) as store:
        ready(store)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(store.submit, 0, 1, "confirmed") for _ in range(2)]
            assert all(future.result().cursor == 1 for future in futures)
        with pytest.raises(SessionError, match="different decision"):
            store.submit(0, 1, "rejected")
        with pytest.raises(SessionError, match="no longer current"):
            store.submit(2, 1, "confirmed")
        with database_connection(store.directory / "session.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM decision_history").fetchone()[0] == 1


def test_failed_commit_rolls_back_decision_draft_and_cursor(
    manager: SessionManager, dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    with manager.create(dataset, settings()) as store:
        ready(store)
        store.save_draft(Draft(step_index=0, pass_id=1, text="replacement", edit_active=True))

        def fail() -> None:
            raise sqlite3.OperationalError("synthetic disk failure")

        monkeypatch.setattr(store, "_after_decision", fail)
        with pytest.raises(SessionError, match="could not be read or saved"):
            store.submit(0, 1, "corrected", "replacement")
        view = store.view()
        assert view.cursor == 0 and view.decisions == {}
        assert view.draft.text == "replacement"
        with database_connection(store.directory / "session.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM decision_history").fetchone()[0] == 0
        monkeypatch.setattr(store, "_after_decision", lambda: None)
        assert store.submit(0, 1, "corrected", "replacement").cursor == 1


def test_followups_keep_seed_queue_history_and_resume_cursor(
    manager: SessionManager, dataset: Dataset
) -> None:
    with manager.create(dataset, settings(allow_skipping=True)) as store:
        first = store.view()
        with pytest.raises(SessionError, match="Finish"):
            store.follow_up()
        for status in ("rejected", "confirmed", "skipped", "rejected"):
            decide(store, status)
        assert store.view().state == "needs_attention"
        follow = store.follow_up()
        assert follow.pass_steps == [0, 2, 3]
        decide(store, "skipped")
        store.pause()
        session_id = store.session_id
    with manager.open(session_id) as store:
        view = store.resume()
        assert view.cursor == 1 and view.current_step.step_index == 2
        assert view.steps == first.steps and view.settings.seed == first.settings.seed
        decide(store, "corrected", "fixed")
        decide(store, "confirmed")
        assert store.view().state == "needs_attention"
        assert store.follow_up().pass_steps == [0]
        decide(store, "corrected", "fixed again")
        view = store.view()
        assert view.state == "completed" and view.resolved == 4
        assert len(view.decisions) == 4
        assert view.counts == {
            "confirmed": 2,
            "corrected": 2,
            "rejected": 0,
            "skipped": 0,
            "pending": 0,
        }
        with pytest.raises(SessionError, match="already resolved"):
            store.follow_up()
        with database_connection(store.directory / "session.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM decision_history").fetchone()[0] == 8
    with manager.open(session_id) as store:
        assert store.view().state == "completed"


def test_path_repair_requires_pause_and_invalidates_ready_images(
    manager: SessionManager, dataset: Dataset
) -> None:
    reference_base = str(manager.root.parent / "new-base")
    with manager.create(dataset, settings()) as store:
        ready(store)
        with pytest.raises(SessionError, match="Pause"):
            store.repair_paths(reference_base, [])
        store.pause()
        mapping = PathMapping(
            source=r"\\server\share", target=str(manager.root.parent / "mounted-share")
        )
        updated = store.repair_paths(reference_base, [mapping])
        assert updated.settings.path_mappings == [mapping]
        store.resume()
        with pytest.raises(SessionError, match="reference"):
            store.submit(0, 1, "confirmed")
        store.record_export(store.directory.parent / "export-bundle")
        with database_connection(store.directory / "session.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM mapping_changes").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM exports").fetchone()[0] == 1


@pytest.mark.parametrize(
    "record",
    [
        MediaRecord(reference_col=0, reference="picture.png", error="unavailable"),
        MediaRecord(reference_col=0, reference="picture.png", sha256="a" * 64, changed=True),
        MediaRecord(reference_col=0, reference="picture.png", sha256="not-a-hash"),
    ],
)
def test_image_failures_never_become_verdicts(
    manager: SessionManager, dataset: Dataset, record: MediaRecord
) -> None:
    with manager.create(dataset, settings()) as store:
        store.mark_media(store.view().current_step.source_row, [record])
        with pytest.raises(SessionError, match="reference"):
            store.submit(0, 1, "confirmed")
        assert store.view().decisions == {} and store.view().cursor == 0


def test_source_and_database_corruption_is_not_silently_restarted(
    manager: SessionManager, dataset: Dataset
) -> None:
    with manager.create(dataset, settings()) as store:
        session_id = store.session_id
        snapshot = Path(store.view().dataset.source_path)
    snapshot.write_text("corruption")
    with pytest.raises(SessionError, match="snapshot is damaged"):
        manager.open(session_id)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE review_passes SET cursor=1",
        "UPDATE source_rows SET cells='[]' WHERE source_row=0",
        "UPDATE review_steps SET step_index=10 WHERE step_index=0",
        "PRAGMA user_version=99",
        "UPDATE session SET state='completed'",
        "UPDATE session SET metadata='not json'",
    ],
)
def test_inconsistent_database_refuses_resume(
    manager: SessionManager, dataset: Dataset, sql: str
) -> None:
    with manager.create(dataset, settings()) as store:
        session_id, path = store.session_id, store.directory / "session.sqlite3"
    with database_connection(path) as connection:
        connection.execute(sql)
    with pytest.raises(SessionError):
        manager.open(session_id)


def test_process_interruption_keeps_committed_decisions_and_draft(
    manager: SessionManager, dataset: Dataset
) -> None:
    with manager.create(dataset, settings()) as store:
        session_id = store.session_id
    code = """
import os, sys
from pathlib import Path
from dynaval.services.sessions import SessionManager
from dynaval.domain.models import MediaRecord, Draft
store = SessionManager(Path(sys.argv[1])).open(sys.argv[2])
view = store.resume()
row = view.current_step.source_row
store.mark_media(row, [MediaRecord(reference_col=0, reference='picture.png', sha256='a'*64)])
view = store.submit(0, 1, 'confirmed')
store.save_draft(Draft(step_index=1, pass_id=1, text='saved draft', edit_active=True))
os._exit(0)
"""
    result = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", code, str(manager.root), session_id],
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(manager.root.parent / "uv-cache")),
        },
    )
    assert result.returncode == 0, result.stderr.decode()
    with manager.open(session_id) as store:
        view = store.view()
        assert view.state == "paused" and view.cursor == 1
        assert view.decisions[0].status == "confirmed"
        assert view.draft.text == "saved draft"
        assert view.settings.seed == 2**64 - 1


def test_migration_backup_and_transaction_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(tmp_path / "session.sqlite3")
    try:
        migrations.initialize(connection)

        def failed_upgrade(database: sqlite3.Connection) -> None:
            database.execute("CREATE TABLE synthetic_upgrade(value TEXT)")
            raise RuntimeError("migration failed")

        monkeypatch.setattr(migrations, "SCHEMA_VERSION", 2)
        monkeypatch.setattr(migrations, "MIGRATIONS", {2: failed_upgrade})
        with pytest.raises(RuntimeError, match="migration failed"):
            migrations.migrate(connection, tmp_path)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='synthetic_upgrade'"
            ).fetchone()
            is None
        )
        backup = list(tmp_path.glob("*.backup.sqlite3"))
        assert len(backup) == 1
        with database_connection(backup[0]) as saved:
            assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()


def test_discovery_ignores_unpublished_staging_reports_corruption(manager: SessionManager) -> None:
    (manager.root / ".staging-interrupted").mkdir()
    bad = manager.root / "11111111-1111-1111-1111-111111111111"
    bad.mkdir()
    (bad / "session.sqlite3").write_text("not sqlite")
    summaries = manager.list_sessions()
    assert len(summaries) == 1 and summaries[0].error
    with pytest.raises(SessionError, match="identifier"):
        manager.open("../outside")


def test_multiple_reference_columns_all_required(manager: SessionManager, dataset: Dataset) -> None:
    with manager.create(
        dataset, settings(reference_columns=[2, 0], validation_columns=[1])
    ) as store:
        view = store.view()
        row = view.current_step.source_row
        store.mark_media(
            row, [MediaRecord(reference_col=0, reference="picture.png", sha256="a" * 64)]
        )
        with pytest.raises(SessionError, match="every reference"):
            store.submit(0, 1, "confirmed")
        with pytest.raises(SessionError, match="configured"):
            store.mark_media(row, [MediaRecord(reference_col=1, reference="not an image")])
        with pytest.raises(SessionError, match="does not match"):
            store.mark_media(row, [MediaRecord(reference_col=0, reference="wrong.png")])
        ready(store)
        assert store.submit(0, 1, "confirmed").decisions[0].reference_sha256 == ["a" * 64, "a" * 64]


def test_paused_and_closed_store_reject_late_mutations(
    manager: SessionManager, dataset: Dataset
) -> None:
    store = manager.create(dataset, settings())
    store.pause()
    with pytest.raises(SessionError, match="Resume"):
        store.save_draft(Draft(step_index=0, pass_id=1, text="late edit"))
    with pytest.raises(SessionError, match="Resume"):
        store.submit(0, 1, "confirmed")
    store.close()
    store.close()
    with pytest.raises(SessionError, match="closed"):
        store.view()


@pytest.mark.parametrize(
    "mutation",
    [
        "latest",
        "reference_hash",
        "nonprefix",
        "future_queue",
        "source_location",
        "identity",
        "draft",
    ],
)
def test_corrupt_history_metadata_and_draft_fail_closed(
    manager: SessionManager, dataset: Dataset, mutation: str
) -> None:
    with manager.create(dataset, settings()) as store:
        decide(store, "confirmed")
        session_id = store.session_id
        path = store.directory / "session.sqlite3"
    with database_connection(path) as connection:
        if mutation == "latest":
            connection.execute("UPDATE decisions SET status='rejected'")
        elif mutation == "reference_hash":
            connection.execute("UPDATE decision_history SET reference_sha256='[]'")
            connection.execute("UPDATE decisions SET reference_sha256='[]'")
        elif mutation == "nonprefix":
            connection.execute("UPDATE decision_history SET step_index=2")
            connection.execute("UPDATE decisions SET step_index=2")
        elif mutation == "draft":
            connection.execute("INSERT INTO drafts VALUES(1,3,1,'lost draft',1,'now')")
        else:
            metadata = json.loads(connection.execute("SELECT metadata FROM session").fetchone()[0])
            key, value = {
                "future_queue": ("queue_version", 99),
                "source_location": ("source_name", "../source.csv"),
                "identity": ("session_id", "mismatch"),
            }[mutation]
            metadata[key] = value
            connection.execute("UPDATE session SET metadata=?", (json.dumps(metadata),))
    with pytest.raises(SessionError):
        manager.open(session_id)


def test_successful_migration_preserves_preupgrade_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(tmp_path / "session.sqlite3")
    try:
        migrations.initialize(connection)
        monkeypatch.setattr(migrations, "SCHEMA_VERSION", 2)

        def upgrade(database: sqlite3.Connection) -> None:
            database.execute("CREATE TABLE synthetic_upgrade(value TEXT)")

        monkeypatch.setattr(migrations, "MIGRATIONS", {2: upgrade})
        migrations.migrate(connection, tmp_path)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='synthetic_upgrade'"
        ).fetchone()
        with database_connection(next(tmp_path.glob("*.backup.sqlite3"))) as saved:
            assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()
