"""Native desktop entry point and application lifecycle."""

import argparse
import logging
import multiprocessing
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nicegui import app, native, ui

from dynaval import __version__

app.native.window_args.update(background_color="#f2fdff", min_size=(900, 640))
if sys.platform == "win32":
    app.native.start_args["gui"] = "edgechromium"


def configure(data_dir: Path | None = None, *, native_window: bool = True):
    """Create services and pages without starting a server (also used in tests)."""
    from dynaval.runtime import Runtime
    from dynaval.ui.theme import apply_theme
    from dynaval.ui.workspace import Workspace

    runtime = Runtime(data_dir, native=native_window)
    runtime.register_media_route()

    @ui.page("/")
    def index() -> None:
        workspace: Workspace
        apply_theme()
        with ui.header().classes("app-header items-center gap-3"):
            with ui.element("div").classes("brand-mark"):
                ui.icon("done_all", size="22px")
            ui.label("DynaVal").classes("brand")
            ui.label("DATASET CORRECTION").classes("header-note ml-4")
            ui.space()
            ui.button("Datasets", icon="grid_view", on_click=lambda: workspace.pause()).props(
                "flat color=secondary"
            )
        workspace = Workspace(runtime)

    app.on_shutdown(runtime.close)
    if native_window:

        async def drop(event) -> None:
            files = event.args.get("files", [])
            workspace = next(iter(runtime.workspaces), None)
            if workspace is None:
                return
            with workspace.client:
                if len(files) != 1:
                    workspace.error("Drop one dataset at a time.")
                    return
                await workspace.import_dataset(Path(files[0]))

        app.native.on("drop", drop)
    return runtime


def configure_logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "dynaval.log", maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("dynaval").addHandler(handler)
    logging.getLogger("dynaval").setLevel(logging.INFO)
    # HTTP clients otherwise log full reference URLs, potentially with private query strings.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    """Run the desktop app, or an explicitly requested local browser instance."""
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Correct datasets against source images.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--browser", action="store_true", help="Use a local browser for UI testing."
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--smoke-exit-after", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    from dynaval.storage.paths import log_directory

    configure_logging(args.data_dir / "logs" if args.data_dir else log_directory())
    configure(args.data_dir, native_window=not args.browser)
    if args.smoke_exit_after is not None:
        app.timer(args.smoke_exit_after, app.shutdown, once=True)
    ui.run(
        title="DynaVal",
        host="127.0.0.1",
        port=args.port or native.find_open_port(),
        native=not args.browser,
        window_size=(1440, 920),
        reload=False,
        show=not args.browser,
    )
