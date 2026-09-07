"""Resolved/unresolved summary with correction exports as the primary action."""

from typing import TYPE_CHECKING

from nicegui import ui

from dynaval.ui.theme import section_label

if TYPE_CHECKING:
    from dynaval.ui.workspace import Workspace


def render(workspace: "Workspace") -> None:
    view = workspace.view
    assert view is not None
    counts = view.counts
    complete = view.resolved == len(view.steps)
    with ui.column().classes("w-full items-center gap-4 py-6"):
        with ui.element("div").classes("success-mark"):
            ui.icon("done_all" if complete else "playlist_add_check", size="38px")
        section_label(view.dataset.name)
        ui.label("Selected fields complete" if complete else "A little more to resolve").classes(
            "page-title text-center"
        )
        ui.label(
            f"{view.resolved:,} of {len(view.steps):,} selected fields confirmed or corrected"
        ).classes("subtle")
    with ui.element("div").classes("summary-stats"):
        for key, label in (
            ("confirmed", "Confirmed"),
            ("corrected", "Corrected"),
            ("rejected", "Needs correction"),
            ("skipped", "Skipped"),
        ):
            with ui.column().classes("summary-stat gap-2"):
                ui.label(f"{counts[key]:,}").classes("stat-number")
                ui.label(label).classes("subtle")
    with ui.column().classes("panel w-full items-center gap-5"):
        ui.label("Your corrected dataset").classes("text-xl font-semibold")
        ui.label("Original rows and columns. Accepted corrections applied.").classes("subtle")
        accuracy = ui.checkbox("Include transcription-accuracy report", value=False)
        with ui.row().classes("gap-3 items-center"):
            if not complete:
                ui.button(
                    "Continue unresolved fields", icon="arrow_forward", on_click=workspace.follow_up
                ).props("outline color=secondary")
            ui.button(
                "Export corrected dataset",
                icon="download",
                on_click=lambda: workspace.export(accuracy.value),
            )
        ui.label("Includes a separate review log.").classes("text-xs subtle")
    ui.button("Back to datasets", icon="arrow_back", on_click=workspace.pause).props(
        "flat color=secondary"
    )
