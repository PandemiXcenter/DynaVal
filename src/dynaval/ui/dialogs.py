"""Native file dialogs with a local browser fallback for development/tests."""

from pathlib import Path

from nicegui import app, ui


async def choose_path(*, directory: bool = False, native: bool = True) -> Path | None:
    if native:
        import webview

        window = app.native.main_window
        if window is None:
            raise ValueError("The native file picker is not ready. Please try again.")
        paths = await window.create_file_dialog(
            dialog_type=webview.FileDialog.FOLDER if directory else webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=() if directory else ("Datasets (*.csv;*.tsv;*.json;*.jsonl;*.ndjson)",),
        )
        return Path(paths[0]) if paths else None
    with ui.dialog() as dialog, ui.card().classes("w-96 gap-4"):
        ui.label("Choose folder" if directory else "Choose dataset").classes(
            "text-lg font-semibold"
        )
        value = (
            ui.input("Folder path" if directory else "File path")
            .classes("w-full")
            .props("outlined")
        )
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat color=secondary")
            ui.button("Choose", on_click=lambda: dialog.submit(value.value))
    result = await dialog
    return Path(result).expanduser() if result else None
