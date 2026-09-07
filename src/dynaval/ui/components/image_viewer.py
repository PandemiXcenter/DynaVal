"""A small local Vue component for source image zooming and panning."""

from nicegui.element import Element


class ImageViewer(Element, component="image_viewer.js"):
    def __init__(self, source: str, label: str) -> None:
        super().__init__()
        self._props.update(source=source, label=label)
        self.classes("w-full h-full")
