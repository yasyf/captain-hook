from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

HOST_SCHEMA = "captain.transcript/1"
MAX_FRAME_BYTES = 1024 * 1024

Token = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Text = Annotated[str, StringConstraints(max_length=MAX_FRAME_BYTES)]
PathText = Annotated[str, StringConstraints(min_length=1, max_length=4096, pattern=r"^/")]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Policy(DomainModel):
    id: Literal["captain-review"]
    version: Literal["1"]


class ReviewRequest(DomainModel):
    schema_: Literal["captain.transcript/1"] = Field(alias="schema")
    id: Token
    deadline_unix_ms: Annotated[int, Field(gt=0, le=2**53 - 1)]
    limits: dict[str, int]
    view: dict[str, Any]
    policy: Policy


class PrepareReview(ReviewRequest):
    operation: Literal["prepare_review"]
    min_confidence: Annotated[float, Field(ge=0, le=1)]
    min_confidence_fix: Annotated[float, Field(ge=0, le=1)]
    repo_key: Text | None = None
    decision_log_path: PathText | None = None
    claude_config_dir: PathText | None = None


class PrepareHookView(DomainModel):
    schema_: Literal["captain.transcript/1"] = Field(alias="schema")
    id: Token
    operation: Literal["prepare_hook_view"]
    deadline_unix_ms: Annotated[int, Field(gt=0, le=2**53 - 1)]
    limits: dict[str, int]
    view: dict[str, Any]
    cwd: PathText | None
    droid: bool


class PrepareClassifier(DomainModel):
    schema_: Literal["captain.transcript/1"] = Field(alias="schema")
    id: Token
    operation: Literal["prepare_classifier"]
    deadline_unix_ms: Annotated[int, Field(gt=0, le=2**53 - 1)]
    limits: dict[str, int]
    handle: dict[str, str]
    classifier: dict[str, str]


class SubmitClassifier(DomainModel):
    schema_: Literal["captain.transcript/1"] = Field(alias="schema")
    id: Token
    operation: Literal["submit_classifier"]
    cursor: Token
    labels: Annotated[list[bool], Field(max_length=256)]


class ClassifierResult(DomainModel):
    kind: Literal["classifier"]
    classifier: dict[str, str]


class ClassificationResult(DomainModel):
    kind: Literal["classification"]
    record_schema: Literal["cc-transcript.event/1"]
    records_json: Annotated[list[Text], Field(max_length=256)]
    event_start: Annotated[int, Field(ge=0, le=2**53 - 1)]


class PrepareCorrections(ReviewRequest):
    operation: Literal["prepare_corrections"]
    anchors: Annotated[list[dict[str, str | None]], Field(max_length=256)]
    feedback: Annotated[list[Text], Field(max_length=256)]
    repo: PathText | None


class CorrectionChoice(DomainModel):
    pair_id: Token
    overlap: Annotated[float, Field(ge=0)]
    correction_json: Text


class PreparedCorrection(DomainModel):
    anchor: dict[str, str | None]
    prompt: Text
    choices: Annotated[list[CorrectionChoice], Field(max_length=12)]


class CorrectionsResult(DomainModel):
    kind: Literal["corrections"]
    corrections: Annotated[list[PreparedCorrection], Field(max_length=256)]


class ReviewResult(DomainModel):
    kind: Literal["review"]
    canonical_path: PathText
    mtime_ns: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
    repo_key: Text | None
    cwd: PathText | None
    disposition: Literal["eligible", "reviewer_session", "no_repo"]
    candidates_json: Annotated[list[Text], Field(max_length=256)]


DOMAIN_REQUEST = TypeAdapter(
    Annotated[
        PrepareReview | PrepareCorrections | PrepareHookView | PrepareClassifier | SubmitClassifier,
        Field(discriminator="operation"),
    ]
)
DOMAIN_RESULT = TypeAdapter(
    Annotated[CorrectionsResult | ReviewResult | ClassifierResult | ClassificationResult, Field(discriminator="kind")]
)
