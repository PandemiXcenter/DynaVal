"""Cross-service correction workflows with real snapshots, images, and CSV bundles."""

import asyncio
import csv
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image

from dynaval.domain.models import Draft, ParserOptions, SessionSettings
from dynaval.importers import parse_dataset
from dynaval.services.async_work import acquire_session, durable_call
from dynaval.services.exporting import export_bundle
from dynaval.services.media import MediaService
from dynaval.services.sessions import SessionManager, SessionStore


def _png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, "PNG")
    return output.getvalue()


async def _load_current(store: SessionStore, media: MediaService) -> None:
    view = store.view()
    assert view.current_step is not None
    row = view.current_step.source_row
    records = await media.load_row(
        view.session_id,
        view.dataset.rows[row],
        view.settings.reference_columns,
        view.settings.reference_base,
        view.settings.path_mappings,
    )
    assert all(record.sha256 and not record.error for record in records)
    store.mark_media(row, records)


def _csv_records(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


@pytest.mark.asyncio
async def test_complete_correction_workflow_and_offline_continuation(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.csv"
    columns = ["image", "other.image", "entity.code", "note", "quantity", "untouched"]
    originals = [
        ["first.png", "https://images.invalid/reference.png", "0007", "NA", "5", " FALSE "],
        [
            "second.png",
            "https://images.invalid/reference.png",
            "0012",
            "quoted\nline",
            "",
            " original ",
        ],
    ]
    (tmp_path / "first.png").write_bytes(_png("red"))
    (tmp_path / "second.png").write_bytes(_png("blue"))
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="$")
        writer.writerow(columns)
        writer.writerows(originals)
    dataset = parse_dataset(source, ParserOptions(delimiter="$"))
    manager = SessionManager(tmp_path / "sessions")
    cache = tmp_path / "cache"
    requests: list[str] = []

    def remote(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=_png("green"), headers={"Content-Type": "image/png"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    media = MediaService(cache, client)
    store = manager.create(
        dataset,
        SessionSettings(
            reference_columns=[0, 1],
            validation_columns=[2, 3, 4],
            allow_skipping=True,
            seed=2**64 - 1,
            reference_base=str(tmp_path),
        ),
    )
    try:
        initial = store.view()
        for outcome, correction in (
            ("confirmed", None),
            ("corrected", ""),
            ("rejected", None),
            ("skipped", None),
        ):
            await _load_current(store, media)
            view = store.view()
            store.submit(view.current_step.step_index, view.pass_id, outcome, correction)  # type: ignore[arg-type]
        view = store.view()
        store.save_draft(
            Draft(
                step_index=view.current_step.step_index,
                pass_id=1,
                text="reviewer replacement",
                edit_active=True,
            )
        )
        paused = store.pause()
        partial = export_bundle(paused, tmp_path)
        store.record_export(partial)
        manifest = json.loads((partial / "manifest.json").read_text())
        assert manifest["partial"] and manifest["unresolved_fields"] == 4
        assert manifest["counts"] == {
            "confirmed": 1,
            "corrected": 1,
            "rejected": 1,
            "skipped": 1,
            "pending": 2,
        }
        session_id = store.session_id
    finally:
        store.close()
        await media.close()
        await client.aclose()
    assert requests == ["/reference.png"]
    source.unlink()
    (tmp_path / "first.png").unlink()
    (tmp_path / "second.png").unlink()

    def offline(_: httpx.Request) -> httpx.Response:
        pytest.fail("Continuation should use the saved image cache while offline.")

    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    media = MediaService(cache, client)
    store = manager.open(session_id)
    try:
        resumed = store.resume()
        assert resumed.settings.seed == initial.settings.seed and resumed.steps == initial.steps
        assert resumed.cursor == 4 and resumed.draft.text == "reviewer replacement"
        await _load_current(store, media)
        store.submit(4, 1, "corrected", "reviewer replacement", edit_active=True)
        await _load_current(store, media)
        first_pass = store.submit(5, 1, "confirmed")
        assert first_pass.state == "needs_attention"
        follow = store.follow_up()
        assert follow.pass_steps == [2, 3]
        await _load_current(store, media)
        store.submit(2, 2, "corrected", "resolved error")
        await _load_current(store, media)
        final = store.submit(3, 2, "confirmed")
        assert final.state == "completed" and final.resolved == 6
        bundle = export_bundle(final, tmp_path, include_accuracy=True)
        store.record_export(bundle)
        expected = [row[:] for row in originals]
        for index, correction in ((1, ""), (4, "reviewer replacement"), (2, "resolved error")):
            step = initial.steps[index]
            expected[step.source_row][step.src_col] = correction
        assert _csv_records(bundle / "corrected_dataset.csv") == [columns, *expected]
        with (bundle / "review_log.csv").open(encoding="utf-8-sig", newline="") as stream:
            log = list(csv.DictReader(stream))
        assert len(log) == 6 and len({row["step_index"] for row in log}) == 6
        assert sum(row["valid"] == "true" for row in log) == 3
        assert sum(row["valid"] == "false" for row in log) == 3
        assert all(len(json.loads(row["reference_sha256"])) == 2 for row in log)
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["selected_fields_complete"] and not manifest["all_fields_selected"]
        assert not manifest["partial"] and manifest["unresolved_fields"] == 0
        with (bundle / "accuracy_report.csv").open(encoding="utf-8-sig", newline="") as stream:
            accuracy = list(csv.DictReader(stream))
        assert accuracy[0]["original_accuracy"] == "0.5"
        assert accuracy[0]["correction_completion"] == "1.0"
        assert partial != bundle and partial.is_dir()
    finally:
        store.close()
        await media.close()
        await client.aclose()
    with manager.open(session_id) as recovered:
        assert recovered.view().state == "completed" and recovered.view().resolved == 6


def test_reference_shape_full_queue_and_partial_export_cardinality(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-reference-shape.csv"
    columns = [
        "image.first",
        "image.second",
        "image.third",
        *[f"field.{index}" for index in range(40)],
    ]
    values = ["001", "NA", "FALSE", "", " text "] * 8
    row = [
        r"\\example\share\first.png",
        r"\\example\share\second.png",
        r"\\example\share\third.png",
        *values,
    ]
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="$")
        writer.writerow(columns)
        writer.writerows(row for _ in range(1115))
    dataset = parse_dataset(source, ParserOptions(delimiter="$"))
    manager = SessionManager(tmp_path / "sessions")
    with manager.create(
        dataset,
        SessionSettings(
            reference_columns=[0, 1, 2], validation_columns=list(range(3, 43)), seed=8842
        ),
    ) as store:
        saved = store.pause()
        session_id = store.session_id
        assert len(saved.steps) == 44_600 and len(saved.dataset.rows) == 1115
    with manager.open(session_id) as store:
        view = store.view()
        assert view.steps == saved.steps and view.settings.seed == 8842
        bundle = export_bundle(view, tmp_path)
    corrected = _csv_records(bundle / "corrected_dataset.csv")
    assert len(corrected) == 1116 and corrected[0] == columns
    assert all(record == row and len(record) == 43 for record in corrected[1:])
    with (bundle / "review_log.csv").open(encoding="utf-8-sig", newline="") as stream:
        entries = list(csv.DictReader(stream))
    assert len(entries) == 44_600
    assert [int(entry["step_index"]) for entry in entries] == list(range(44_600))
    assert len({(entry["source_row"], entry["src_col"]) for entry in entries}) == 44_600
    assert all(entry["status"] == "pending" and entry["valid"] == "" for entry in entries)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["record_count"] == 1115 and manifest["scheduled_fields"] == 44_600
    assert manifest["all_fields_selected"] and not manifest["selected_fields_complete"]


@pytest.mark.asyncio
async def test_cancelled_disk_save_holds_async_lock_until_thread_finishes() -> None:
    started, release = threading.Event(), threading.Event()
    operations: list[str] = []
    lock = asyncio.Lock()

    def write() -> None:
        started.set()
        assert release.wait(timeout=5)
        operations.append("saved")

    async def save() -> None:
        async with lock:
            await durable_call(write)

    async def next_operation() -> None:
        async with lock:
            operations.append("next")

    task = asyncio.create_task(save())
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    following = asyncio.create_task(next_operation())
    await asyncio.sleep(0)
    assert lock.locked() and not task.done() and operations == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await following
    assert operations == ["saved", "next"]


@pytest.mark.asyncio
async def test_cancelled_session_open_releases_acquired_writer_lock(tmp_path: Path) -> None:
    source = tmp_path / "example.csv"
    source.write_text("image,value\nsource.png,original\n")
    manager = SessionManager(tmp_path / "sessions")
    with manager.create(
        parse_dataset(source),
        SessionSettings(reference_columns=[0], validation_columns=[1], seed=1),
    ) as store:
        session_id = store.session_id
    started, release = threading.Event(), threading.Event()

    def open_later() -> SessionStore:
        started.set()
        assert release.wait(timeout=5)
        return manager.open(session_id)

    task = asyncio.create_task(acquire_session(open_later))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with manager.open(session_id) as reopened:
        assert reopened.view().cursor == 0
