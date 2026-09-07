"""Shared contracts for import, storage, media, exports, and presentation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CellKind = Literal["string", "number", "boolean", "null", "missing", "object", "array"]
Outcome = Literal["confirmed", "corrected", "rejected", "skipped"]
SessionState = Literal["active", "paused", "needs_attention", "completed"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cell(Model):
    text: str
    kind: CellKind = "string"


class ParserOptions(Model):
    format: Literal["csv", "tsv", "json", "jsonl", "ndjson"] | None = None
    encoding: str = "utf-8-sig"
    delimiter: str | None = None


class Dataset(Model):
    name: str
    sha256: str
    columns: list[str]
    rows: list[list[Cell]]
    parser: ParserOptions
    source_path: str | None = None


class PathMapping(Model):
    source: str
    target: str


class SessionSettings(Model):
    reference_columns: list[int]
    validation_columns: list[int]
    allow_corrections: bool = True
    allow_skipping: bool = False
    seed: int = Field(ge=0, le=2**64 - 1)
    reference_base: str | None = None
    path_mappings: list[PathMapping] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_columns(self) -> "SessionSettings":
        for columns in (self.reference_columns, self.validation_columns):
            if not columns or min(columns) < 0 or len(set(columns)) != len(columns):
                raise ValueError("Select distinct reference and validation columns.")
        if set(self.reference_columns) & set(self.validation_columns):
            raise ValueError("Image columns cannot also be validation fields.")
        return self


class ReviewStep(Model):
    step_index: int
    source_row: int
    src_col: int


class Draft(Model):
    step_index: int
    pass_id: int
    text: str
    edit_active: bool = False
    updated_at: str = ""


class MediaRecord(Model):
    reference_col: int
    reference: str
    sha256: str | None = None
    media_id: str | None = None
    path: str | None = None
    error: str | None = None
    changed: bool = False


class Decision(Model):
    step_index: int
    pass_id: int
    status: Outcome
    correction: str | None = None
    reviewed_at: str
    reference_sha256: list[str | None] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_correction(self) -> "Decision":
        if (self.status == "corrected") != (self.correction is not None):
            raise ValueError("Only corrected outcomes contain replacement text.")
        return self

    @property
    def valid(self) -> bool | None:
        if self.status == "skipped":
            return None
        return self.status == "confirmed"


class SessionView(Model):
    session_id: str
    original_source_path: str | None = None
    dataset: Dataset
    settings: SessionSettings
    state: SessionState
    steps: list[ReviewStep]
    decisions: dict[int, Decision] = Field(default_factory=dict)
    pass_id: int = 1
    pass_steps: list[int]
    cursor: int = 0
    draft: Draft | None = None
    created_at: str
    updated_at: str

    @property
    def current_step(self) -> ReviewStep | None:
        if self.cursor >= len(self.pass_steps):
            return None
        return self.steps[self.pass_steps[self.cursor]]

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("confirmed", "corrected", "rejected", "skipped", "pending"), 0)
        for decision in self.decisions.values():
            counts[decision.status] += 1
        counts["pending"] = len(self.steps) - len(self.decisions)
        return counts

    @property
    def resolved(self) -> int:
        counts = self.counts
        return counts["confirmed"] + counts["corrected"]


class SessionSummary(Model):
    session_id: str
    name: str
    sha256: str
    state: SessionState
    seed: int
    total: int
    resolved: int
    updated_at: str
    columns: list[str]
    error: str | None = None
