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


class ReviewEvents(DomainModel):
    schema_: Literal["captain.transcript/1"] = Field(alias="schema")
    id: Token
    operation: Literal["review_events"]
    deadline_unix_ms: Annotated[int, Field(gt=0, le=2**53 - 1)]
    limits: dict[str, int]
    path: PathText
    source_revision: Token | None
    watermark: Token | None
    context_events: Annotated[int, Field(ge=0, le=4096)]


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


class ReviewEventsResult(DomainModel):
    kind: Literal["review_events"]
    source_revision: Token
    canonical_path: PathText
    mtime_ns: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
    record_schema: Literal["cc-transcript.event/1"]
    records_json: Annotated[list[Text], Field(max_length=256)]
    event_start: Annotated[int, Field(ge=0, le=2**53 - 1)]
    fresh_start: Annotated[int, Field(ge=0, le=2**53 - 1)]
    skipped: Annotated[list[Annotated[int, Field(ge=0, le=2**53 - 1)]], Field(max_length=256)]
    watermark: Token
    caught_up: bool
    reset: bool


DOMAIN_REQUEST = TypeAdapter(
    Annotated[
        ReviewEvents | PrepareHookView | PrepareClassifier | SubmitClassifier,
        Field(discriminator="operation"),
    ]
)
DOMAIN_RESULT = TypeAdapter(
    Annotated[ReviewEventsResult | ClassifierResult | ClassificationResult, Field(discriminator="kind")]
)
