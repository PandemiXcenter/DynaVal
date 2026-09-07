"""Versioned deterministic review order, persisted once at session creation."""

import random

from dynaval.domain.models import ReviewStep

QUEUE_VERSION = 1


def create_queue(row_count: int, columns: list[int], seed: int) -> list[ReviewStep]:
    """Shuffle whole rows while keeping selected columns in schema order."""
    if row_count < 1:
        raise ValueError("A review queue requires at least one source row.")
    if not columns or any(column < 0 for column in columns):
        raise ValueError("Select at least one nonnegative source column.")
    if len(set(columns)) != len(columns):
        raise ValueError("Selected source columns must be distinct.")
    if not 0 <= seed <= 2**64 - 1:
        raise ValueError("The seed must be an unsigned 64-bit integer.")
    row_order = list(range(row_count))
    random.Random(seed).shuffle(row_order)
    return [
        ReviewStep(step_index=index, source_row=row, src_col=column)
        for index, (row, column) in enumerate(
            (row, column) for row in row_order for column in sorted(columns)
        )
    ]
