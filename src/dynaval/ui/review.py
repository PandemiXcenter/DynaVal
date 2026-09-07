"""Source-image and single-field correction screen."""

from typing import TYPE_CHECKING, TypedDict

from nicegui import ui

from dynaval.ui.components.image_viewer import ImageViewer
from dynaval.ui.theme import section_label

if TYPE_CHECKING:
    from dynaval.ui.workspace import Workspace


class _EventIdentity(TypedDict):
    expected_session: str
    expected_step: int
    expected_pass: int


def render(workspace: "Workspace") -> None:
    view = workspace.view
    assert view is not None and view.current_step is not None
    with ui.row().classes("w-full justify-between items-center"):
        with ui.column().classes("gap-2"):
            section_label(view.dataset.name)
            ui.label("Review & correct").classes("page-title")
        with ui.row().classes("items-center gap-3"):
            ui.label("Autosaved locally").classes("save-state")
            ui.button("Export progress", icon="download", on_click=workspace.export).props(
                "flat color=secondary"
            )
            ui.button("Pause", icon="pause", on_click=workspace.pause).props(
                "outline color=secondary"
            )
    with ui.column().classes("w-full gap-2"):
        workspace.progress_label = ui.label().classes("subtle")
        workspace.progress_bar = ui.linear_progress(value=0, show_value=False).props(
            "color=accent size=5px"
        )
        workspace.outcome_label = ui.label().classes("text-xs subtle")
        update_progress(workspace)
    with ui.splitter(value=58, limits=(35, 72)).classes("review-splitter") as splitter:
        with splitter.before, ui.column().classes("panel reference-panel w-full"):
            with ui.row().classes("reference-toolbar justify-between items-center"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("image", size="18px").classes("subtle")
                    section_label("SOURCE MATERIAL")
                ui.button(icon="folder_open", on_click=workspace.repair_paths).props(
                    'flat dense color=secondary aria-label="Repair image locations"'
                )
            workspace.reference_body = ui.column().classes("reference-body")
            render_reference(workspace)
        with splitter.after:
            workspace.field_body = ui.column().classes("panel review-field w-full")
            render_field(workspace)


def update_progress(workspace: "Workspace") -> None:
    view = workspace.view
    if view is None:
        return
    step = view.current_step
    prefix = f"Pass {view.pass_id} · " if view.pass_id > 1 else ""
    if workspace.progress_label:
        workspace.progress_label.set_text(
            f"{prefix}Step {min(view.cursor + 1, len(view.pass_steps)):,} "
            f"of {len(view.pass_steps):,}"
            f"  ·  {view.resolved:,} fields resolved"
            + (f"  ·  Source row {step.source_row + 1:,}" if step else "")
        )
    if workspace.progress_bar:
        workspace.progress_bar.set_value(view.resolved / max(1, len(view.steps)))
    if workspace.outcome_label:
        counts = view.counts
        workspace.outcome_label.set_text(
            f"{counts['confirmed']:,} confirmed · {counts['corrected']:,} corrected · "
            f"{counts['rejected']:,} needs correction · {counts['skipped']:,} skipped"
        )


def render_reference(workspace: "Workspace") -> None:
    body = workspace.reference_body
    view = workspace.view
    if body is None or view is None:
        return
    body.clear()
    with body:
        if not workspace.records:
            with ui.column().classes("reference-message"):
                ui.spinner("dots", size="38px", color="secondary")
                ui.label("Loading source images").classes("subtle")
            return
        workspace.selected_image = min(workspace.selected_image, len(workspace.records) - 1)
        with (
            ui.row()
            .classes("reference-tabs w-full")
            .style("padding:10px 12px;background:white;z-index:1")
        ):
            for index, record in enumerate(workspace.records):

                def select(i: int = index) -> None:
                    workspace.selected_image = i
                    render_reference(workspace)

                label = view.dataset.columns[record.reference_col]
                ui.button(
                    label, icon="warning_amber" if record.error else None, on_click=select
                ).props(
                    "unelevated color=secondary"
                    if index == workspace.selected_image
                    else "flat color=secondary"
                )
        record = workspace.records[workspace.selected_image]
        with ui.column().classes("w-full relative flex-1").style("min-height:270px"):
            if record.media_id:
                ImageViewer(
                    f"/dynaval/media/{record.media_id}", view.dataset.columns[record.reference_col]
                )
            if record.error:
                with (
                    ui.column()
                    .classes("reference-message self-center")
                    .style("z-index:2;background:#ffffffed;margin:auto;border-radius:12px")
                ):
                    ui.icon("image_not_supported", size="32px").classes("subtle")
                    ui.label("Image needs attention").classes("font-semibold")
                    ui.label(record.error).classes("text-sm subtle")
                    if record.changed:
                        ui.button(
                            "Use updated image",
                            on_click=lambda: workspace.acknowledge_image(record.reference),
                        )
                    else:
                        ui.button(
                            "Retry images", icon="refresh", on_click=workspace.retry_images
                        ).props("outline color=secondary")
                    ui.button(
                        "Image locations", icon="folder_open", on_click=workspace.repair_paths
                    ).props("flat color=secondary")
        if not workspace.media_ready and not record.error:
            with ui.row().classes("w-full p-3 items-center gap-2").style("background:#fff0e9"):
                ui.icon("warning_amber", size="18px")
                ui.label("Another source image needs attention.").classes("text-xs")


def render_field(workspace: "Workspace") -> None:
    view, body = workspace.view, workspace.field_body
    if view is None or view.current_step is None or body is None:
        return
    body.clear()
    workspace.action_buttons = []
    workspace.confirm_button = None
    workspace.correct_button = None
    workspace.skip_button = None
    workspace.editor = None
    step = view.current_step
    expected: _EventIdentity = {
        "expected_session": view.session_id,
        "expected_step": step.step_index,
        "expected_pass": view.pass_id,
    }
    cell = view.dataset.rows[step.source_row][step.src_col]
    with body:
        with ui.row().classes("w-full items-center justify-between"):
            section_label("CURRENT FIELD")
            workspace.edited_badge = ui.label("Edited").classes("pill pill-coral")
        ui.label(view.dataset.columns[step.src_col]).classes("field-title")
        with ui.column().classes("w-full gap-2"):
            section_label("ORIGINAL VALUE")
            text = {"missing": "Missing", "null": "Null"}.get(
                cell.kind, cell.text or "Empty string"
            )
            original = ui.label(text).classes("original-value")
            if cell.kind in ("missing", "null") or not cell.text:
                original.classes("empty-value")
        if workspace.edit_active:
            with ui.column().classes("w-full gap-2"):
                with ui.row().classes("w-full items-center justify-between"):
                    section_label("YOUR CORRECTION")
                    ui.button(
                        "Revert", icon="undo", on_click=lambda: workspace.revert(**expected)
                    ).props("flat dense color=secondary")
                workspace.editor = (
                    ui.textarea(
                        "Replacement value",
                        value=workspace.draft_text,
                        on_change=lambda event: workspace.change_draft(event.value, **expected),
                    )
                    .props("outlined autogrow")
                    .classes("correction-editor edited")
                )
        elif view.settings.allow_corrections:
            ui.button("Edit value", icon="edit", on_click=lambda: workspace.edit(**expected)).props(
                "outline color=secondary"
            )
        with ui.column().classes("review-actions"):
            if workspace.edit_active:
                workspace.correct_button = ui.button(
                    "Save Correction",
                    icon="check",
                    on_click=lambda: workspace.submit("corrected", **expected),
                ).classes("primary-action")
            workspace.confirm_button = ui.button(
                "Confirm as Valid",
                icon="check",
                on_click=lambda: workspace.submit("confirmed", **expected),
            ).classes("primary-action")
            if workspace.edit_active:
                workspace.confirm_button.props("outline color=secondary")
            with ui.row().classes("w-full justify-between"):
                reject = ui.button(
                    "Mark Invalid", on_click=lambda: workspace.submit("rejected", **expected)
                ).props("flat color=secondary")
                workspace.action_buttons.append(reject)
                if view.settings.allow_skipping:
                    workspace.skip_button = ui.button(
                        "Skip",
                        icon="skip_next",
                        on_click=lambda: workspace.submit("skipped", **expected),
                    ).props("flat color=secondary")
            with ui.row().classes("w-full justify-between items-center"):
                ui.icon("lock_outline", size="14px").classes("subtle")
                workspace.save_label = ui.label(workspace.save_status).classes("save-state")
        workspace.update_actions()
