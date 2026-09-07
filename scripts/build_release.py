"""Build the native bundle, archive the full app, and write SHA-256 checksums."""

import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def release_target(system: str, machine: str) -> tuple[str, str]:
    machine = machine.lower()
    if system == "Darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        return "macos", "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    if system == "Windows" and machine in {"x86_64", "amd64"}:
        return "windows", "x86_64"
    raise ValueError(
        "Build on macOS with its native architecture or on Windows x64. Linux is deferred."
    )


def archive_bundle(bundle: Path, destination: Path, system: str) -> None:
    """Use Apple's archival tool for .app symlinks, executable modes and resources."""
    if destination.exists():
        raise ValueError("The release archive already exists. Choose a new release directory.")
    if not bundle.is_dir():
        raise ValueError("The complete application bundle is missing. Run the build first.")
    if destination.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("Choose an archive destination outside the application bundle.")
    if system == "Darwin":
        subprocess.run(
            ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(destination)],
            check=True,
        )
    else:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Unexpected symlink in the Windows bundle; inspect the build.")
                archive.write(path, path.relative_to(bundle.parent))


def build_release(
    root: Path = ROOT,
    *,
    skip_build: bool = False,
    output: Path | None = None,
) -> Path:
    system = platform.system()
    target, architecture = release_target(system, platform.machine())
    with (root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not skip_build:
        subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "--group",
                "build",
                "pyinstaller",
                "--clean",
                "--noconfirm",
                "--distpath",
                str(root / "dist"),
                "--workpath",
                str(root / "build"),
                str(root / "packaging/DynaVal.spec"),
            ],
            cwd=root,
            check=True,
        )
    bundle = root / "dist" / ("DynaVal.app" if target == "macos" else "DynaVal")
    executable = bundle / ("Contents/MacOS/DynaVal" if target == "macos" else "DynaVal.exe")
    if not executable.is_file():
        raise ValueError("The expected native executable is missing from the complete bundle.")
    output = (output or root / "dist" / "releases").resolve()
    if output.is_relative_to(bundle.resolve()):
        raise ValueError("Choose an output directory outside the application bundle.")
    output.mkdir(parents=True, exist_ok=True)
    archive_name = f"DynaVal-{version}-{target}-{architecture}.zip"
    destination = output / archive_name
    checksum_path = output / f"{archive_name}.sha256"
    manifest_path = output / f"{archive_name}.json"
    if any(path.exists() for path in (destination, checksum_path, manifest_path)):
        raise ValueError("Release files already exist. Choose a new --output directory.")
    with tempfile.TemporaryDirectory(prefix=".dynaval-release-", dir=output) as temp:
        staged_archive = Path(temp) / archive_name
        archive_bundle(bundle, staged_archive, system)
        with staged_archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        metadata = {
            "application": "DynaVal",
            "version": version,
            "platform": target,
            "architecture": architecture,
            "archive": archive_name,
            "sha256": digest,
            "created_at": datetime.now(UTC).isoformat(),
            "build_host": platform.platform(),
            "python": platform.python_version(),
            "native_smoke_test": "required; packaging does not perform desktop verification",
            "signing": "No release signing or notarization configured by this script.",
        }
        staged_checksum = Path(temp) / checksum_path.name
        staged_checksum.write_text(f"{digest}  {archive_name}\n", encoding="ascii")
        staged_manifest = Path(temp) / manifest_path.name
        staged_manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        staged_archive.rename(destination)
        staged_checksum.rename(checksum_path)
        staged_manifest.rename(manifest_path)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build", action="store_true", help="Archive an existing native build."
    )
    parser.add_argument(
        "--output", type=Path, help="Directory for ZIP, checksum and build metadata."
    )
    args = parser.parse_args()
    try:
        print(build_release(skip_build=args.skip_build, output=args.output))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release packaging failed: {error}\n")


if __name__ == "__main__":
    main()
