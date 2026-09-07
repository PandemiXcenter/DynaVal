"""NiceGUI's simulated users exercise the real local correction workflow."""

import asyncio
import csv
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from inspect import getclosurevars
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nicegui import ui
from nicegui.testing import User, user_simulation
from PIL import Image

from dynaval.domain.errors import SessionError
from dynaval.domain.models import Cell, MediaRecord, PathMapping
from dynaval.runtime import Runtime
from dynaval.ui import dialogs
from dynaval.ui.theme import apply_theme
from dynaval.ui.workspace import Workspace

pytest_plugins = ["nicegui.testing.user_plugin"]


@pytest.fixture(autouse=True)
def no_unhandled_ui_errors(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    yield
    errors = [record.getMessage() for record in caplog.get_records("call") if record.levelno >= 40]
    assert not errors, f"The UI logged unhandled errors: {errors}"


@dataclass
class Screen:
    user: User
    runtime: Runtime
    workspaces: list[Workspace]

    @property
    def workspace(self) -> Workspace:
        return self.workspaces[0]


@asynccontextmanager
async def app_screen(tmp_path: Path) -> AsyncIterator[Screen]:
    runtime = Runtime(tmp_path / "application", native=False)
    workspaces: list[Workspace] = []

    def root() -> None:
        if not workspaces:
            runtime.register_media_route()
        apply_theme()
        workspaces.append(Workspace(runtime))

    async with user_simulation(root) as user:
        try:
            await user.open("/")
            yield Screen(user, runtime, workspaces)
        finally:
            await runtime.close()


def dataset_file(tmp_path: Path, *, rows: int = 1, missing_image: bool = False) -> Path:
    paths: list[Path] = []
    for index in range(rows):
        image = tmp_path / f"source-{index}.png"
        if not missing_image:
            Image.new("RGB", (12, 8), "red" if index % 2 == 0 else "blue").save(image)
        paths.append(image)
    path = tmp_path / "example.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["image", "label", "age"])
        writer.writerows(
            [[image.name, f"record {index}", "007"] for index, image in enumerate(paths)]
        )
    return path


async def eventually(predicate: Callable[[], bool], *, timeout: float = 4) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def button(user: User, text: str) -> ui.button:
    matches = user.find(kind=ui.button, content=text).elements
    exact = [element for element in matches if element.text == text]
    assert len(exact) == 1
    return exact[0]


def start_button(user: User) -> ui.button:
    return next(
        element
        for element in user.find(ui.button).elements
        if element.text in {"Start review", "Start new review"}
    )


async def configure(
    screen: Screen,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fields: tuple[str, ...] = ("label", "age"),
    skipping: bool = False,
    corrections: bool = True,
) -> None:
    user, workspace = screen.user, screen.workspace
    monkeypatch.setattr(dialogs, "choose_path", AsyncMock(return_value=path))
    user.find("Choose local file").click()
    await user.should_see("SET UP YOUR REVIEW", retries=30)
    assert workspace.dataset and workspace.dataset.columns == ["image", "label", "age"]
    assert not start_button(user).enabled
    user.find(kind=ui.select, content="Image columns").click()
    user.find("image").click()
    assert workspace.references == [0]
    # Close the multi-select popup before choosing review checkboxes.
    user.find(kind=ui.select, content="Image columns").click()
    for field in fields:
        user.find(kind=ui.checkbox, content=field).click()
    if skipping:
        user.find("Allow skipping").click()
    if not corrections:
        user.find("Allow corrections").click()
    user.find(kind=ui.input, content="Random seed").clear().type("42")
    assert start_button(user).enabled
    assert workspace.fields == [workspace.dataset.columns.index(field) for field in fields]
    user.find(kind=ui.button, content=start_button(user).text).click()
    await user.should_see("Review & correct", retries=30)
    assert workspace.view and workspace.view.settings.seed == 42


async def test_confirm_empty_correction_autosave_resume_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    original_bytes = source.read_bytes()
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        await configure(screen, source, monkeypatch)
        await eventually(lambda: workspace.media_ready)
        assert button(user, "Confirm as Valid").enabled
        await user.should_not_see(kind=ui.button, content="Skip")
        user.find("Confirm as Valid").click()
        await eventually(lambda: bool(workspace.view and workspace.view.cursor == 1))
        assert workspace.view and workspace.view.resolved == 1
        original_queue = workspace.view.steps
        session_id = workspace.view.session_id
        user.find("Edit value").click()
        await user.should_see("Replacement value")
        user.find(kind=ui.textarea).clear().type("")
        await user.should_see("Edited")
        assert not button(user, "Confirm as Valid").enabled
        assert not button(user, "Mark Invalid").enabled
        assert button(user, "Save Correction").enabled
        await eventually(lambda: workspace.save_status == "Saved locally")
        assert workspace.store is not None
        saved = await asyncio.to_thread(workspace.store.view)
        assert saved.draft and saved.draft.text == "" and saved.draft.edit_active
        user.find("Pause").click()
        await user.should_see("Continue a dataset", retries=30)
        await user.should_see(kind=ui.button, content="Continue", retries=30)
        assert workspace.store is None
        user.find(kind=ui.button, content="Continue").click()
        await user.should_see("Review & correct", retries=30)
        await eventually(lambda: workspace.media_ready)
        assert workspace.view and workspace.view.session_id == session_id
        assert workspace.view.settings.seed == 42 and workspace.view.steps == original_queue
        assert workspace.view.cursor == 1 and workspace.draft_text == "" and workspace.edit_active
        await user.should_see("Edited")
        user.find("Save Correction").click()
        await user.should_see("Selected fields complete", retries=30)
        assert workspace.view and workspace.view.counts["corrected"] == 1
        destination = tmp_path / "exports"
        destination.mkdir()
        monkeypatch.setattr(dialogs, "choose_path", AsyncMock(return_value=destination))
        user.find("Include transcription-accuracy report").click()
        user.find("Export corrected dataset").click()
        await user.should_see("Export saved", retries=30)
        folders = list(destination.iterdir())
        assert len(folders) == 1
        with (folders[0] / "corrected_dataset.csv").open(encoding="utf-8-sig", newline="") as file:
            assert list(csv.reader(file)) == [
                ["image", "label", "age"],
                ["source-0.png", "record 0", ""],
            ]
        with (folders[0] / "review_log.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert len(rows) == 2 and [row["valid"] for row in rows] == ["true", "false"]
        assert rows[1]["is_corrected"] == "true" and rows[1]["correction"] == ""
        assert (folders[0] / "accuracy_report.csv").is_file()
        assert source.read_bytes() == original_bytes


async def test_skip_missing_images_then_follow_up_without_reshuffle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path, missing_image=True)
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        await configure(screen, source, monkeypatch, fields=("label",), skipping=True)
        await user.should_see("Image needs attention", retries=30)
        assert not button(user, "Confirm as Valid").enabled
        assert not button(user, "Mark Invalid").enabled
        assert button(user, "Skip").enabled
        user.find("Skip").click()
        await user.should_see("A little more to resolve", retries=30)
        assert workspace.view and workspace.view.counts["skipped"] == 1
        queue = workspace.view.steps
        user.find("Continue unresolved fields").click()
        await user.should_see("Review & correct", retries=30)
        assert workspace.view and workspace.view.pass_id == 2 and workspace.view.steps == queue
        assert workspace.view.settings.seed == 42
        Image.new("RGB", (12, 8), "green").save(tmp_path / "source-0.png")
        await user.should_see("Retry images", retries=30)
        user.find("Retry images").click()
        await eventually(lambda: workspace.media_ready)
        user.find("Confirm as Valid").click()
        await user.should_see("Selected fields complete", retries=30)
        assert workspace.view and len(workspace.view.decisions) == 1
        assert workspace.view.counts["confirmed"] == 1 and workspace.view.counts["skipped"] == 0


async def test_corrections_disabled_still_allows_invalid_without_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        await configure(screen, source, monkeypatch, fields=("label",), corrections=False)
        await eventually(lambda: workspace.media_ready)
        await user.should_not_see(kind=ui.button, content="Edit value")
        await user.should_not_see(kind=ui.button, content="Skip")
        assert button(user, "Mark Invalid").enabled
        user.find("Mark Invalid").click()
        await user.should_see("A little more to resolve", retries=30)
        assert workspace.view and workspace.view.resolved == 0
        decision = next(iter(workspace.view.decisions.values()))
        assert decision.status == "rejected" and decision.valid is False


async def test_partial_export_does_not_apply_an_unsaved_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        await configure(screen, source, monkeypatch)
        await eventually(lambda: workspace.media_ready)
        user.find("Edit value").click()
        user.find(kind=ui.textarea).clear().type("draft only")
        destination = tmp_path / "exports"
        destination.mkdir()
        monkeypatch.setattr(dialogs, "choose_path", AsyncMock(return_value=destination))
        user.find("Export progress").click()
        await user.should_see("Export saved", retries=30)
        await user.should_see("Partial dataset", retries=30)
        export = next(destination.iterdir())
        manifest = json.loads((export / "manifest.json").read_text())
        assert manifest["selected_fields_complete"] is False
        with (export / "corrected_dataset.csv").open(encoding="utf-8-sig", newline="") as file:
            assert list(csv.DictReader(file))[0]["label"] == "record 0"
        with (export / "review_log.csv").open(encoding="utf-8-sig", newline="") as file:
            assert all(
                row["status"] == "pending" and row["valid"] == "" for row in csv.DictReader(file)
            )
        assert workspace.store
        snapshot = await asyncio.to_thread(workspace.store.view)
        assert snapshot.draft and snapshot.draft.text == "draft only" and not snapshot.decisions


async def test_reverting_a_draft_reenables_confirmation_and_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        await configure(screen, source, monkeypatch, skipping=True)
        await eventually(lambda: workspace.media_ready)
        user.find("Edit value").click()
        user.find(kind=ui.textarea).clear().type("different")
        await user.should_see("Edited")
        assert not button(user, "Confirm as Valid").enabled
        assert not button(user, "Skip").enabled
        user.find("Revert").click()
        await user.should_see("Edit value", retries=30)
        await user.should_not_see("Edited")
        assert button(user, "Confirm as Valid").enabled and button(user, "Skip").enabled
        assert not workspace.edit_active and workspace.draft_text == "record 0"


async def test_second_window_cannot_open_another_windows_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch, fields=("label",))
        await eventually(lambda: screen.workspace.media_ready)
        first = screen.workspace
        assert first.view and first.store
        original_store = first.store
        second_user = User(screen.user.http_client)
        await second_user.open("/")
        second = screen.workspaces[1]
        await second_user.should_see(kind=ui.button, content="Continue", retries=30)
        second_user.find(kind=ui.button, content="Continue").click()
        await eventually(lambda: bool(second.error_text))
        assert second.store is None and second.view is None
        assert first.store is original_store and first.view.cursor == 0
        assert "open" in second.error_text.casefold() or "locked" in second.error_text.casefold()
        await second_user.should_see(second.error_text)


async def test_late_image_response_cannot_replace_next_rows_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path, rows=2)
    async with app_screen(tmp_path) as screen:
        user, workspace = screen.user, screen.workspace
        media = screen.runtime.media
        original_load = media.load_row
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        first_finished = asyncio.Event()
        first_request = True

        async def delayed_load(
            session_id: str,
            cells: list[Cell],
            reference_columns: list[int],
            reference_base: str | None,
            path_mappings: list[PathMapping],
            *,
            force: bool = False,
        ) -> list[MediaRecord]:
            nonlocal first_request
            delayed = first_request
            first_request = False
            if delayed:
                first_started.set()
                await release_first.wait()
            result = await original_load(
                session_id, cells, reference_columns, reference_base, path_mappings, force=force
            )
            if delayed:
                first_finished.set()
            return result

        monkeypatch.setattr(media, "load_row", delayed_load)
        await configure(screen, source, monkeypatch, fields=("label",), skipping=True)
        await asyncio.wait_for(first_started.wait(), 3)
        assert not button(user, "Confirm as Valid").enabled
        user.find("Skip").click()
        await eventually(lambda: bool(workspace.view and workspace.view.cursor == 1))
        await eventually(lambda: workspace.media_ready)
        records = workspace.records.copy()
        release_first.set()
        await asyncio.wait_for(first_finished.wait(), 3)
        await asyncio.sleep(0.05)
        assert workspace.records == records and workspace.view and workspace.view.cursor == 1
        assert button(user, "Confirm as Valid").enabled
        assert workspace.store
        view = await asyncio.to_thread(workspace.store.view)
        assert view.counts["skipped"] == 1 and view.counts["pending"] == 1


async def test_media_route_serves_only_registered_active_session_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch, fields=("label",))
        workspace, user = screen.workspace, screen.user
        await eventually(lambda: workspace.media_ready)
        token = workspace.records[0].media_id
        response = await user.http_client.get(f"/dynaval/media/{token}")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await user.http_client.get("/dynaval/media/unknown")).status_code == 404
        assert (await user.http_client.get("/dynaval/media/..%2Fsource-0.png")).status_code == 404
        user.find("Pause").click()
        await eventually(lambda: workspace.store is None)
        assert (await user.http_client.get(f"/dynaval/media/{token}")).status_code == 404


async def test_failed_save_keeps_current_step_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch)
        workspace, user = screen.workspace, screen.user
        await eventually(lambda: workspace.media_ready)
        assert workspace.store
        submit = workspace.store.submit

        def fail(*args: object, **kwargs: object) -> None:
            raise SessionError("The decision could not be saved. Retry this step.")

        monkeypatch.setattr(workspace.store, "submit", fail)
        user.find("Confirm as Valid").click()
        await user.should_see("The decision could not be saved", retries=30)
        assert workspace.view and workspace.view.cursor == 0 and not workspace.view.decisions
        assert button(user, "Confirm as Valid").enabled
        monkeypatch.setattr(workspace.store, "submit", submit)
        user.find("Confirm as Valid").click()
        await eventually(lambda: bool(workspace.view and workspace.view.cursor == 1))
        assert workspace.view and workspace.view.counts["confirmed"] == 1


async def test_a_retried_old_button_event_cannot_confirm_the_next_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch)
        workspace, user = screen.workspace, screen.user
        await eventually(lambda: workspace.media_ready)
        original_button = button(user, "Confirm as Valid")
        # Capture the rendered callback before rerender deletes its listener registry.
        listener = next(
            listener
            for listener in original_button._event_listeners.values()
            if listener.type == "click"
        )
        callback = getclosurevars(listener.handler).nonlocals["callback"]
        user.find("Confirm as Valid").click()
        await eventually(
            lambda: bool(workspace.view and workspace.view.cursor == 1 and not workspace.busy)
        )
        assert user.client
        with user:
            # Replay the already-dispatched callback, after its original widget was removed.
            await callback()
        assert workspace.view and workspace.view.cursor == 1
        assert workspace.view.counts["confirmed"] == 1 and workspace.view.counts["pending"] == 1


async def test_null_original_requires_explicit_edit_and_accepts_an_empty_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_file(tmp_path)
    source = tmp_path / "example.json"
    source.write_text(json.dumps([{"image": "source-0.png", "label": None, "age": "007"}]))
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch, fields=("label",))
        workspace, user = screen.workspace, screen.user
        await eventually(lambda: workspace.media_ready)
        await user.should_see(kind=ui.label, content="Null")
        assert not workspace.edit_active and not workspace.edited
        user.find("Edit value").click()
        await user.should_see("Edited")
        editor = next(iter(user.find(kind=ui.textarea).elements))
        assert editor.value == ""
        assert not button(user, "Confirm as Valid").enabled
        assert button(user, "Save Correction").enabled
        user.find("Save Correction").click()
        await user.should_see("Selected fields complete", retries=30)
        assert workspace.view
        decision = next(iter(workspace.view.decisions.values()))
        assert decision.status == "corrected" and decision.correction == ""


async def test_an_old_session_button_cannot_modify_a_new_sessions_first_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = dataset_file(tmp_path)
    async with app_screen(tmp_path) as screen:
        await configure(screen, source, monkeypatch, fields=("label",))
        workspace, user = screen.workspace, screen.user
        await eventually(lambda: workspace.media_ready)
        assert workspace.view
        previous_id = workspace.view.session_id
        old_button = button(user, "Confirm as Valid")
        listener = next(
            listener
            for listener in old_button._event_listeners.values()
            if listener.type == "click"
        )
        callback = getclosurevars(listener.handler).nonlocals["callback"]
        user.find("Pause").click()
        await user.should_see("Continue a dataset", retries=30)
        await configure(screen, source, monkeypatch, fields=("label",))
        await eventually(lambda: workspace.media_ready and not workspace.busy)
        assert workspace.view and workspace.view.session_id != previous_id
        assert user.client
        with user:
            await callback()
        assert workspace.view.cursor == 0 and not workspace.view.decisions
