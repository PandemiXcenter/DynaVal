"""Dataset import and saved-session home screen."""

import asyncio
from typing import TYPE_CHECKING

from nicegui import ui

from dynaval.domain.models import ParserOptions
from dynaval.ui.theme import section_label

if TYPE_CHECKING:
    from dynaval.ui.workspace import Workspace


def render(workspace: "Workspace") -> None:
    with ui.row().classes("w-full items-end justify-between"), ui.column().classes("gap-2"):
        section_label("DATASET WORKSPACE")
        ui.label("Your datasets").classes("page-title")
        ui.label("Review fields against their source images.").classes("subtle")
    with ui.element("div").classes("home-grid"):
        with ui.column().classes("gap-4 w-full"):
            with ui.column().classes("drop-panel w-full"):
                if workspace.importing:
                    ui.spinner("dots", size="42px", color="primary")
                    ui.label("Reading dataset").classes("drop-title")
                    progress = ui.linear_progress(value=0, show_value=False).classes("w-64")
                    ui.timer(0.1, lambda: progress.set_value(workspace.import_progress))
                    ui.button("Cancel", on_click=workspace.import_cancel.set).props(
                        "flat color=secondary"
                    )
                else:
                    ui.icon("upload_file", size="36px").classes("upload-icon")
                    ui.label("Drop a dataset").classes("drop-title")
                    ui.label("CSV · TSV · JSONL · JSON").classes("subtle")
                    ui.upload(
                        label="Browse or drop a file",
                        auto_upload=True,
                        max_file_size=100 * 1024 * 1024,
                        max_files=1,
                        on_upload=workspace.upload,
                        on_rejected=lambda: workspace.error("Choose one dataset under 100 MiB."),
                    ).props('accept=".csv,.tsv,.json,.jsonl,.ndjson" flat').classes(
                        "dataset-upload"
                    )
                    ui.button(
                        "Choose local file", icon="folder_open", on_click=workspace.choose_dataset
                    ).props("flat color=secondary")
                    ui.label("Your dataset stays on this device.").classes("text-xs subtle")
            with ui.expansion("Import options", icon="tune").classes("w-full"):
                options = workspace.parser_options
                with ui.row().classes("w-full gap-3"):
                    format_input = (
                        ui.select(
                            {
                                "auto": "Auto detect",
                                "csv": "CSV",
                                "tsv": "TSV",
                                "json": "JSON",
                                "jsonl": "JSONL",
                                "ndjson": "NDJSON",
                            },
                            value=options.format or "auto",
                            label="Format",
                        )
                        .props("outlined dense")
                        .classes("flex-1")
                    )
                    delimiter = (
                        ui.input("Delimiter", value=options.delimiter or "", placeholder="Auto")
                        .props("outlined dense maxlength=1")
                        .classes("w-28")
                    )
                    encoding = (
                        ui.input("Encoding", value=options.encoding)
                        .props("outlined dense")
                        .classes("flex-1")
                    )

                def set_options() -> None:
                    workspace.parser_options = ParserOptions(
                        format=None if format_input.value == "auto" else format_input.value,
                        delimiter=delimiter.value or None,
                        encoding=encoding.value or "utf-8-sig",
                    )

                format_input.on_value_change(set_options)
                delimiter.on_value_change(set_options)
                encoding.on_value_change(set_options)
                if workspace.import_path:
                    ui.button(
                        "Retry import", icon="refresh", on_click=workspace.retry_import
                    ).props("flat color=secondary")
        with ui.column().classes("panel gap-5 w-full"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("Continue a dataset").classes("text-lg font-semibold")
                ui.icon("history", size="22px").classes("subtle")
            sessions_area = ui.column().classes("w-full gap-3")

            async def load_sessions() -> None:
                try:
                    sessions = await asyncio.to_thread(workspace.runtime.manager.list_sessions)
                except Exception as error:
                    workspace.error(error)
                    return
                if sessions_area.is_deleted:
                    return
                with sessions_area:
                    if not sessions:
                        with ui.column().classes("empty-sessions w-full"):
                            ui.icon("layers", size="40px").style("color:#9ad4d6")
                            ui.label("No saved datasets").classes("font-semibold")
                            ui.label("Your saved datasets will appear here.").classes("subtle")
                    for session in sessions:
                        with ui.column().classes("session-card"):
                            with ui.row().classes("w-full justify-between items-start"):
                                ui.label(session.name).classes("session-name")
                                ui.label(
                                    {
                                        "active": "In progress",
                                        "paused": "Paused",
                                        "needs_attention": "Unresolved",
                                        "completed": "Complete",
                                    }[session.state]
                                ).classes("pill")
                            ui.label(" · ".join(session.columns)).classes("text-xs subtle")
                            ui.label(session.updated_at[:16].replace("T", " ") + " UTC").classes(
                                "text-xs subtle"
                            )
                            ui.linear_progress(
                                value=session.resolved / max(1, session.total), show_value=False
                            ).props("color=accent")
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(
                                    f"{session.resolved:,} / {session.total:,} fields resolved"
                                ).classes("subtle")
                                if session.error:
                                    ui.label(session.error).classes("text-xs text-negative")
                                else:
                                    ui.button(
                                        "Open" if session.state == "completed" else "Continue",
                                        icon="arrow_forward",
                                        on_click=lambda sid=session.session_id: (
                                            workspace.open_session(sid)
                                        ),
                                    ).props("flat color=secondary")

            ui.timer(0, load_sessions, once=True)
