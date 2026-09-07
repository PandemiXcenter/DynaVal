"""Bounded file loading and a single import entry point."""

import hashlib
from collections.abc import Callable
from pathlib import Path

from dynaval.domain.errors import ImportFailure
from dynaval.domain.models import Dataset, ParserOptions
from dynaval.importers.csv import parse_csv
from dynaval.importers.json import parse_json

MAX_DATASET_BYTES = 100 * 1024 * 1024
Progress = Callable[[float], None]
Cancel = Callable[[], bool]


def parse_dataset(
    path: Path,
    options: ParserOptions | None = None,
    *,
    name: str | None = None,
    cancel: Cancel | None = None,
    progress: Progress | None = None,
) -> Dataset:
    """Parse a file without guessing types or changing its bytes.

    Call from a worker. Progress is a fraction from zero to one and cancellation
    is checked both while loading bytes and while parsing logical records.
    """
    options = options or ParserOptions()
    source_name = name or path.name
    extension = Path(source_name).suffix.lower().lstrip(".")
    format_name = options.format or extension
    if format_name not in {"csv", "tsv", "json", "jsonl", "ndjson"}:
        raise ImportFailure("Choose CSV, TSV, JSON, JSONL, or NDJSON as the file format.")

    def checkpoint(value: float) -> None:
        if cancel is not None and cancel():
            raise ImportFailure("Import canceled. No session was created.")
        if progress is not None:
            progress(value)

    checkpoint(0.0)
    try:
        size = path.stat().st_size
        if size > MAX_DATASET_BYTES:
            raise ImportFailure("The dataset exceeds the 100 MiB import limit.")
        blocks: list[bytes] = []
        length = 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                length += len(block)
                if length > MAX_DATASET_BYTES:
                    raise ImportFailure("The dataset exceeds the 100 MiB import limit.")
                blocks.append(block)
                checkpoint(min(0.3, length / max(size, 1) * 0.3))
        raw = b"".join(blocks)
    except OSError as exc:
        raise ImportFailure("Cannot read the dataset. Check its location and file access.") from exc
    try:
        text = raw.decode(options.encoding)
    except (UnicodeError, LookupError) as exc:
        raise ImportFailure(
            "Cannot decode the dataset. Select its text encoding and retry."
        ) from exc
    parser = options.model_copy(update={"format": format_name})
    if format_name in {"csv", "tsv"}:
        columns, rows, delimiter = parse_csv(text, parser, checkpoint)
        parser = parser.model_copy(update={"delimiter": delimiter})
    else:
        columns, rows = parse_json(text, format_name != "json", checkpoint)
    checkpoint(1.0)
    return Dataset(
        name=source_name,
        sha256=hashlib.sha256(raw).hexdigest(),
        columns=columns,
        rows=rows,
        parser=parser,
        source_path=str(path.resolve()),
    )
