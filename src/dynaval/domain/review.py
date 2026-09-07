"""Pure review semantics shared by persistence, exports, and presentation."""

from dynaval.domain.models import Cell, Decision, ReviewStep


def is_resolved(decision: Decision | None) -> bool:
    return decision is not None and decision.status in {"confirmed", "corrected"}


def editor_text(original: Cell) -> str:
    """Null and missing values start with an explicitly inactive blank editor."""
    return "" if original.kind in {"null", "missing"} else original.text


def draft_is_edited(original: Cell, text: str, edit_active: bool) -> bool:
    if original.kind in {"null", "missing"}:
        return edit_active
    return text != original.text


def effective_cell(original: Cell, decision: Decision | None) -> Cell:
    if decision is not None and decision.status == "corrected":
        assert decision.correction is not None
        return Cell(text=decision.correction, kind="string")
    return original


def unresolved_steps(steps: list[ReviewStep], decisions: dict[int, Decision]) -> list[int]:
    """Return original queue IDs; never reshuffle or manufacture new steps."""
    return [step.step_index for step in steps if not is_resolved(decisions.get(step.step_index))]
