import csv
import json
from pathlib import Path
from typing import Any

import pytest

from dynaval.domain.errors import ExportError
from dynaval.domain.models import (
    Cell,
    Dataset,
    Decision,
    Draft,
    ParserOptions,
    SessionSettings,
    SessionView,
)
from dynaval.domain.queue import create_queue
from dynaval.services import exporting
from dynaval.services.exporting import export_bundle
from dynaval.services.reporting import accuracy_rows


@pytest.fixture
def view() -> SessionView:
    dataset = Dataset(
        name="example.csv",
        sha256="a" * 64,
        columns=["image", "name", "fp.age", "image.2", "untouched"],
        rows=[
            [
                Cell(text="a.png"),
                Cell(text="Søren"),
                Cell(text="012"),
                Cell(text="a2.png"),
                Cell(text="=SUM(1,2)"),
            ],
            [
                Cell(text="b.png"),
                Cell(text="NA"),
                Cell(text=" 15 "),
                Cell(text="b2.png"),
                Cell(text="FALSE"),
            ],
        ],
        parser=ParserOptions(format="csv", delimiter="$"),
    )
    settings = SessionSettings(
        reference_columns=[0, 3], validation_columns=[1, 2], seed=1, allow_skipping=True
    )
    steps = create_queue(2, settings.validation_columns, settings.seed)
    return SessionView(
        session_id="test-session",
        dataset=dataset,
        settings=settings,
        state="active",
        steps=steps,
        pass_steps=list(range(4)),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def decide(
    view: SessionView, statuses: list[str], corrections: dict[int, str] | None = None
) -> SessionView:
    corrections = corrections or {}
    decisions = {
        index: Decision.model_validate(
            {
                "step_index": index,
                "pass_id": 1,
                "status": status,
                "correction": corrections.get(index),
                "reviewed_at": "2026-01-01T01:00:00+00:00",
                "reference_sha256": ["b" * 64, "c" * 64],
            }
        )
        for index, status in enumerate(statuses)
    }
    return view.model_copy(update={"decisions": decisions})


def test_partial_export_preserves_shape_source_order_and_unselected_cells(
    tmp_path: Path,
    view: SessionView,
) -> None:
    assert view.steps[0].source_row == 1  # Review queue is opposite original record order.
    corrected_step = next(
        step.step_index for step in view.steps if step.source_row == 0 and step.src_col == 2
    )
    decision = Decision(
        step_index=corrected_step,
        pass_id=1,
        status="corrected",
        correction="013",
        reviewed_at="now",
    )
    view = view.model_copy(update={"decisions": {corrected_step: decision}})
    before = view.model_dump_json()
    folder = export_bundle(view, tmp_path)
    assert read_csv(folder / "corrected_dataset.csv") == [
        view.dataset.columns,
        ["a.png", "Søren", "013", "a2.png", "=SUM(1,2)"],
        ["b.png", "NA", " 15 ", "b2.png", "FALSE"],
    ]
    assert (folder / "corrected_dataset.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    log = read_dicts(folder / "review_log.csv")
    assert len(log) == 4
    assert [int(row["source_row"]) for row in log] == [1, 1, 0, 0]
    assert [int(row["src_col"]) for row in log] == [1, 2, 1, 2]
    assert log[0]["status"] == "pending"
    assert log[0]["valid"] == log[0]["reviewed_at"] == log[0]["correction"] == ""
    assert json.loads(log[0]["reference_sha256"]) == [None, None]
    assert log[3]["valid"] == "false"
    assert log[3]["effective_value"] == "013"
    references = json.loads(log[3]["reference_sources"])
    assert [item["src_col"] for item in references] == [0, 3]
    assert [item["reference"] for item in references] == ["a.png", "a2.png"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["partial"] is True
    assert manifest["selected_fields_complete"] is False
    assert manifest["all_fields_selected"] is False
    assert manifest["counts"] == {
        "pending": 3,
        "confirmed": 0,
        "corrected": 1,
        "skipped": 0,
        "rejected": 0,
    }
    assert manifest["record_count"] == 2
    assert manifest["unresolved_fields"] == 3
    assert not (folder / "accuracy_report.csv").exists()
    assert view.model_dump_json() == before


def test_outcomes_empty_corrections_and_drafts_are_unambiguous(
    tmp_path: Path, view: SessionView
) -> None:
    view = decide(view, ["confirmed", "corrected", "rejected", "skipped"], {1: ""})
    view = view.model_copy(
        update={"draft": Draft(step_index=3, pass_id=1, text="UNSAVED", edit_active=True)}
    )
    folder = export_bundle(view, tmp_path, include_accuracy=True)
    log = read_dicts(folder / "review_log.csv")
    assert [row["valid"] for row in log] == ["true", "false", "false", ""]
    assert [row["is_corrected"] for row in log] == ["false", "true", "false", "false"]
    assert log[1]["correction"] == log[1]["effective_value"] == ""
    assert log[2]["effective_value"] == "Søren"
    assert log[3]["effective_value"] == "012"
    assert log[3]["reviewed_at"]
    assert read_csv(folder / "corrected_dataset.csv")[2][2] == ""
    assert "UNSAVED" not in (folder / "corrected_dataset.csv").read_text(encoding="utf-8-sig")
    assert json.loads(log[0]["reference_sha256"]) == ["b" * 64, "c" * 64]
    report = read_dicts(folder / "accuracy_report.csv")
    assert len(report) == 3
    assert report[0]["judged_originals"] == "3"
    assert float(report[0]["original_accuracy"]) == pytest.approx(1 / 3)
    assert float(report[0]["correction_completion"]) == 0.5


def test_json_null_missing_manifest_is_sparse_and_excludes_replacements(
    tmp_path: Path,
    view: SessionView,
) -> None:
    rows = [list(row) for row in view.dataset.rows]
    rows[0][1] = Cell(text="null", kind="null")
    rows[0][2] = Cell(text="", kind="missing")
    rows[1][4] = Cell(text="null", kind="null")  # Includes unselected columns.
    rows[1][1] = Cell(text="null", kind="string")
    dataset = view.dataset.model_copy(update={"rows": rows, "parser": ParserOptions(format="json")})
    decision = Decision(
        step_index=2, pass_id=1, status="corrected", correction="", reviewed_at="now"
    )
    view = view.model_copy(update={"dataset": dataset, "decisions": {2: decision}})
    folder = export_bundle(view, tmp_path)
    exported = read_csv(folder / "corrected_dataset.csv")
    assert exported[1][1:3] == ["", ""]
    assert exported[2][1] == "null"
    assert exported[2][4] == ""
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["original_null_missing_cells"] == [
        {"source_row": 0, "src_col": 2, "kind": "missing"},
        {"source_row": 1, "src_col": 4, "kind": "null"},
    ]
    log = read_dicts(folder / "review_log.csv")
    assert log[2]["original_kind"] == "null"
    assert log[2]["original_value"] == "null"
    assert log[2]["effective_kind"] == "string"


def test_repeated_export_creates_new_folders_without_overwriting(
    tmp_path: Path, view: SessionView
) -> None:
    source = tmp_path / "example.csv"
    source.write_text("original bytes")
    view = view.model_copy(
        update={"dataset": view.dataset.model_copy(update={"source_path": str(source)})}
    )
    first = export_bundle(view, tmp_path)
    saved = (first / "corrected_dataset.csv").read_bytes()
    second = export_bundle(view, tmp_path)
    assert first != second
    assert first.exists() and second.exists()
    assert source.read_text() == "original bytes"
    assert (first / "corrected_dataset.csv").read_bytes() == saved
    assert not list(tmp_path.glob(".dynaval-export-*"))


@pytest.mark.parametrize("failure_stage", ["csv", "manifest", "rename"])
def test_failed_export_is_never_partially_published(
    tmp_path: Path,
    view: SessionView,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    previous = export_bundle(view, tmp_path)
    before = view.model_dump_json()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated disk failure")

    if failure_stage == "csv":
        monkeypatch.setattr(exporting, "_write_csv", fail)
    elif failure_stage == "manifest":
        monkeypatch.setattr(exporting, "_write_json", fail)
    else:
        monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(ExportError, match="saved corrections are unchanged"):
        export_bundle(view, tmp_path)
    assert list(tmp_path.iterdir()) == [previous]
    assert view.model_dump_json() == before


def test_export_refuses_session_internals_and_missing_parent(
    tmp_path: Path, view: SessionView
) -> None:
    with pytest.raises(ExportError, match="existing"):
        export_bundle(view, tmp_path / "does-not-exist")
    session = tmp_path / "session"
    session.mkdir()
    (session / "session.sqlite3").touch()
    child = session / "child"
    child.mkdir()
    with pytest.raises(ExportError, match="outside saved session"):
        export_bundle(view, child)
    assert list(child.iterdir()) == []


def test_export_rejects_corrupt_queue_before_writing(tmp_path: Path, view: SessionView) -> None:
    view = view.model_copy(update={"steps": view.steps[:-1]})
    with pytest.raises(ExportError, match="inconsistent"):
        export_bundle(view, tmp_path)
    assert not list(tmp_path.iterdir())


def test_reports_use_latest_decisions_and_separate_accuracy_from_completion(
    view: SessionView,
) -> None:
    view = decide(view, ["confirmed", "corrected", "rejected", "skipped"], {1: "changed"})
    first = accuracy_rows(view)
    assert first[0]["judged_originals"] == 3
    assert first[0]["resolved"] == 2
    decisions = dict(view.decisions)
    decisions[2] = Decision(
        step_index=2, pass_id=2, status="corrected", correction="new", reviewed_at="later"
    )
    second = accuracy_rows(view.model_copy(update={"decisions": decisions, "pass_id": 2}))
    assert second[0]["scheduled"] == 4
    assert second[0]["judged_originals"] == 3
    assert second[0]["original_accuracy"] == first[0]["original_accuracy"]
    assert second[0]["correction_completion"] == 0.75
    assert [row["src_col"] for row in second] == [None, 1, 2]
    assert second[1]["scheduled"] == 2
    assert second[1]["confirmed"] == second[1]["corrected"] == 1


def test_no_verdict_accuracy_is_blank_and_completion_is_zero(
    tmp_path: Path, view: SessionView
) -> None:
    view = decide(view, ["skipped"])
    report = accuracy_rows(view)
    assert report[0]["judged_originals"] == 0
    assert report[0]["original_accuracy"] is None
    assert report[0]["correction_completion"] == 0
    folder = export_bundle(view, tmp_path, include_accuracy=True)
    assert read_dicts(folder / "accuracy_report.csv")[0]["original_accuracy"] == ""


def test_complete_selected_fields_does_not_claim_all_columns_selected(
    tmp_path: Path, view: SessionView
) -> None:
    view = decide(view, ["confirmed"] * 4)
    folder = export_bundle(view, tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["selected_fields_complete"] is True
    assert manifest["all_fields_selected"] is False
    assert manifest["partial"] is False
