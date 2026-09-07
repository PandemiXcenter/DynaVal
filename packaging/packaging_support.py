"""Build-time metadata and notices; never imported by the application."""

import importlib.metadata
import re
import shutil
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def project_metadata(root: Path) -> dict[str, Any]:
    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def runtime_distributions(requirements: list[str]) -> list[importlib.metadata.Distribution]:
    """Resolve only installed dependencies reachable from project runtime roots."""
    queue = deque((Requirement(text), "") for text in requirements)
    visited: set[tuple[str, str]] = set()
    result: dict[str, importlib.metadata.Distribution] = {}
    while queue:
        requirement, parent_extra = queue.popleft()
        if requirement.marker and not requirement.marker.evaluate({"extra": parent_extra}):
            continue
        name = canonicalize_name(requirement.name)
        distribution = result.get(name) or importlib.metadata.distribution(name)
        result[name] = distribution
        for extra in {"", *requirement.extras}:
            if (name, extra) in visited:
                continue
            visited.add((name, extra))
            queue.extend((Requirement(text), extra) for text in distribution.requires or [])
    return [result[name] for name in sorted(result)]


def collect_notices(requirements: list[str], destination: Path) -> None:
    """Copy installed distribution license notices, including vendored JS notices."""
    destination.mkdir(parents=True, exist_ok=True)
    index = [
        "DynaVal third-party runtime notices",
        "",
        "Generated from installed runtime dependencies for this operating system.",
        "Development and packaging tools are not included in this dependency list.",
        "",
    ]
    for distribution in runtime_distributions(requirements):
        name = canonicalize_name(distribution.metadata["Name"])
        target = destination / name
        target.mkdir(exist_ok=True)
        files: list[str] = []
        for package_path in distribution.files or []:
            parts = package_path.parts
            basename = package_path.name.lower()
            if any(part in {"tests", "test", "__pycache__"} for part in parts) or not any(
                word in basename for word in ("license", "licence", "copying", "notice")
            ):
                continue
            source = Path(str(distribution.locate_file(package_path)))
            if not source.is_file() or source.suffix.lower() in {".py", ".pyc", ".so", ".dll"}:
                continue
            # Flatten paths into bounded names; metadata can include ../ scripts.
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", str(package_path))[-180:]
            if filename in files:
                filename = f"{len(files)}-{filename}"
            shutil.copyfile(source, target / filename)
            files.append(filename)
        license_text = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or ""
        )
        summary = [
            f"{distribution.metadata['Name']} {distribution.version}",
            distribution.metadata.get("Home-page") or "",
            "",
            license_text,
            "",
        ]
        if not files:
            summary.append("No standalone license file was present in the installed distribution.")
        (target / "METADATA.txt").write_text("\n".join(summary), encoding="utf-8")
        index.append(f"{distribution.metadata['Name']} {distribution.version}: {name}/")
    (destination / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(index) + "\n", encoding="utf-8")


def windows_version_text(version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)?", version):
        raise ValueError("Version metadata must start with major.minor.patch.")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None
    numbers = tuple(int(part) for part in match.groups())
    if any(number > 65535 for number in numbers):
        raise ValueError("Windows version components must fit in 16 bits.")
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={(*numbers, 0)!r}, prodvers={(*numbers, 0)!r},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'DynaVal dataset correction'),
    StringStruct('FileVersion', {version!r}),
    StringStruct('InternalName', 'DynaVal'),
    StringStruct('OriginalFilename', 'DynaVal.exe'),
    StringStruct('ProductName', 'DynaVal'),
    StringStruct('ProductVersion', {version!r})
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def prepare_packaging_assets(root: Path) -> Path:
    generated = root / "build" / "packaging-assets"
    notices = generated / "licenses"
    if notices.exists():
        shutil.rmtree(notices)
    metadata = project_metadata(root)
    collect_notices(metadata["dependencies"], notices)
    (generated / "windows-version.txt").write_text(
        windows_version_text(metadata["version"]),
        encoding="utf-8",
    )
    return generated
