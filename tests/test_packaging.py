import csv
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_support() -> types.ModuleType:
    return load_module("packaging_support", "packaging/packaging_support.py")


release = load_module("build_release", "scripts/build_release.py")
demo = load_module("create_demo", "scripts/create_demo.py")


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", ("macos", "arm64")),
        ("Darwin", "x86_64", ("macos", "x86_64")),
        ("Windows", "AMD64", ("windows", "x86_64")),
    ],
)
def test_release_names_match_native_target(
    system: str, machine: str, expected: tuple[str, str]
) -> None:
    assert release.release_target(system, machine) == expected


@pytest.mark.parametrize(
    "system,machine", [("Linux", "x86_64"), ("Windows", "arm64"), ("Darwin", "universal2")]
)
def test_unsupported_build_targets_are_explicit(system: str, machine: str) -> None:
    with pytest.raises(ValueError, match="Build on macOS"):
        release.release_target(system, machine)


def test_windows_archive_contains_entire_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "DynaVal"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "DynaVal.exe").write_bytes(b"synthetic executable")
    (bundle / "_internal/library.dll").write_bytes(b"supporting dependency")
    destination = tmp_path / "release.zip"
    release.archive_bundle(bundle, destination, "Windows")
    with zipfile.ZipFile(destination) as archive:
        assert "DynaVal/DynaVal.exe" in archive.namelist()
        assert archive.read("DynaVal/_internal/library.dll") == b"supporting dependency"
    with pytest.raises(ValueError, match="already exists"):
        release.archive_bundle(bundle, destination, "Windows")
    with pytest.raises(ValueError, match="outside the application bundle"):
        release.archive_bundle(bundle, bundle / "recursive.zip", "Windows")
    assert not (bundle / "recursive.zip").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple's archive utility is native to macOS")
def test_macos_archive_preserves_framework_symlink_and_executable_mode(tmp_path: Path) -> None:
    bundle = tmp_path / "DynaVal.app"
    binary = bundle / "Contents/MacOS/DynaVal"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic executable")
    binary.chmod(0o755)
    (binary.parent / "current").symlink_to("DynaVal")
    destination = tmp_path / "release.zip"
    release.archive_bundle(bundle, destination, "Darwin")
    with zipfile.ZipFile(destination) as archive:
        executable_info = archive.getinfo("DynaVal.app/Contents/MacOS/DynaVal")
        link_info = archive.getinfo("DynaVal.app/Contents/MacOS/current")
        assert executable_info.external_attr >> 16 & stat.S_IXUSR
        assert stat.S_ISLNK(link_info.external_attr >> 16)
        assert archive.read(link_info) == b"DynaVal"


def test_release_checksum_matches_archive_and_repeat_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.1"\n')
    bundle = tmp_path / "dist/DynaVal"
    bundle.mkdir(parents=True)
    (bundle / "DynaVal.exe").write_bytes(b"synthetic executable")
    monkeypatch.setattr(release.platform, "system", lambda: "Windows")
    monkeypatch.setattr(release.platform, "machine", lambda: "AMD64")
    archive = release.build_release(tmp_path, skip_build=True)
    assert archive.name == "DynaVal-0.2.1-windows-x86_64.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archive.with_suffix(".zip.sha256").read_text() == f"{digest}  {archive.name}\n"
    manifest = json.loads(archive.with_suffix(".zip.json").read_text())
    assert manifest["sha256"] == digest
    assert manifest["architecture"] == "x86_64"
    assert "required" in manifest["native_smoke_test"]
    with pytest.raises(ValueError, match="already exist"):
        release.build_release(tmp_path, skip_build=True)


def test_build_failure_never_creates_release_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.1"\n')
    monkeypatch.setattr(release.platform, "system", lambda: "Windows")
    monkeypatch.setattr(release.platform, "machine", lambda: "AMD64")
    calls: list[list[str]] = []

    def failed_run(command: list[str], **_: Any) -> None:
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(release.subprocess, "run", failed_run)
    with pytest.raises(subprocess.CalledProcessError):
        release.build_release(tmp_path)
    assert calls[0][:7] == ["uv", "run", "--locked", "--group", "build", "pyinstaller", "--clean"]
    assert calls[0][-1] == str(tmp_path / "packaging/DynaVal.spec")
    assert not list(tmp_path.rglob("*.zip"))


def test_version_resources_align_with_project() -> None:
    support = load_support()
    metadata = support.project_metadata(ROOT)
    from dynaval import __version__

    assert metadata["version"] == __version__
    resource = support.windows_version_text("2.7.12")
    assert "filevers=(2, 7, 12, 0)" in resource
    assert "'DynaVal.exe'" in resource
    with pytest.raises(ValueError):
        support.windows_version_text("invalid")
    with pytest.raises(ValueError):
        support.windows_version_text("65536.0.0")


def test_runtime_notice_dependency_walk_ignores_dev_and_inactive_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    support = load_support()

    class Distribution:
        def __init__(self, name: str, requires: list[str]) -> None:
            self.metadata = {"Name": name, "License": "Synthetic test license"}
            self.version = "1.0"
            self.requires = requires
            self.files: list[Path] = []

    distributions = {
        "runtime": Distribution(
            "runtime", ["shared", "extra; extra == 'image'", "inactive; python_version < '1'"]
        ),
        "shared": Distribution("shared", ["runtime"]),
        "extra": Distribution("extra", []),
        "pytest": Distribution("pytest", []),
    }
    monkeypatch.setattr(
        support.importlib.metadata, "distribution", lambda name: distributions[name]
    )
    result = support.runtime_distributions(["runtime[image]"])
    assert [distribution.metadata["Name"] for distribution in result] == [
        "extra",
        "runtime",
        "shared",
    ]
    support.collect_notices(["runtime[image]"], tmp_path)
    assert not (tmp_path / "pytest").exists()
    assert "Synthetic test license" in (tmp_path / "runtime/METADATA.txt").read_text()
    assert "shared 1.0" in (tmp_path / "THIRD_PARTY_NOTICES.txt").read_text()


@pytest.mark.parametrize("windows", [False, True])
def test_spec_includes_local_assets_and_selects_native_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows: bool,
) -> None:
    support = load_support()
    monkeypatch.setattr(support, "prepare_packaging_assets", lambda root: tmp_path)
    monkeypatch.setitem(sys.modules, "packaging_support", support)
    compat = types.ModuleType("PyInstaller.compat")
    compat.is_darwin = not windows  # type: ignore[attr-defined]
    compat.is_win = windows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyInstaller.compat", compat)
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = lambda *args, **kwargs: []  # type: ignore[attr-defined]
    hooks.collect_submodules = lambda *args, **kwargs: ["dynaval"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    calls: dict[str, dict[str, Any]] = {}

    def node(name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> types.SimpleNamespace:
            calls[name] = kwargs
            return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        return record

    namespace = {
        "SPECPATH": str(ROOT / "packaging"),
        **{name: node(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")},
    }
    original_path = list(sys.path)
    try:
        exec(
            compile((ROOT / "packaging/DynaVal.spec").read_text(), "DynaVal.spec", "exec"),
            namespace,
        )
    finally:
        sys.path[:] = original_path
    resources = calls["Analysis"]["datas"]
    assert any(path.endswith("styles.css") for path, _ in resources)
    assert any(path.endswith("image_viewer.js") for path, _ in resources)
    assert calls["Analysis"]["pathex"] == [str(ROOT / "src")]
    assert calls["EXE"]["console"] is False
    assert calls["EXE"]["exclude_binaries"] is True
    assert ("BUNDLE" in calls) is not windows
    assert calls["EXE"]["icon"].endswith(".ico" if windows else ".icns")
    if windows:
        assert "clr" in calls["Analysis"]["hiddenimports"]
        assert calls["EXE"]["version"].endswith("windows-version.txt")


def test_icons_have_native_formats_and_multiple_resolutions() -> None:
    with Image.open(ROOT / "packaging/icons/dynaval.png") as icon:
        assert icon.size == (1024, 1024)
        assert icon.getpixel((100, 512)) == (16, 25, 53, 255)
    with Image.open(ROOT / "packaging/icons/dynaval.ico") as icon:
        assert icon.format == "ICO"
        assert (16, 16) in icon.info["sizes"]
        assert (256, 256) in icon.info["sizes"]
    with Image.open(ROOT / "packaging/icons/dynaval.icns") as icon:
        assert icon.format == "ICNS"


def test_demo_is_self_contained_synthetic_and_never_overwrites(tmp_path: Path) -> None:
    destination = tmp_path / "demo"
    csv_path = demo.create_demo(destination)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 2
    assert list(records[0]) == ["image", "name", "age", "label"]
    assert records[0]["age"] == "37"
    for record in records:
        with Image.open(destination / record["image"]) as image:
            assert image.size == (1200, 860)
    assert "38" in (destination / "README.txt").read_text()
    before = csv_path.read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        demo.create_demo(destination)
    assert csv_path.read_bytes() == before
