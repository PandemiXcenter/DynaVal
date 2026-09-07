"""Reference suggestions stay advisory, bounded, and independent of header names."""

import pytest

from dynaval.domain.models import Cell, Dataset, ParserOptions
from dynaval.services.reference_suggestions import suggested_reference_columns


def dataset(rows: list[list[Cell]]) -> Dataset:
    return Dataset(
        name="example.csv",
        sha256="a" * 64,
        columns=[f"column.{index}" for index in range(len(rows[0]))],
        rows=rows,
        parser=ParserOptions(format="csv", delimiter=","),
    )


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.test/image.png?token=abc",
        "HTTP://example.test/image-service?id=3",
        r"\\server\share\scan.JPG",
        r"C:\images\scan.jpeg",
        "/Volumes/source/image.webp",
        "images/a page.PNG",
        r"images\page.jpg",
        "image.png",
    ],
)
def test_recognizes_supported_references_without_mutating_dataset(reference: str) -> None:
    original = dataset([[Cell(text="plain value"), Cell(text=reference), Cell(text="007")]])
    before = original.model_dump()
    assert suggested_reference_columns(original) == [1]
    assert original.model_dump() == before


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "ordinary words",
        "NA",
        "FALSE",
        "https://",
        "http://[invalid",
        "ftp://example.test/image.png",
        "file:///tmp/image.jpg",
        "data:image/png,abc",
        "not an image.pdf",
        "image.png\nextra",
        r"C:drive-relative.jpg",
    ],
)
def test_ignores_non_reference_values(value: str) -> None:
    assert suggested_reference_columns(dataset([[Cell(text=value)]])) == []


def test_ignores_non_text_values_and_handles_sparse_references_in_source_order() -> None:
    original = dataset(
        [
            [
                Cell(text="", kind="missing"),
                Cell(text="null", kind="null"),
                Cell(text="image.png", kind="object"),
            ],
            [
                Cell(text="image.webp"),
                Cell(text="https://example.test/image"),
                Cell(text="0", kind="number"),
            ],
        ]
    )
    assert suggested_reference_columns(original) == [0, 1]


def test_only_inspects_the_first_25_rows() -> None:
    original = dataset(
        [[Cell(text="ordinary text")] for _ in range(25)] + [[Cell(text="image.png")]]
    )
    assert suggested_reference_columns(original) == []
