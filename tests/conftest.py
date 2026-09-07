"""The default test suite stays independent of a browser installation."""

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-browser",
        action="store_true",
        default=False,
        help="Run real Chromium application tests (requires uv run playwright install chromium).",
    )
    parser.addoption(
        "--app-executable",
        default=None,
        metavar="PATH",
        help=(
            "Run browser tests against a packaged executable instead of the Python source app. "
            "Pass the macOS bundle's Contents/MacOS/DynaVal or Windows DynaVal.exe."
        ),
    )


@pytest.fixture
def app_executable(pytestconfig: pytest.Config) -> Path | None:
    supplied = pytestconfig.getoption("--app-executable")
    if supplied is None:
        return None
    path = Path(supplied).expanduser().resolve()
    if not path.is_file():
        pytest.fail(
            "--app-executable must point to an existing executable file: "
            "Contents/MacOS/DynaVal inside a macOS .app, or DynaVal.exe on Windows."
        )
    if os.name != "nt" and not os.access(path, os.X_OK):
        pytest.fail(f"--app-executable is not executable: {path}")
    return path


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: opt-in real Chromium application regression")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-browser"):
        return
    skip = pytest.mark.skip(reason="Use --run-browser to run the real Chromium workflow.")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)
