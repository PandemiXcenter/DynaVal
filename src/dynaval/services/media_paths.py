"""Resolve image references without changing the imported source values."""

import os
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from dynaval.domain.errors import MediaError
from dynaval.domain.models import PathMapping


def is_remote_reference(reference: str) -> bool:
    """Only HTTP(S) references are downloaded; Windows drive letters are paths."""
    return urlsplit(reference).scheme.casefold() in {"http", "https"}


def resolve_local_reference(
    reference: str,
    reference_base: str | None,
    mappings: list[PathMapping],
    *,
    platform: str | None = None,
) -> Path:
    """Resolve native paths or the longest explicit Windows component mapping.

    The platform argument exists for deterministic tests of Windows path syntax;
    actual filesystem access always belongs to the host operating system.
    """
    if not reference.strip():
        raise MediaError("This image reference is empty. Repair the source or skip this field.")
    windows = PureWindowsPath(reference)
    host = platform or os.name
    matches: list[tuple[int, PathMapping, tuple[str, ...]]] = []
    for mapping in mappings:
        prefix = PureWindowsPath(mapping.source)
        prefix_parts = tuple(part.casefold() for part in prefix.parts)
        reference_parts = tuple(part.casefold() for part in windows.parts)
        if (
            prefix.is_absolute()
            and prefix_parts
            and reference_parts[: len(prefix_parts)] == prefix_parts
        ):
            matches.append((len(prefix_parts), mapping, windows.parts[len(prefix_parts) :]))
    if matches:
        _, selected, suffix = max(matches, key=lambda item: item[0])
        root = Path(selected.target).expanduser()
        if not root.is_absolute():
            raise MediaError("The mapped image directory must be an absolute local path.")
        try:
            root = root.resolve()
            candidate = root.joinpath(*suffix).resolve()
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise MediaError(
                "The image path escapes its mapped directory. Repair the mapping."
            ) from error
        return candidate
    if windows.drive:
        if host != "nt":
            raise MediaError(
                "This Windows image path needs a prefix mapping to an already mounted directory."
            )
        if not windows.is_absolute():
            raise MediaError("Drive-relative image paths are ambiguous. Use an absolute path.")
        return Path(reference)
    if urlsplit(reference).scheme:
        raise MediaError("Image references must use HTTP(S), local paths, or mounted-share paths.")
    path = Path(reference).expanduser()
    if path.is_absolute():
        return path
    if not reference_base:
        raise MediaError("Set the image base directory to open relative image paths.")
    base = Path(reference_base).expanduser()
    if not base.is_absolute():
        raise MediaError("The image base directory must be an absolute local path.")
    # Relative Windows paths are useful when datasets move from Windows to macOS.
    return base.joinpath(*windows.parts) if "\\" in reference else base / path
