import random

import pytest

from dynaval.domain.models import Cell, Decision
from dynaval.domain.queue import create_queue
from dynaval.domain.review import (
    draft_is_edited,
    editor_text,
    effective_cell,
    is_resolved,
    unresolved_steps,
)


def test_queue_covers_each_coordinate_in_grouped_seeded_order() -> None:
    steps = create_queue(8, [9, 2, 5], 724)
    assert steps == create_queue(8, [5, 9, 2], 724)
    assert [step.step_index for step in steps] == list(range(24))
    assert {(step.source_row, step.src_col) for step in steps} == {
        (row, column) for row in range(8) for column in (2, 5, 9)
    }
    for index in range(0, 24, 3):
        assert [step.src_col for step in steps[index : index + 3]] == [2, 5, 9]
        assert len({step.source_row for step in steps[index : index + 3]}) == 1
    assert steps != create_queue(8, [2, 5, 9], 725)


def test_queue_does_not_mutate_global_random_or_input() -> None:
    columns = [3, 1]
    state = random.getstate()
    create_queue(3, columns, 0)
    assert random.getstate() == state
    assert columns == [3, 1]


@pytest.mark.parametrize(
    "rows,columns,seed",
    [
        (0, [1], 0),
        (-1, [1], 0),
        (2, [], 0),
        (2, [-1], 0),
        (2, [1, 1], 0),
        (2, [1], -1),
        (2, [1], 2**64),
    ],
)
def test_queue_rejects_invalid_inputs(rows: int, columns: list[int], seed: int) -> None:
    with pytest.raises(ValueError):
        create_queue(rows, columns, seed)


@pytest.mark.parametrize(
    "status,valid,resolved",
    [
        ("confirmed", True, True),
        ("corrected", False, True),
        ("rejected", False, False),
        ("skipped", None, False),
    ],
)
def test_decision_semantics(status: str, valid: bool | None, resolved: bool) -> None:
    decision = Decision.model_validate(
        {
            "step_index": 0,
            "pass_id": 1,
            "status": status,
            "correction": "" if status == "corrected" else None,
            "reviewed_at": "now",
        }
    )
    assert decision.valid is valid
    assert is_resolved(decision) is resolved
    assert effective_cell(Cell(text="original"), decision).text == (
        "" if status == "corrected" else "original"
    )


@pytest.mark.parametrize(
    "original,text,active,edited",
    [
        (Cell(text="hello"), "hello", True, False),
        (Cell(text=" hello"), "hello", True, True),
        (Cell(text="hello"), "", True, True),
        (Cell(text=""), "", True, False),
        (Cell(text="", kind="missing"), "", False, False),
        (Cell(text="", kind="missing"), "", True, True),
        (Cell(text="null", kind="null"), "", False, False),
        (Cell(text="null", kind="null"), "", True, True),
    ],
)
def test_exact_edits_and_explicit_empty_replacements(
    original: Cell,
    text: str,
    active: bool,
    edited: bool,
) -> None:
    assert draft_is_edited(original, text, active) is edited
    assert editor_text(original) == ("" if original.kind in {"null", "missing"} else original.text)


def test_unresolved_retains_original_step_ids_and_queue_order() -> None:
    steps = create_queue(3, [2], 5)
    decisions = {
        0: Decision(step_index=0, pass_id=1, status="confirmed", reviewed_at="now"),
        1: Decision(step_index=1, pass_id=2, status="skipped", reviewed_at="later"),
    }
    assert unresolved_steps(steps, decisions) == [1, 2]
    assert not is_resolved(None)


@pytest.mark.parametrize(
    "status,correction",
    [("corrected", None), ("confirmed", ""), ("rejected", "value"), ("skipped", "")],
)
def test_decision_rejects_ambiguous_corrections(status: str, correction: str | None) -> None:
    with pytest.raises(ValueError):
        Decision.model_validate(
            {
                "step_index": 0,
                "pass_id": 1,
                "status": status,
                "correction": correction,
                "reviewed_at": "now",
            }
        )
