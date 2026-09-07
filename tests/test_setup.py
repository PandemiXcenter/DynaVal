"""Setup rejects invalid preview requests and revokes outdated image previews."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nicegui import ui
from nicegui.testing import User, user_simulation

from dynaval.domain.models import MediaRecord, SessionSettings
from dynaval.importers import parse_dataset
from dynaval.runtime import Runtime
from dynaval.ui.workspace import Workspace

pytest_plugins = ["nicegui.testing.user_plugin"]


@asynccontextmanager
async def setup_screen(
    tmp_path: Path, configure: Callable[[Runtime], None] | None = None
) -> AsyncIterator[tuple[User, Workspace, Runtime]]:
    source = tmp_path / "example.csv"
    source.write_text("image,second.image,field\nfirst.png,other.png,001\nnext.png,last.png,NA\n")
    dataset = parse_dataset(source)
    runtime = Runtime(tmp_path / "application", native=False)
    if configure:
        configure(runtime)
    workspaces: list[Workspace] = []

    def page() -> None:
        workspace = Workspace(runtime)
        workspace.dataset = dataset
        workspace.references = [0]
        workspace.fields = [2]
        workspace.reference_base = str(tmp_path)
        workspace.show("setup")
        workspaces.append(workspace)

    async with user_simulation(page) as user:
        try:
            await user.open("/")
            yield user, workspaces[0], runtime
        finally:
            await runtime.close()


@pytest.mark.parametrize("value", [0, 3, 1.5, None])
async def test_preview_row_range_is_checked_before_media_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: float | None
) -> None:
    async with setup_screen(tmp_path) as (user, workspace, runtime):
        loader = AsyncMock(return_value=[])
        monkeypatch.setattr(runtime.media, "load_row", loader)
        number = next(iter(user.find(kind=ui.number, content="Preview row").elements))
        with user.client:
            number.set_value(value)
        user.find("Preview images").click()
        await user.should_see("Choose a preview row from 1 to 2.")
        loader.assert_not_awaited()
        assert workspace.preview_sessions == set()


@pytest.mark.parametrize("change", ["base", "row", "columns"])
async def test_slow_image_preview_is_revoked_when_configuration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_preview(*_: object, **__: object) -> list[MediaRecord]:
        started.set()
        await release.wait()
        finished.set()
        return [MediaRecord(reference_col=0, reference="first.png", error="STALE PREVIEW")]

    async with setup_screen(tmp_path) as (user, workspace, runtime):
        monkeypatch.setattr(runtime.media, "load_row", slow_preview)
        user.find("Preview images").click()
        await asyncio.wait_for(started.wait(), timeout=2)
        previous = set(workspace.preview_sessions)
        assert previous and previous <= runtime.allowed_media_sessions
        with user.client:
            if change == "base":
                next(iter(user.find(kind=ui.input, content="Image folder").elements)).set_value(
                    str(tmp_path / "new-base")
                )
            elif change == "row":
                next(iter(user.find(kind=ui.number, content="Preview row").elements)).set_value(2)
            else:
                next(iter(user.find(kind=ui.select, content="Image columns").elements)).set_value(
                    [1]
                )
        assert workspace.preview_sessions == set()
        assert not previous & runtime.allowed_media_sessions
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        await asyncio.sleep(0)
        await user.should_not_see("STALE PREVIEW")
        assert not previous & runtime.allowed_media_sessions


async def test_matching_sessions_finish_lookup_before_enabling_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()
    expected: dict[str, str] = {}

    def configure(runtime: Runtime) -> None:
        dataset = parse_dataset(tmp_path / "example.csv")
        with runtime.manager.create(
            dataset, SessionSettings(reference_columns=[0], validation_columns=[2], seed=22)
        ) as store:
            view = store.pause()
            expected["time"] = view.updated_at[:16].replace("T", " ") + " UTC"
        original = runtime.manager.matching

        def matching(sha256: str):
            started.set()
            assert release.wait(timeout=5)
            return original(sha256)

        monkeypatch.setattr(runtime.manager, "matching", matching)

    try:
        async with setup_screen(tmp_path, configure) as (user, _, __):
            assert await asyncio.to_thread(started.wait, 2)
            start = next(iter(user.find(kind=ui.button, content="Start review").elements))
            assert not start.enabled
            release.set()
            await user.should_see("Saved review · 0 / 2 fields resolved", retries=30)
            await user.should_see("Continue saved review")
            await user.should_see(expected["time"])
            assert start.text == "Start new review" and start.enabled
    finally:
        release.set()
