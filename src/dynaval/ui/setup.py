"""Dataset preview, reference selection, field selection, and session settings."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from nicegui import ui

from dynaval.domain.models import PathMapping
from dynaval.services.reference_suggestions import suggested_reference_columns
from dynaval.ui import dialogs
from dynaval.ui.components.image_viewer import ImageViewer
from dynaval.ui.theme import section_label

if TYPE_CHECKING:
    from dynaval.ui.workspace import Workspace


def path_controls(workspace: "Workspace", on_change: Callable[[], None] | None = None) -> None:
    with ui.row().classes("w-full items-center no-wrap"):
        base = (
            ui.input(
                "Image folder",
                value=workspace.reference_base or "",
                placeholder="For relative image paths",
            )
            .props("outlined dense")
            .classes("flex-1")
        )

        def update_base(event) -> None:
            workspace.reference_base = event.value
            if on_change:
                on_change()

        base.on_value_change(update_base)

        async def choose_base() -> None:
            path = await dialogs.choose_path(directory=True, native=workspace.runtime.native)
            if path:
                base.set_value(str(path))

        ui.button(icon="folder_open", on_click=choose_base).props(
            'flat color=secondary aria-label="Choose image folder"'
        )
    with ui.expansion("Map a shared folder", icon="drive_folder_upload").classes("w-full"):
        mappings = ui.column().classes("w-full gap-2")

        def show_mappings() -> None:
            mappings.clear()
            with mappings:
                for mapping in workspace.mappings:
                    with ui.row().classes("w-full items-center no-wrap"):
                        ui.label(f"{mapping.source} → {mapping.target}").classes(
                            "text-xs break-all flex-1"
                        )

                        def remove(item: PathMapping = mapping) -> None:
                            workspace.mappings.remove(item)
                            show_mappings()
                            if on_change:
                                on_change()

                        ui.button(icon="close", on_click=remove).props(
                            'flat dense color=secondary aria-label="Remove mapping"'
                        )

        show_mappings()
        source = (
            ui.input("Original path prefix", placeholder=r"\\server\share")
            .props("outlined dense")
            .classes("w-full")
        )
        target = (
            ui.input("Mounted folder", placeholder="/Volumes/share")
            .props("outlined dense")
            .classes("w-full")
        )

        def add() -> None:
            if not source.value or not target.value:
                workspace.error("Enter both the original prefix and its local folder.")
                return
            workspace.mappings.append(PathMapping(source=source.value, target=target.value))
            source.set_value("")
            target.set_value("")
            show_mappings()
            if on_change:
                on_change()

        ui.button("Add mapping", icon="add", on_click=add).props("flat color=secondary")


def render(workspace: "Workspace") -> None:
    dataset = workspace.dataset
    assert dataset is not None
    with ui.row().classes("w-full items-start justify-between"):
        with ui.column().classes("gap-2"):
            section_label("SET UP YOUR REVIEW")
            ui.label(dataset.name).classes("page-title")
            delimiter = (
                f" · {dataset.parser.delimiter!r} delimiter" if dataset.parser.delimiter else ""
            )
            ui.label(
                f"{len(dataset.rows):,} rows · {len(dataset.columns)} columns{delimiter}"
            ).classes("subtle")
            ui.label(
                f"{(dataset.parser.format or 'auto').upper()} · {dataset.parser.encoding}"
            ).classes("text-xs subtle")
        ui.button("Change file", icon="arrow_back", on_click=lambda: workspace.show("home")).props(
            "flat color=secondary"
        )
    matches_area = ui.column().classes("w-full gap-2")
    matches_area.set_visibility(False)
    matches_ready = False

    async def load_matches() -> None:
        nonlocal matches_ready
        try:
            matches = await asyncio.to_thread(workspace.runtime.manager.matching, dataset.sha256)
        except Exception as error:
            workspace.error(error)
            return
        if matches_area.is_deleted:
            return
        matches_area.set_visibility(bool(matches))
        with matches_area:
            for match in matches:
                if match.error:
                    continue
                with (
                    ui.row()
                    .classes("panel w-full items-center justify-between")
                    .style("padding:12px 18px")
                ):
                    ui.label(
                        f"Saved review · {match.resolved:,} / {match.total:,} fields resolved"
                    ).classes("subtle")
                    ui.label(" · ".join(match.columns)).classes("text-xs subtle")
                    ui.label(match.updated_at[:16].replace("T", " ") + " UTC").classes(
                        "text-xs subtle"
                    )
                    ui.button(
                        "Continue saved review",
                        on_click=lambda sid=match.session_id: workspace.open_session(sid),
                    ).props("flat color=secondary")
        matches_ready = True
        if matches:
            start_button.set_text("Start new review")
        update_count()

    ui.timer(0, load_matches, once=True)

    with ui.element("div").classes("setup-grid"):
        with ui.column().classes("panel gap-5 w-full"):
            section_label("01 / SOURCE IMAGES")
            ui.label("Connect the original material").classes("text-xl font-semibold")
            options = {index: name for index, name in enumerate(dataset.columns)}
            ref_select = (
                ui.select(
                    options,
                    value=workspace.references,
                    multiple=True,
                    with_input=True,
                    label="Image columns",
                )
                .props("outlined use-chips")
                .classes("w-full")
            )
            suggestions = suggested_reference_columns(dataset)
            if suggestions:
                with ui.row().classes("items-center gap-1"):
                    ui.label("Suggested").classes("text-xs subtle")
                    for index in suggestions:

                        def add_reference(column: int = index) -> None:
                            ref_select.set_value(sorted(set(workspace.references) | {column}))

                        ui.button(dataset.columns[index], icon="add", on_click=add_reference).props(
                            "flat dense no-caps color=secondary"
                        )
            preview_area = ui.column().classes("w-full")
            preview_generation = 0

            def invalidate_preview() -> None:
                nonlocal preview_generation
                preview_generation += 1
                workspace.runtime.allowed_media_sessions.difference_update(
                    workspace.preview_sessions
                )
                workspace.preview_sessions.clear()
                preview_area.clear()

            path_controls(workspace, on_change=invalidate_preview)
            preview_row = (
                ui.number("Preview row", value=1, min=1, max=len(dataset.rows), precision=0)
                .props("outlined dense")
                .classes("w-32")
            )
            preview_row.on_value_change(invalidate_preview)

            async def preview() -> None:
                nonlocal preview_generation
                if not workspace.references:
                    workspace.error("Choose at least one image column.")
                    return
                try:
                    row_number = float(preview_row.value)
                    if not row_number.is_integer() or not 1 <= row_number <= len(dataset.rows):
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    workspace.error(f"Choose a preview row from 1 to {len(dataset.rows):,}.")
                    return
                invalidate_preview()
                generation = preview_generation
                with preview_area:
                    ui.spinner("dots", size="25px")
                preview_id = f"preview-{uuid4().hex}"
                workspace.preview_sessions.add(preview_id)
                workspace.runtime.allowed_media_sessions.add(preview_id)
                try:
                    records = await workspace.runtime.media.load_row(
                        preview_id,
                        dataset.rows[int(row_number) - 1],
                        sorted(workspace.references),
                        workspace.reference_base,
                        list(workspace.mappings),
                    )
                except Exception as error:
                    if generation == preview_generation and not preview_area.is_deleted:
                        preview_area.clear()
                        workspace.error(error)
                    workspace.runtime.allowed_media_sessions.discard(preview_id)
                    workspace.preview_sessions.discard(preview_id)
                    return
                if preview_area.is_deleted or generation != preview_generation:
                    workspace.runtime.allowed_media_sessions.discard(preview_id)
                    workspace.preview_sessions.discard(preview_id)
                    return
                preview_area.clear()
                with preview_area:
                    for record in records:
                        ui.label(dataset.columns[record.reference_col]).classes("section-label")
                        if record.error:
                            ui.label(record.error).classes("text-sm text-negative")
                        elif record.media_id:
                            with (
                                ui.element("div")
                                .classes("w-full relative overflow-hidden rounded-xl")
                                .style("height:250px;background:#e8f0f2")
                            ):
                                ImageViewer(
                                    f"/dynaval/media/{record.media_id}",
                                    dataset.columns[record.reference_col],
                                )

            ui.button("Preview images", icon="image_search", on_click=preview).props(
                "outline color=secondary"
            )
            with ui.expansion("Dataset preview", icon="table_rows").classes("w-full"):
                table_columns = [
                    {"name": str(i), "label": name, "field": str(i), "align": "left"}
                    for i, name in enumerate(dataset.columns)
                ]
                rows = [
                    {str(i): cell.text for i, cell in enumerate(row)} for row in dataset.rows[:5]
                ]
                ui.table(columns=table_columns, rows=rows).classes("preview-table").props(
                    "flat dense separator=cell"
                )

        with ui.column().classes("panel gap-5 w-full"):
            section_label("02 / FIELDS TO CORRECT")
            ui.label("Choose your focus").classes("text-xl font-semibold")
            search = (
                ui.input(placeholder="Find a column", on_change=lambda: render_fields())
                .props('outlined dense clearable aria-label="Find a column"')
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-between items-center"):
                selected_count = ui.label().classes("subtle")
                with ui.row().classes("gap-0"):

                    def select_all() -> None:
                        workspace.fields = [i for i in options if i not in workspace.references]
                        render_fields()

                    def clear() -> None:
                        workspace.fields = []
                        render_fields()

                    ui.button("All", on_click=select_all).props("flat dense color=secondary")
                    ui.button("Clear", on_click=clear).props("flat dense color=secondary")
            field_area = ui.column().classes("field-list")
            with ui.column().classes("w-full gap-1"):
                section_label("03 / REVIEW SETTINGS")
                ui.switch(
                    "Allow corrections",
                    value=workspace.allow_corrections,
                    on_change=lambda event: setattr(workspace, "allow_corrections", event.value),
                ).props("color=primary")
                ui.switch(
                    "Allow skipping",
                    value=workspace.allow_skipping,
                    on_change=lambda event: setattr(workspace, "allow_skipping", event.value),
                ).props("color=primary")
                with ui.expansion("Review order", icon="shuffle").classes("w-full"):
                    # Text avoids JavaScript number rounding for uint64 seeds.
                    seed = (
                        ui.input("Random seed", value=str(workspace.seed))
                        .props("outlined dense inputmode=numeric")
                        .classes("w-full")
                    )
                    seed.on_value_change(lambda event: setattr(workspace, "seed", event.value))
                    ui.label("Saved with your review. Continuing keeps the same order.").classes(
                        "text-xs subtle"
                    )
            with ui.row().classes("setup-footer items-center justify-between"):
                with ui.column().classes("gap-1"):
                    workload = ui.label("0").classes("stat-number")
                    ui.label("fields to review").classes("text-xs subtle")
                start_button = ui.button(
                    "Start review", icon="arrow_forward", on_click=workspace.start
                )

            def render_fields() -> None:
                field_area.clear()
                query = (search.value or "").casefold()
                with field_area:
                    for index, name in options.items():
                        if index in workspace.references or query not in name.casefold():
                            continue
                        with ui.column().classes("field-option gap-0"):

                            def toggle(event, col=index) -> None:
                                if event.value and col not in workspace.fields:
                                    workspace.fields.append(col)
                                elif not event.value and col in workspace.fields:
                                    workspace.fields.remove(col)
                                update_count()

                            ui.checkbox(name, value=index in workspace.fields, on_change=toggle)
                            ui.label(dataset.rows[0][index].text or "Empty").classes(
                                "field-preview"
                            )
                update_count()

            def update_count() -> None:
                selected_count.set_text(f"{len(workspace.fields)} selected")
                workload.set_text(f"{len(dataset.rows) * len(workspace.fields):,}")
                start_button.set_enabled(
                    bool(matches_ready and workspace.fields and workspace.references)
                )

            def refs_changed(event) -> None:
                workspace.references = sorted(event.value or [])
                workspace.fields = [i for i in workspace.fields if i not in workspace.references]
                invalidate_preview()
                render_fields()

            ref_select.on_value_change(refs_changed)
            render_fields()
