"""CSV/TSV parsing with exact strings and explicit ambiguous delimiters."""

import csv
import io
from collections.abc import Callable, Iterator

from dynaval.domain.errors import ImportFailure
from dynaval.domain.models import Cell, ParserOptions

DELIMITERS = (",", "\t", ";", "|", "$")


def _records(text: str, delimiter: str) -> Iterator[list[str]]:
    return csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)


def detect_delimiter(text: str) -> str:
    """Offer automatic detection only when one candidate is structurally clear."""
    candidates: list[str] = []
    for delimiter in DELIMITERS:
        try:
            records = (record for record in _records(text, delimiter) if record)
            header = next(records, [])
            if len(header) < 2:
                continue
            widths = [len(record) for _, record in zip(range(50), records, strict=False)]
            if not widths or all(width == len(header) for width in widths):
                candidates.append(delimiter)
        except csv.Error:
            continue
    if len(candidates) != 1:
        raise ImportFailure(
            "The delimiter is ambiguous. Choose a delimiter explicitly, then preview again."
        )
    return candidates[0]


def parse_csv(
    text: str, options: ParserOptions, checkpoint: Callable[[float], None]
) -> tuple[list[str], list[list[Cell]], str]:
    csv.field_size_limit(100 * 1024 * 1024)
    delimiter = options.delimiter
    if delimiter is None:
        delimiter = "\t" if options.format == "tsv" else detect_delimiter(text)
    if len(delimiter) != 1 or delimiter in {'"', "\r", "\n", "\0"}:
        raise ImportFailure("The delimiter must be one character other than a quote or newline.")
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    columns: list[str] | None = None
    rows: list[list[Cell]] = []
    try:
        for record in reader:
            checkpoint(0.3 + 0.65 * stream.tell() / max(len(text), 1))
            if not record:
                continue
            if columns is None:
                if any(not column.strip() for column in record):
                    raise ImportFailure("Column headers cannot be blank. Fix the header and retry.")
                if len(set(record)) != len(record):
                    raise ImportFailure("Duplicate column headers are not supported. Rename them.")
                columns = record
            elif len(record) != len(columns):
                raise ImportFailure(
                    f"Record {len(rows) + 1}, ending at line {reader.line_num}, has "
                    f"{len(record)} cells; expected {len(columns)}. Check the delimiter or record."
                )
            else:
                rows.append([Cell(text=value) for value in record])
    except csv.Error as exc:
        raise ImportFailure(
            f"Malformed CSV near line {reader.line_num}. Check record quoting."
        ) from exc
    if not columns or not rows:
        raise ImportFailure("The dataset needs a nonempty header and at least one data record.")
    return columns, rows, delimiter
