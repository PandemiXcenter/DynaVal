"""Exact JSON number and value handling without implicit flattening."""

import io
import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from dynaval.domain.errors import ImportFailure
from dynaval.domain.models import Cell


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImportFailure("Duplicate JSON object keys are not supported. Fix them and retry.")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ImportFailure("Non-finite JSON numbers are not supported.")


def canonical_json(value: Any) -> str:
    """Serialize exact decimal values recursively; strings retain Unicode."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    raise ImportFailure("Unsupported JSON value.")


def _cell(value: Any) -> Cell:
    if isinstance(value, str):
        return Cell(text=value)
    if value is None:
        return Cell(text="null", kind="null")
    if isinstance(value, bool):
        return Cell(text=canonical_json(value), kind="boolean")
    if isinstance(value, (int, Decimal)):
        return Cell(text=canonical_json(value), kind="number")
    if isinstance(value, dict):
        return Cell(text=canonical_json(value), kind="object")
    return Cell(text=canonical_json(value), kind="array")


def _decode(text: str, location: str) -> Any:
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_nonfinite,
            object_pairs_hook=_object,
        )
    except (json.JSONDecodeError, ValueError, InvalidOperation, RecursionError) as exc:
        raise ImportFailure(f"Invalid JSON {location}. Fix the record and retry.") from exc


def parse_json(
    text: str, lines: bool, checkpoint: Callable[[float], None]
) -> tuple[list[str], list[list[Cell]]]:
    records: list[dict[str, Any]] = []
    if lines:
        position = 0
        for line_number, line in enumerate(io.StringIO(text, newline=""), start=1):
            position += len(line)
            checkpoint(0.3 + 0.4 * position / max(len(text), 1))
            if not line.strip():
                continue
            record = _decode(line, f"at line {line_number}")
            if not isinstance(record, dict):
                raise ImportFailure(f"JSONL line {line_number} must contain one object.")
            records.append(record)
    else:
        value = _decode(text, "in the source file")
        if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
            raise ImportFailure("A JSON dataset must be an array of objects.")
        records = value
    columns = list(dict.fromkeys(key for record in records for key in record))
    if not records or not columns:
        raise ImportFailure("The dataset needs at least one record and one column.")
    if any(not column.strip() for column in columns):
        raise ImportFailure("JSON column keys cannot be blank.")
    rows: list[list[Cell]] = []
    try:
        for index, record in enumerate(records):
            checkpoint(0.7 + 0.25 * index / len(records))
            row = [
                _cell(record[key]) if key in record else Cell(text="", kind="missing")
                for key in columns
            ]
            # Unpaired surrogate escapes are legal to the decoder but cannot be
            # represented in the promised UTF-8 exports without losing data.
            for cell in row:
                cell.text.encode("utf-8")
            rows.append(row)
        for column in columns:
            column.encode("utf-8")
    except (UnicodeError, RecursionError) as exc:
        raise ImportFailure("JSON contains unsupported Unicode or excessive nesting.") from exc
    return columns, rows
