"""Validate the release tag and complete download set before creating a draft."""

import argparse
import ast
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (("macos", "arm64"), ("windows", "x86_64"))


def release_version(root: Path, tag: str | None = None) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a numeric major.minor.patch project version for desktop builds.")
    module = ast.parse((root / "src/dynaval/__init__.py").read_text(encoding="utf-8"))
    declared = [
        ast.literal_eval(statement.value)
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        )
    ]
    if declared != [version]:
        raise ValueError("pyproject.toml and src/dynaval/__init__.py versions must match.")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag must be v{version}; received {tag!r}.")
    return version


def verify_downloads(directory: Path, version: str) -> None:
    """Require both platform archives and matching checksum/metadata sidecars."""
    expected_names = {
        f"DynaVal-{version}-{system}-{arch}.zip{suffix}"
        for system, arch in TARGETS
        for suffix in ("", ".sha256", ".json")
    }
    if {path.name for path in directory.iterdir()} != expected_names:
        raise ValueError("Expected exactly the macOS arm64 and Windows x64 ZIPs and sidecars.")
    for system, arch in TARGETS:
        archive = directory / f"DynaVal-{version}-{system}-{arch}.zip"
        with archive.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if archive.with_suffix(".zip.sha256").read_text(encoding="ascii").strip() != (
            f"{checksum}  {archive.name}"
        ):
            raise ValueError(f"Checksum mismatch for {archive.name}.")
        metadata = json.loads(archive.with_suffix(".zip.json").read_text(encoding="utf-8"))
        expected = {
            "application": "DynaVal",
            "version": version,
            "platform": system,
            "architecture": arch,
            "archive": archive.name,
            "sha256": checksum,
        }
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            raise ValueError(f"Build metadata mismatch for {archive.name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    commands.add_parser("verify").add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        tag = (
            os.environ.get("GITHUB_REF_NAME", "")
            if os.environ.get("GITHUB_REF_TYPE") == "tag"
            else None
        )
        version = release_version(ROOT, tag)
        if args.command == "verify":
            verify_downloads(args.directory, version)
        print(f"Verified DynaVal {version}")
    except (ValueError, OSError, KeyError, SyntaxError) as error:
        parser.exit(1, f"Release validation failed: {error}\n")


if __name__ == "__main__":
    main()
