"""Opt-in Chromium regression with a real server, process restart, and CSV exports."""

import asyncio
import csv
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Page, async_playwright, expect

pytestmark = pytest.mark.browser
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT_ROOT / "output" / "playwright"


def synthetic_dataset(directory: Path) -> Path:
    image = Image.new("RGB", (900, 600), "#f2fdff")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((45, 45, 855, 555), radius=24, fill="white", outline="#9ad4d6", width=4)
    title_font = ImageFont.load_default(size=46)
    text_font = ImageFont.load_default(size=32)
    draw.text((90, 95), "Synthetic source record", fill="#101935", font=title_font)
    draw.text((90, 210), "Label: record 0", fill="#101935", font=text_font)
    draw.text((90, 280), "Age: 013", fill="#a31621", font=text_font)
    draw.text((90, 440), "Generated test data", fill="#101935", font=text_font)
    image.save(directory / "source.png")
    path = directory / "example.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["image", "label", "age"])
        writer.writerow(["source.png", "record 0", "007"])
    return path


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        # Signal the group so uv and the Python server both receive shutdown.
        with suppress(ProcessLookupError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        if sys.platform == "win32":
            cleanup = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout=5)


@asynccontextmanager
async def running_app(
    data_dir: Path, *, label: str, executable: Path | None = None
) -> AsyncIterator[str]:
    if executable is not None:
        command = [str(executable)]
    else:
        uv = shutil.which("uv")
        if uv is None:
            pytest.fail("Browser tests start the application with uv. Add uv to PATH and retry.")
        command = [uv, "run", "--no-sync", "python", "main.py"]
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    port = available_port()
    url = f"http://127.0.0.1:{port}"
    log_path = ARTIFACTS / f"server-{label}.log"
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    # This subprocess is a normal application, not NiceGUI's simulated pytest server.
    environment.pop("PYTEST_CURRENT_TEST", None)
    environment.pop("NICEGUI_USER_SIMULATION", None)
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            "--browser",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                try:
                    async with asyncio.timeout(30):
                        while True:
                            if process.returncode is not None:
                                pytest.fail(
                                    f"The application exited during startup. Inspect {log_path}."
                                )
                            try:
                                # This endpoint establishes readiness without opening a UI client.
                                response = await client.get(f"{url}/dynaval/media/readiness-check")
                                if response.status_code == 404:
                                    break
                            except httpx.RequestError:
                                pass
                            await asyncio.sleep(0.1)
                except TimeoutError:
                    pytest.fail(f"Application startup exceeded 30 seconds. Inspect {log_path}.")
            yield url
        finally:
            await stop_process(process)


def capture_browser_errors(page: Page, errors: list[str]) -> None:
    page.on("pageerror", lambda error: errors.append(f"Page: {error}"))
    page.on(
        "console",
        lambda message: (
            errors.append(f"Console: {message.text}") if message.type == "error" else None
        ),
    )
    page.on("requestfailed", lambda request: errors.append(f"Request: {request.failure}"))
    page.on(
        "response",
        lambda response: (
            errors.append(f"HTTP {response.status}: {response.url}")
            if response.status >= 400
            else None
        ),
    )


async def image_ready(page: Page) -> None:
    image = page.get_by_role("img", name="image", exact=True)
    await expect(image).to_be_visible()
    await expect(image).to_have_js_property("naturalWidth", 900)


async def test_browser_correction_survives_process_restart_and_exports(
    tmp_path: Path, app_executable: Path | None
) -> None:
    source = synthetic_dataset(tmp_path)
    original = source.read_bytes()
    data_dir = tmp_path / "application"
    exports = tmp_path / "exports"
    exports.mkdir()
    errors: list[str] = []
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as error:
            pytest.fail(
                "Chromium could not launch. Ensure `uv run playwright install chromium` "
                "has completed and this environment permits browser processes, then retry "
                f"--run-browser. Details: {error}"
            )
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(15_000)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            async with running_app(data_dir, label="initial", executable=app_executable) as url:
                page = await context.new_page()
                capture_browser_errors(page, errors)
                await page.goto(url)
                await page.get_by_role("button", name="Choose local file", exact=True).click()
                dialog = page.get_by_role("dialog")
                await dialog.get_by_label("File path", exact=True).fill(str(source))
                await dialog.get_by_role("button", name="Choose", exact=True).click()
                await expect(page.get_by_text("SET UP YOUR REVIEW", exact=True)).to_be_visible()
                await page.get_by_role("combobox", name="Image columns", exact=True).click()
                await page.get_by_role("option", name="image", exact=True).click()
                await page.keyboard.press("Escape")
                await expect(page.get_by_role("checkbox", name="image", exact=True)).to_have_count(
                    0
                )
                # Quasar checkboxes settle through a server roundtrip, so click then assert.
                await page.get_by_role("checkbox", name="label", exact=True).click()
                await expect(page.get_by_role("checkbox", name="label", exact=True)).to_be_checked()
                await page.get_by_role("checkbox", name="age", exact=True).click()
                await expect(page.get_by_role("checkbox", name="age", exact=True)).to_be_checked()
                await page.get_by_text("Review order", exact=True).click()
                await page.get_by_label("Random seed", exact=True).fill("7")
                await page.screenshot(path=ARTIFACTS / "setup.png", full_page=True)
                await page.get_by_role("button", name="Start review", exact=True).click()
                await image_ready(page)
                confirm = page.get_by_role("button", name="Confirm as Valid", exact=True)
                await expect(confirm).to_be_enabled()
                await page.get_by_role("button", name="Zoom in", exact=True).click()
                await expect(
                    page.get_by_role("button", name="Reset zoom", exact=True)
                ).to_have_text("125%")
                await page.screenshot(path=ARTIFACTS / "review.png", full_page=True)
                await confirm.click()
                await expect(page.get_by_text("Step 2 of 2", exact=False)).to_be_visible()
                await expect(
                    page.get_by_role("button", name="Reset zoom", exact=True)
                ).to_have_text("125%")
                await page.get_by_role("button", name="Fit image", exact=True).click()
                await expect(
                    page.get_by_role("button", name="Reset zoom", exact=True)
                ).to_have_text("100%")
                await page.get_by_role("button", name="Edit value", exact=True).click()
                await page.get_by_label("Replacement value", exact=True).fill("013")
                await expect(page.get_by_text("Edited", exact=True)).to_be_visible()
                await expect(confirm).to_be_disabled()
                await expect(page.get_by_text("Saved locally", exact=True)).to_be_visible()
                await page.screenshot(path=ARTIFACTS / "edited.png", full_page=True)
                await page.get_by_role("button", name="Pause", exact=True).click()
                await expect(
                    page.get_by_role("button", name="Continue", exact=True)
                ).to_be_visible()
                await page.close()

            async with running_app(data_dir, label="reopened", executable=app_executable) as url:
                page = await context.new_page()
                capture_browser_errors(page, errors)
                await page.goto(url)
                await page.get_by_role("button", name="Continue", exact=True).click()
                await image_ready(page)
                await expect(page.get_by_text("Step 2 of 2", exact=False)).to_be_visible()
                await expect(page.get_by_label("Replacement value", exact=True)).to_have_value(
                    "013"
                )
                await expect(page.get_by_text("Edited", exact=True)).to_be_visible()
                await expect(
                    page.get_by_role("button", name="Confirm as Valid", exact=True)
                ).to_be_disabled()
                await page.get_by_role("button", name="Save Correction", exact=True).click()
                await expect(
                    page.get_by_text("Selected fields complete", exact=True)
                ).to_be_visible()
                await page.get_by_role(
                    "checkbox", name="Include transcription-accuracy report", exact=True
                ).click()
                await expect(
                    page.get_by_role(
                        "checkbox", name="Include transcription-accuracy report", exact=True
                    )
                ).to_be_checked()
                await page.screenshot(path=ARTIFACTS / "summary.png", full_page=True)
                await page.get_by_role(
                    "button", name="Export corrected dataset", exact=True
                ).click()
                dialog = page.get_by_role("dialog")
                await dialog.get_by_label("Folder path", exact=True).fill(str(exports))
                await dialog.get_by_role("button", name="Choose", exact=True).click()
                await expect(page.get_by_text("Export saved", exact=True)).to_be_visible()
                await page.screenshot(path=ARTIFACTS / "export.png", full_page=True)
                await page.close()
        finally:
            await context.tracing.stop(path=ARTIFACTS / "workflow.zip")
            await context.close()
            await browser.close()

    assert not errors, f"Browser or HTTP errors occurred: {errors}"
    folders = list(exports.iterdir())
    assert len(folders) == 1
    with (folders[0] / "corrected_dataset.csv").open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.reader(file)) == [
            ["image", "label", "age"],
            ["source.png", "record 0", "013"],
        ]
    with (folders[0] / "review_log.csv").open(encoding="utf-8-sig", newline="") as file:
        decisions = list(csv.DictReader(file))
    assert [decision["status"] for decision in decisions] == ["confirmed", "corrected"]
    assert [decision["src_col"] for decision in decisions] == ["1", "2"]
    assert [decision["valid"] for decision in decisions] == ["true", "false"]
    assert all(decision["seed"] == "7" for decision in decisions)
    assert (folders[0] / "accuracy_report.csv").is_file()
    assert source.read_bytes() == original
