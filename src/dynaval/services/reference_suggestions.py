"""Advisory reference-column detection without choosing any session settings."""

from pathlib import PureWindowsPath
from urllib.parse import urlsplit

from dynaval.domain.models import Dataset

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _looks_like_reference(value: str) -> bool:
    if not value.strip() or "\n" in value or "\r" in value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme.casefold() in {"http", "https"}:
        return bool(parsed.netloc and parsed.hostname)
    path = PureWindowsPath(value)
    if parsed.scheme and not (path.drive and path.is_absolute()):
        return False
    return path.suffix.casefold() in _IMAGE_SUFFIXES


def suggested_reference_columns(dataset: Dataset) -> list[int]:
    """Suggest source-ordered columns with a reference in the first 25 records.

    Sparse columns are useful reference sources, so one recognizable value is
    enough. The reviewer still explicitly chooses every reference column.
    """
    return [
        column
        for column in range(len(dataset.columns))
        if any(
            row[column].kind == "string" and _looks_like_reference(row[column].text)
            for row in dataset.rows[:25]
        )
    ]
