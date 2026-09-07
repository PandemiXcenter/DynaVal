"""The application's small, offline design system."""

from pathlib import Path

from nicegui import ui

PALETTE = {
    "ruby": "#a31621",
    "coral": "#f78764",
    "mist": "#f2fdff",
    "aqua": "#9ad4d6",
    "ink": "#101935",
}


def apply_theme() -> None:
    ui.colors(primary=PALETTE["ruby"], secondary=PALETTE["ink"], accent=PALETTE["aqua"])
    ui.add_css(Path(__file__).with_name("styles.css").read_text(encoding="utf-8"))


def section_label(text: str) -> None:
    ui.label(text).classes("section-label")
