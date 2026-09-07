"""Per-window orchestration; widgets render state, services own data decisions."""

import asyncio
import logging
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from nicegui import background_tasks, events, ui

from dynaval.domain.errors import DynaValError
from dynaval.domain.models import (
    Dataset,
    Draft,
    MediaRecord,
    Outcome,
    ParserOptions,
    PathMapping,
    SessionSettings,
    SessionView,
)
from dynaval.domain.review import draft_is_edited, editor_text
from dynaval.importers import parse_dataset
from dynaval.services.async_work import acquire_session, durable_call
from dynaval.services.exporting import export_bundle
from dynaval.ui import dialogs, home, review, setup, summary

if TYPE_CHECKING:
    from dynaval.runtime import Runtime
    from dynaval.services.sessions import SessionStore

logger = logging.getLogger(__name__)


class Workspace:
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.runtime.workspaces.add(self)
        self.store: SessionStore | None = None
        self.view: SessionView | None = None
        self.dataset: Dataset | None = None
        self.import_path: Path | None = None
        self.import_name: str | None = None
        self.reference_base: str | None = None
        self.references: list[int] = []
        self.fields: list[int] = []
        self.mapping_source = ""
        self.mapping_target = ""
        self.mappings: list[PathMapping] = []
        self.seed = secrets.randbits(64)
        self.allow_corrections = True
        self.allow_skipping = False
        self.parser_options = ParserOptions()
        self.screen = "home"
        self.busy = False
        self.importing = False
        self.import_progress = 0.0
        self.import_cancel = threading.Event()
        self.error_text = ""
        self.records: list[MediaRecord] = []
        self.selected_image = 0
        self.preview_sessions: set[str] = set()
        self.generation = 0
        self.edit_active = False
        self.draft_text = ""
        self.save_status = "Saved locally"
        self._draft_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._operation = asyncio.Lock()
        self._closed = False
        self.area = ui.column().classes("workspace")
        self.reference_body: ui.column | None = None
        self.field_body: ui.column | None = None
        self.error_label: ui.label | None = None
        self.action_buttons: list[ui.button] = []
        self.confirm_button: ui.button | None = None
        self.correct_button: ui.button | None = None
        self.skip_button: ui.button | None = None
        self.edited_badge: ui.label | None = None
        self.save_label: ui.label | None = None
        self.progress_label: ui.label | None = None
        self.outcome_label: ui.label | None = None
        self.progress_bar: ui.linear_progress | None = None
        self.editor: ui.textarea | None = None
        self.client = ui.context.client
        self.client.on_delete(self.close)
        self.show("home")

    def show(self, screen: str) -> None:
        if self._closed or self.area.is_deleted:
            return
        if screen != "setup":
            self.runtime.allowed_media_sessions.difference_update(self.preview_sessions)
            self.preview_sessions.clear()
        self.screen = screen
        self.error_text = ""
        self.reference_body = None
        self.field_body = None
        self.action_buttons = []
        self.confirm_button = None
        self.correct_button = None
        self.skip_button = None
        self.edited_badge = None
        self.save_label = None
        self.progress_label = None
        self.outcome_label = None
        self.progress_bar = None
        self.editor = None
        self.area.clear()
        with self.area:
            self.error_label = ui.label().classes("error-banner")
            self.error_label.set_visibility(False)
            {
                "home": home.render,
                "setup": setup.render,
                "review": review.render,
                "summary": summary.render,
            }[screen](self)

    def error(self, error: Exception | str) -> None:
        if isinstance(error, (DynaValError, ValueError, str)):
            message = str(error)
        else:
            logger.error("Application operation failed (%s)", type(error).__name__)
            message = "The operation could not finish. Please try again."
        self.error_text = message
        if not self._closed and self.error_label is not None and not self.error_label.is_deleted:
            self.error_label.set_text(message)
            self.error_label.set_visibility(True)

    async def choose_dataset(self) -> None:
        if self._closed or self.busy:
            return
        path = await dialogs.choose_path(native=self.runtime.native)
        if path and not self._closed:
            await self.import_dataset(path)

    async def upload(self, event: events.UploadEventArguments) -> None:
        if self._closed or self.busy or self.importing:
            return
        if event.file.size() > 100 * 1024 * 1024:
            self.error("Datasets must be smaller than 100 MiB.")
            return
        name = Path(event.file.name.replace("\\", "/")).name
        path = self.runtime.upload_dir / f"{uuid4().hex}{Path(name).suffix}"
        await event.file.save(path)
        await self.import_dataset(path, name=name, uploaded=True)

    async def import_dataset(
        self, path: Path, *, name: str | None = None, uploaded: bool = False
    ) -> None:
        if self._closed or self.importing or self.busy:
            return
        if self.store:
            self.error("Pause the current review before importing another dataset.")
            return
        self.import_path, self.import_name = path, name or path.name
        if not uploaded:
            self.reference_base = str(path.resolve().parent)
        else:
            self.reference_base = None
        self.importing = True
        self.import_progress = 0
        self.import_cancel.clear()
        self.show("home")
        try:
            async with self._operation:
                if self._closed:
                    return
                dataset = await durable_call(
                    parse_dataset,
                    path,
                    self.parser_options,
                    name=self.import_name,
                    cancel=self.import_cancel.is_set,
                    progress=lambda value: setattr(self, "import_progress", value),
                )
                if self.import_cancel.is_set() or self._closed:
                    return
                self.dataset = dataset
                self.references, self.fields = [], []
                self.show("setup")
        except Exception as error:
            self.show("home")
            self.error(error)
        finally:
            self.importing = False
            if self.screen == "home":
                message = self.error_text
                self.show("home")
                if message:
                    self.error(message)

    async def retry_import(self) -> None:
        if self.import_path:
            base = self.reference_base
            await self.import_dataset(
                self.import_path,
                name=self.import_name,
                uploaded=self.import_path.parent == self.runtime.upload_dir,
            )
            self.reference_base = base

    async def open_session(self, session_id: str) -> None:
        if self._closed or self.busy:
            return
        self.busy = True
        self.update_actions()
        try:
            async with self._operation:
                if self._closed:
                    return
                await self._pause_store()
                store = await acquire_session(lambda: self.runtime.manager.open(session_id))
                if self._closed:
                    await durable_call(store.close)
                    return
                self.store = store
                self.runtime.allowed_media_sessions.add(store.session_id)
                self.view = await durable_call(store.resume)
                self.enter_review()
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    async def start(self) -> None:
        if self._closed or self.busy or self.dataset is None or self.store is not None:
            return
        self.busy = True
        try:
            settings = SessionSettings(
                reference_columns=sorted(self.references),
                validation_columns=sorted(self.fields),
                seed=int(self.seed),
                allow_corrections=self.allow_corrections,
                allow_skipping=self.allow_skipping,
                reference_base=self.reference_base or None,
                path_mappings=self.mappings,
            )
            dataset = self.dataset
            async with self._operation:
                if self._closed or self.store is not None:
                    return
                store = await acquire_session(
                    lambda: self.runtime.manager.create(dataset, settings)
                )
                if self._closed:
                    await durable_call(store.close)
                    return
                self.store = store
                self.runtime.allowed_media_sessions.add(store.session_id)
                self.view = await durable_call(store.view)
                self.enter_review()
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    def enter_review(self) -> None:
        if self._closed or self.view is None:
            return
        if self.view.current_step is None:
            self.show("summary")
            return
        draft = self.view.draft
        step = self.view.current_step
        self.edit_active = draft.edit_active if draft else False
        self.draft_text = (
            draft.text
            if draft
            else editor_text(self.view.dataset.rows[step.source_row][step.src_col])
        )
        self.save_status = "Saved locally"
        self.records = []
        self.selected_image = 0
        self.generation += 1
        self.show("review")
        background_tasks.create(self.load_images(self.generation))

    @property
    def edited(self) -> bool:
        if not self.view or not self.view.current_step or not self.edit_active:
            return False
        step = self.view.current_step
        original = self.view.dataset.rows[step.source_row][step.src_col]
        return draft_is_edited(original, self.draft_text, self.edit_active)

    @property
    def media_ready(self) -> bool:
        return bool(
            self.view
            and len(self.records) == len(self.view.settings.reference_columns)
            and all(r.sha256 and not r.error and not r.changed for r in self.records)
        )

    def update_actions(self) -> None:
        if self._closed or self.screen != "review":
            return
        for button in self.action_buttons:
            button.set_enabled(not self.busy and not self.edited and self.media_ready)
        if self.confirm_button:
            self.confirm_button.set_enabled(not self.busy and not self.edited and self.media_ready)
        if self.correct_button:
            self.correct_button.set_enabled(not self.busy and self.edited and self.media_ready)
        if self.skip_button is not None:
            self.skip_button.set_enabled(not self.busy and not self.edited)
        if self.edited_badge:
            self.edited_badge.set_visibility(self.edited)
        if self.save_label:
            self.save_label.set_text(self.save_status)
        if self.editor is not None and not self.editor.is_deleted:
            self.editor.set_enabled(not self.busy)

    def _matches_step(
        self,
        expected_step: int | None,
        expected_pass: int | None,
        expected_session: str | None = None,
    ) -> bool:
        if (
            self._closed
            or self.store is None
            or self.view is None
            or self.view.current_step is None
        ):
            return False
        if expected_session is not None and self.view.session_id != expected_session:
            return False
        return (expected_step is None or self.view.current_step.step_index == expected_step) and (
            expected_pass is None or self.view.pass_id == expected_pass
        )

    def edit(
        self,
        *,
        expected_step: int | None = None,
        expected_pass: int | None = None,
        expected_session: str | None = None,
    ) -> None:
        if self.busy or not self._matches_step(expected_step, expected_pass, expected_session):
            return
        if self.view is None or not self.view.settings.allow_corrections:
            return
        self.edit_active = True
        review.render_field(self)
        self.schedule_draft()

    async def revert(
        self,
        *,
        expected_step: int | None = None,
        expected_pass: int | None = None,
        expected_session: str | None = None,
    ) -> None:
        if self.busy or not self._matches_step(expected_step, expected_pass, expected_session):
            return
        assert self.view is not None and self.view.current_step is not None
        step = self.view.current_step
        self.edit_active = False
        self.draft_text = editor_text(self.view.dataset.rows[step.source_row][step.src_col])
        review.render_field(self)
        self.schedule_draft()

    def change_draft(
        self,
        text: str,
        *,
        expected_step: int | None = None,
        expected_pass: int | None = None,
        expected_session: str | None = None,
    ) -> None:
        if (
            self.busy
            or not self.edit_active
            or not self._matches_step(expected_step, expected_pass, expected_session)
        ):
            return
        self.draft_text = text
        self.schedule_draft()

    def schedule_draft(self) -> None:
        if self._closed:
            return
        if self._draft_task:
            self._draft_task.cancel()
        self.save_status = "Saving…"
        self.update_actions()
        self._draft_task = background_tasks.create(self._autosave())

    async def _autosave(self) -> None:
        await asyncio.sleep(0.5)
        try:
            async with self._operation:
                await self._flush_draft()
        except Exception as error:
            self.save_status = "Could not save"
            self.error(error)
        self.update_actions()

    async def _flush_draft(self) -> None:
        if not self.store or not self.view or not self.view.current_step:
            return
        await durable_call(
            self.store.save_draft,
            Draft(
                step_index=self.view.current_step.step_index,
                pass_id=self.view.pass_id,
                text=self.draft_text,
                edit_active=self.edit_active,
            ),
        )
        self.save_status = "Saved locally"

    async def submit(
        self,
        status: Outcome,
        *,
        expected_step: int | None = None,
        expected_pass: int | None = None,
        expected_session: str | None = None,
    ) -> None:
        if self.busy or not self._matches_step(expected_step, expected_pass, expected_session):
            return
        if status != "skipped" and not self.media_ready:
            return
        assert (
            self.store is not None and self.view is not None and self.view.current_step is not None
        )
        store = self.store
        token = (self.view.current_step.step_index, self.view.pass_id)
        self.busy = True
        self.update_actions()
        try:
            async with self._operation:
                if self.store is not store or not self._matches_step(*token):
                    return
                if self._draft_task:
                    self._draft_task.cancel()
                await self._flush_draft()
                step = self.view.current_step
                self.view = await durable_call(
                    store.submit,
                    step.step_index,
                    self.view.pass_id,
                    status,
                    correction=self.draft_text if status == "corrected" else None,
                    edit_active=self.edit_active,
                )
                # Keep the editor's identity in sync with the committed cursor,
                # even if window deletion began while the disk write finished.
                self.edit_active = False
                next_step = self.view.current_step
                self.draft_text = (
                    editor_text(self.view.dataset.rows[next_step.source_row][next_step.src_col])
                    if next_step is not None
                    else ""
                )
                self.save_status = "Saved locally"
                if self._closed:
                    return
                if self.view.current_step and self.view.current_step.source_row == step.source_row:
                    next_step = self.view.current_step
                    self.draft_text = editor_text(
                        self.view.dataset.rows[next_step.source_row][next_step.src_col]
                    )
                    review.update_progress(self)
                    review.render_field(self)
                else:
                    self.enter_review()
                if status == "corrected":
                    ui.notify("Correction saved", type="positive", position="bottom-right")
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    async def load_images(self, generation: int, *, force: bool = False) -> None:
        view, store = self.view, self.store
        if self._closed or view is None or view.current_step is None or store is None:
            return
        row = view.current_step.source_row
        settings = view.settings
        try:
            records = await self.runtime.media.load_row(
                view.session_id,
                view.dataset.rows[row],
                settings.reference_columns,
                settings.reference_base,
                settings.path_mappings,
                force=force,
            )
            async with self._operation:
                if not self._images_current(store, generation, row):
                    return
                await durable_call(store.mark_media, row, records)
                if not self._images_current(store, generation, row):
                    return
                self.records = records
                review.render_reference(self)
                self.update_actions()
            next_rows = [
                view.steps[i].source_row
                for i in view.pass_steps[view.cursor + 1 :]
                if view.steps[i].source_row != row
            ]
            if next_rows:
                if self._prefetch_task:
                    self._prefetch_task.cancel()
                self._prefetch_task = background_tasks.create(
                    self.runtime.media.load_row(
                        view.session_id,
                        view.dataset.rows[next_rows[0]],
                        settings.reference_columns,
                        settings.reference_base,
                        settings.path_mappings,
                    )
                )
        except Exception as error:
            if self._images_current(store, generation, row):
                self.error(error)

    def _images_current(self, store: "SessionStore", generation: int, row: int) -> bool:
        return bool(
            not self._closed
            and self.store is store
            and generation == self.generation
            and self.screen == "review"
            and self.view
            and self.view.current_step
            and self.view.current_step.source_row == row
        )

    async def retry_images(self) -> None:
        if self._closed or self.busy or self.store is None:
            return
        self.generation += 1
        self.records = []
        review.render_reference(self)
        self.update_actions()
        await self.load_images(self.generation, force=True)

    async def acknowledge_image(self, reference: str) -> None:
        if self.view and self.store and not self._closed:
            store, generation = self.store, self.generation
            record = next((r for r in self.records if r.reference == reference), None)
            if not record:
                return
            await self.runtime.media.acknowledge_change(
                self.view.session_id,
                reference,
                expected_sha256=record.sha256,
            )
            if self._closed or self.store is not store or self.generation != generation:
                return
            self.generation += 1
            self.records = []
            review.render_reference(self)
            self.update_actions()
            await self.load_images(self.generation)

    async def pause(self) -> None:
        if self._closed:
            return
        self.generation += 1
        try:
            async with self._operation:
                if self._closed:
                    return
                self.busy = True
                self.update_actions()
                await self._pause_store()
                self.show("home")
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    async def _detach_store(self, store: "SessionStore") -> None:
        try:
            await durable_call(store.close)
        finally:
            self.runtime.allowed_media_sessions.discard(store.session_id)
            if self.store is store:
                self.store = None
            self.records = []

    async def _pause_store(self) -> None:
        if self.store is None:
            return
        if self._draft_task:
            self._draft_task.cancel()
        if self._prefetch_task:
            self._prefetch_task.cancel()
        await self._flush_draft()
        store = self.store
        self.view = await durable_call(store.pause)
        await self._detach_store(store)
        self.generation += 1

    async def follow_up(self) -> None:
        if self._closed or self.busy or self.store is None:
            return
        store = self.store
        self.busy = True
        try:
            async with self._operation:
                if self._closed or self.store is not store:
                    return
                self.view = await durable_call(store.follow_up)
                self.enter_review()
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    async def export(self, include_accuracy: bool = False) -> None:
        if self._closed or self.busy or self.store is None:
            return
        store = self.store
        parent = await dialogs.choose_path(directory=True, native=self.runtime.native)
        if not parent or self._closed or self.store is not store:
            return
        self.busy = True
        self.update_actions()
        try:
            async with self._operation:
                if self._closed or self.store is not store:
                    return
                await self._flush_draft()
                snapshot = await durable_call(store.view)
                path = await durable_call(export_bundle, snapshot, parent, include_accuracy)
                await durable_call(store.record_export, path)
            if self._closed:
                return
            with ui.dialog() as dialog, ui.card().classes("gap-4 max-w-xl"):
                ui.icon("task_alt", size="32px").style("color:#326369")
                ui.label("Export saved").classes("text-xl font-semibold")
                ui.label(str(path)).classes("subtle break-all")
                if snapshot.resolved < len(snapshot.steps):
                    ui.label(
                        "Partial dataset · unresolved fields are listed in the review log."
                    ).classes("subtle")
                ui.button("Done", on_click=dialog.close).classes("self-end")
            dialog.open()
        except Exception as error:
            self.error(error)
        finally:
            self.busy = False
            self.update_actions()

    async def repair_paths(self) -> None:
        if self._closed or self.busy or not self.store or not self.view:
            return
        from dynaval.ui.setup import path_controls

        store = self.store
        settings = self.view.settings
        self.reference_base, self.mappings = settings.reference_base, list(settings.path_mappings)
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-4"):
            ui.label("Image locations").classes("text-xl font-semibold")
            path_controls(self)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                    "flat color=secondary"
                )
                ui.button("Save & retry", on_click=lambda: dialog.submit(True))
        if not await dialog or self._closed or self.store is not store:
            return
        self.busy = True
        self.update_actions()
        self.generation += 1
        try:
            async with self._operation:
                if self._closed or self.store is not store:
                    return
                if self._draft_task:
                    self._draft_task.cancel()
                await self._flush_draft()
                await durable_call(store.pause)
                self.view = await durable_call(
                    store.repair_paths,
                    self.reference_base or None,
                    self.mappings,
                )
                await self.runtime.media.invalidate_session(self.view.session_id)
                self.view = await durable_call(store.resume)
            self.enter_review()
        except Exception as error:
            self.error(error)
            if self.store is store and not self._closed:
                try:
                    self.view = await durable_call(store.resume)
                except Exception as recovery_error:
                    self.error(recovery_error)
        finally:
            self.busy = False
            self.update_actions()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.import_cancel.set()
        self.runtime.allowed_media_sessions.difference_update(self.preview_sessions)
        self.preview_sessions.clear()
        self.generation += 1
        if self._draft_task:
            self._draft_task.cancel()
        if self._prefetch_task:
            self._prefetch_task.cancel()
        try:
            async with self._operation:
                if self.store:
                    store = self.store
                    try:
                        await self._flush_draft()
                        await durable_call(store.pause)
                    finally:
                        await self._detach_store(store)
        finally:
            self.runtime.workspaces.discard(self)
