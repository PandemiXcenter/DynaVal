"""Publish consistent correction bundles without changing source or prior output."""

import csv
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dynaval.domain.errors import ExportError
from dynaval.domain.models import Cell, SessionView
from dynaval.domain.review import effective_cell
from dynaval.services.reporting import ACCURACY_COLUMNS, accuracy_rows

EXPORT_VERSION = 1
REVIEW_COLUMNS = (
    "schema_version",
    "session_id",
    "dataset_sha256",
    "step_index",
    "source_row",
    "src_col",
    "src_col_name",
    "original_value",
    "original_kind",
    "status",
    "valid",
    "is_corrected",
    "correction",
    "effective_value",
    "effective_kind",
    "reference_sources",
    "reference_sha256",
    "reviewed_at",
    "seed",
)


def _boolean(value: bool | None) -> str:
    return "" if value is None else str(value).lower()


def _csv_text(cell: Cell) -> str:
    return "" if cell.kind in {"missing", "null"} else cell.text


def review_rows(view: SessionView) -> Iterable[dict[str, Any]]:
    for step in view.steps:
        original = view.dataset.rows[step.source_row][step.src_col]
        decision = view.decisions.get(step.step_index)
        effective = effective_cell(original, decision)
        references = [
            {
                "src_col": column,
                "src_col_name": view.dataset.columns[column],
                "reference": view.dataset.rows[step.source_row][column].text,
            }
            for column in sorted(view.settings.reference_columns)
        ]
        hashes = decision.reference_sha256 if decision else []
        yield {
            "schema_version": EXPORT_VERSION,
            "session_id": view.session_id,
            "dataset_sha256": view.dataset.sha256,
            "step_index": step.step_index,
            "source_row": step.source_row,
            "src_col": step.src_col,
            "src_col_name": view.dataset.columns[step.src_col],
            "original_value": original.text,
            "original_kind": original.kind,
            "status": decision.status if decision else "pending",
            "valid": _boolean(decision.valid if decision else None),
            "is_corrected": _boolean(decision is not None and decision.status == "corrected"),
            "correction": decision.correction if decision else None,
            "effective_value": effective.text,
            "effective_kind": effective.kind,
            "reference_sources": json.dumps(references, ensure_ascii=False, separators=(",", ":")),
            "reference_sha256": json.dumps(
                [
                    hashes[index] if index < len(hashes) else None
                    for index in range(len(references))
                ],
                separators=(",", ":"),
            ),
            "reviewed_at": decision.reviewed_at if decision else "",
            "seed": view.settings.seed,
        }


def _write_csv(path: Path, header: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _check_snapshot(view: SessionView) -> None:
    """Fail closed if an inconsistent snapshot could silently export wrong cells."""
    width = len(view.dataset.columns)
    if not width or not view.dataset.rows or any(len(row) != width for row in view.dataset.rows):
        raise ExportError("The saved dataset shape is inconsistent. Reopen the saved session.")
    expected = {
        (row, column)
        for row in range(len(view.dataset.rows))
        for column in view.settings.validation_columns
    }
    actual = {(step.source_row, step.src_col) for step in view.steps}
    if (
        actual != expected
        or len(actual) != len(view.steps)
        or [step.step_index for step in view.steps] != list(range(len(view.steps)))
        or any(
            column >= width
            for column in (*view.settings.reference_columns, *view.settings.validation_columns)
        )
        or any(
            index < 0 or index >= len(view.steps) or decision.step_index != index
            for index, decision in view.decisions.items()
        )
    ):
        raise ExportError("The saved review queue is inconsistent. Reopen the saved session.")


def _check_parent(parent: Path, view: SessionView) -> Path:
    parent = parent.expanduser().resolve()
    if not parent.is_dir():
        raise ExportError("Choose an existing writable folder for the export.")
    for candidate in (parent, *parent.parents):
        if (candidate / "session.sqlite3").exists():
            raise ExportError("Choose an export folder outside saved session data.")
    # Snapshot and cache folders may sit together; prohibit exporting into a
    # known session subtree even if its database has gone missing.
    if view.dataset.source_path:
        source_parent = Path(view.dataset.source_path).resolve().parent
        if source_parent.name == view.session_id and parent.is_relative_to(source_parent):
            raise ExportError("Choose an export folder outside saved session data.")
    return parent


def export_bundle(view: SessionView, parent: Path, include_accuracy: bool = False) -> Path:
    """Export one already consistent SessionView; success returns its new folder."""
    _check_snapshot(view)
    parent = _check_parent(parent, view)
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".dynaval-export-", dir=parent))
        correction_by_cell = {
            (step.source_row, step.src_col): view.decisions[step.step_index]
            for step in view.steps
            if step.step_index in view.decisions
            and view.decisions[step.step_index].status == "corrected"
        }
        _write_csv(
            staging / "corrected_dataset.csv",
            view.dataset.columns,
            (
                [
                    _csv_text(effective_cell(cell, correction_by_cell.get((row_index, column))))
                    for column, cell in enumerate(row)
                ]
                for row_index, row in enumerate(view.dataset.rows)
            ),
        )
        _write_csv(
            staging / "review_log.csv",
            REVIEW_COLUMNS,
            ([row[column] for column in REVIEW_COLUMNS] for row in review_rows(view)),
        )
        files = ["corrected_dataset.csv", "review_log.csv", "manifest.json"]
        if include_accuracy:
            _write_csv(
                staging / "accuracy_report.csv",
                ACCURACY_COLUMNS,
                ([row[column] for column in ACCURACY_COLUMNS] for row in accuracy_rows(view)),
            )
            files.append("accuracy_report.csv")
        exported_at = datetime.now(UTC)
        null_cells = [
            {"source_row": row_index, "src_col": column, "kind": cell.kind}
            for row_index, row in enumerate(view.dataset.rows)
            for column, cell in enumerate(row)
            if cell.kind in {"missing", "null"} and (row_index, column) not in correction_by_cell
        ]
        manifest = {
            "schema_version": EXPORT_VERSION,
            "session_id": view.session_id,
            "dataset_sha256": view.dataset.sha256,
            "dataset_name": view.dataset.name,
            "exported_at": exported_at.isoformat(),
            "seed": view.settings.seed,
            "validation_columns": sorted(view.settings.validation_columns),
            "reference_columns": sorted(view.settings.reference_columns),
            "parser": view.dataset.parser.model_dump(),
            "columns": view.dataset.columns,
            "record_count": len(view.dataset.rows),
            "scheduled_fields": len(view.steps),
            "counts": view.counts,
            "resolved_fields": view.resolved,
            "unresolved_fields": len(view.steps) - view.resolved,
            "selected_fields_complete": view.resolved == len(view.steps),
            "all_fields_selected": set(view.settings.validation_columns)
            == (set(range(len(view.dataset.columns))) - set(view.settings.reference_columns)),
            "partial": view.resolved != len(view.steps),
            "original_null_missing_cells": null_cells,
            "files": files,
            "csv_encoding": "utf-8-sig",
            "csv_delimiter": ",",
            "value_note": "CSV preserves literal text. Import value columns as text in "
            "spreadsheet software; formula-like strings are not modified.",
        }
        _write_json(staging / "manifest.json", manifest)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(view.dataset.name).stem).strip("-")[:40]
        while True:
            destination = parent / (
                f"{stem or 'dataset'}-corrected-{exported_at:%Y%m%d-%H%M%S}-{uuid4().hex[:12]}"
            )
            if not destination.exists():
                break
        staging.rename(destination)
        return destination
    except (OSError, ValueError, TypeError) as exc:
        raise ExportError(
            "Export failed. Check folder access and free space, then retry. "
            "Your saved corrections are unchanged."
        ) from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
