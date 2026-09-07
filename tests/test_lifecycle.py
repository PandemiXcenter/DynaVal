"""Window lifecycle races must not lose a committed correction or leak a writer."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from nicegui.testing import User, user_simulation
from PIL import Image

from dynaval.domain.models import SessionSettings
from dynaval.runtime import Runtime
from dynaval.ui.workspace import Workspace

pytest_plugins = ["nicegui.testing.user_plugin"]


@asynccontextmanager
async def lifecycle_screen(tmp_path: Path) -> AsyncIterator[tuple[User, Workspace, Runtime]]:
    source = tmp_path / "example.csv"
    source.write_text("image,first,second\nimage.png,original,next original\n")
    Image.new("RGB", (5, 5), "red").save(tmp_path / "image.png")
    runtime = Runtime(tmp_path / "application", native=False)
    workspaces: list[Workspace] = []

    def page() -> None:
        workspaces.append(Workspace(runtime))

    async with user_simulation(page) as user:
        try:
            await user.open("/")
            workspace = workspaces[0]
            with user.client:
                await workspace.import_dataset(source)
            workspace.references = [0]
            workspace.fields = [1, 2]
            yield user, workspace, runtime
        finally:
            await runtime.close()


@pytest.mark.parametrize("action", ["pause", "close"])
async def test_window_action_waits_for_decision_and_keeps_next_draft_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    async with lifecycle_screen(tmp_path) as (user, workspace, runtime):
        with user.client:
            await workspace.start()
        async with asyncio.timeout(3):
            while not workspace.media_ready:
                await asyncio.sleep(0.01)
        store = workspace.store
        assert store is not None
        session_id = store.session_id
        original_submit = store.submit

        def slow_submit(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(store, "submit", slow_submit)
        with user.client:
            workspace.edit()
            workspace.change_draft("accepted correction")
            decision = asyncio.create_task(workspace.submit("corrected"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            with user.client:
                lifecycle = asyncio.create_task(getattr(workspace, action)())
            await asyncio.sleep(0)
            assert not lifecycle.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(decision, lifecycle), timeout=5)
            assert workspace.store is None
            with runtime.manager.open(session_id) as reopened:
                view = reopened.view()
                assert view.cursor == 1
                assert view.decisions[0].correction == "accepted correction"
                assert view.draft is not None
                assert view.draft.step_index == 1 and not view.draft.edit_active
                assert view.draft.text == "next original"
        finally:
            release.set()


@pytest.mark.parametrize("operation", ["create", "open"])
async def test_closing_window_during_session_acquisition_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    async with lifecycle_screen(tmp_path) as (user, workspace, runtime):
        assert workspace.dataset is not None
        if operation == "open":
            with runtime.manager.create(
                workspace.dataset,
                SessionSettings(reference_columns=[0], validation_columns=[1, 2], seed=4),
            ) as stored:
                session_id = stored.session_id
        original = getattr(runtime.manager, operation)

        def slow_acquire(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.manager, operation, slow_acquire)
        with user.client:
            opening = asyncio.create_task(
                workspace.start() if operation == "create" else workspace.open_session(session_id)
            )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            closing = asyncio.create_task(workspace.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(opening, closing), timeout=5)
            assert workspace.store is None and workspace not in runtime.workspaces
            monkeypatch.setattr(runtime.manager, operation, original)
            saved = runtime.manager.list_sessions()
            assert len(saved) == 1
            with runtime.manager.open(saved[0].session_id) as reopened:
                assert reopened.view().cursor == 0
        finally:
            release.set()
