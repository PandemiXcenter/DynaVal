"""Secondary transcription metrics, separate from correction completion."""

from typing import Any

from dynaval.domain.models import ReviewStep, SessionView

STATUSES = ("confirmed", "corrected", "rejected", "skipped", "pending")
ACCURACY_COLUMNS = (
    "schema_version",
    "session_id",
    "dataset_sha256",
    "scope",
    "src_col",
    "src_col_name",
    "scheduled",
    "confirmed",
    "corrected",
    "rejected",
    "skipped",
    "pending",
    "judged_originals",
    "original_accuracy",
    "resolved",
    "correction_completion",
)


def _counts(view: SessionView, steps: list[ReviewStep]) -> dict[str, int]:
    result = dict.fromkeys(STATUSES, 0)
    for step in steps:
        decision = view.decisions.get(step.step_index)
        result[decision.status if decision else "pending"] += 1
    return result


def accuracy_rows(view: SessionView) -> list[dict[str, Any]]:
    """One overall row and one row per selected column, using latest outcomes."""
    rows: list[dict[str, Any]] = []
    scopes: list[int | None] = [None, *sorted(view.settings.validation_columns)]
    for column in scopes:
        steps = [step for step in view.steps if column is None or step.src_col == column]
        counts = _counts(view, steps)
        judged = counts["confirmed"] + counts["corrected"] + counts["rejected"]
        resolved = counts["confirmed"] + counts["corrected"]
        rows.append(
            {
                "schema_version": 1,
                "session_id": view.session_id,
                "dataset_sha256": view.dataset.sha256,
                "scope": "overall" if column is None else "column",
                "src_col": column,
                "src_col_name": "" if column is None else view.dataset.columns[column],
                "scheduled": len(steps),
                **counts,
                "judged_originals": judged,
                "original_accuracy": counts["confirmed"] / judged if judged else None,
                "resolved": resolved,
                "correction_completion": resolved / len(steps) if steps else None,
            }
        )
    return rows
