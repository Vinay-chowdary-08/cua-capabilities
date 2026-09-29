"""Capability artifact schema — Pydantic v2, discriminated unions, validators."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


INPUT_REF = re.compile(r"\{\{inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class CapabilityStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE = "irreversible"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL_ID = "internal_id"
    PII = "pii"
    FINANCIAL = "financial"
    SECRET = "secret"


class FieldSpec(BaseModel):
    type: str = "string"
    pattern: str | None = None
    format: str | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    redact_in_logs: bool = False
    description: str | None = None


class AppInfo(BaseModel):
    vendor: str
    product: str
    compatible_versions: str = "*"


class FrameHint(BaseModel):
    name_hint: str | None = None
    index_fallback: int | None = None


class StrategyByRole(BaseModel):
    by: Literal["role"] = "role"
    role: str
    name: str | None = None
    exact: bool = False


class StrategyByText(BaseModel):
    by: Literal["text"] = "text"
    text: str
    exact: bool = False


class StrategyByCss(BaseModel):
    by: Literal["css"] = "css"
    value: str


class StrategyByLabel(BaseModel):
    by: Literal["label_proximity"] = "label_proximity"
    label: str


class StrategyByTableCell(BaseModel):
    by: Literal["table_cell"] = "table_cell"
    row_header: str
    column_header: str = "Balance"


class StrategyByNameAttr(BaseModel):
    by: Literal["name_attr"] = "name_attr"
    name: str


class StrategyByHeuristic(BaseModel):
    by: Literal["heuristic"] = "heuristic"
    text: str | None = None
    value: str | None = None


Strategy = Annotated[
    Union[
        StrategyByRole,
        StrategyByText,
        StrategyByCss,
        StrategyByLabel,
        StrategyByTableCell,
        StrategyByNameAttr,
        StrategyByHeuristic,
    ],
    Field(discriminator="by"),
]


class VisualAnchor(BaseModel):
    bbox_rel: list[float] | None = None
    crop_hash: str | None = None


class StepTarget(BaseModel):
    ref: str | None = None
    frame: FrameHint | str | None = None
    strategies: list[Strategy] = Field(default_factory=list)
    visual_anchor: VisualAnchor | None = None

    def frame_name(self) -> str | None:
        if isinstance(self.frame, FrameHint):
            return self.frame.name_hint
        if isinstance(self.frame, str):
            return self.frame
        return None

    def strategies_as_dicts(self) -> list[dict[str, Any]]:
        return [s.model_dump() for s in self.strategies]


class ExpectElementVisible(BaseModel):
    kind: Literal["element_visible"] = "element_visible"
    target_ref: str | None = None
    target: StepTarget | None = None


class ExpectTextVisible(BaseModel):
    kind: Literal["text_visible"] = "text_visible"
    text: str
    then: Literal["continue"] | None = None
    outcome: str | None = None


class ExpectUrlMatches(BaseModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str
    outcome: str | None = None


class ExpectAnyOf(BaseModel):
    kind: Literal["any_of"] = "any_of"
    any_of: list[Annotated[Union[ExpectTextVisible, ExpectElementVisible, ExpectUrlMatches], Field(discriminator="kind")]]


Expect = Annotated[
    Union[ExpectElementVisible, ExpectTextVisible, ExpectUrlMatches, ExpectAnyOf],
    Field(discriminator="kind"),
]


class ExtractField(BaseModel):
    target: StepTarget
    parse: Literal["currency", "text", "number"] = "text"


class ActionClick(BaseModel):
    action: Literal["click"] = "click"
    target: StepTarget


class ActionType(BaseModel):
    action: Literal["type"] = "type"
    target: StepTarget
    value: str


class ActionSelect(BaseModel):
    action: Literal["select"] = "select"
    target: StepTarget
    value: str


class ActionPress(BaseModel):
    action: Literal["press"] = "press"
    key: str = "Enter"


class ActionExtract(BaseModel):
    action: Literal["extract"] = "extract"
    extract: dict[str, ExtractField]


class ActionNavigate(BaseModel):
    action: Literal["navigate"] = "navigate"
    url: str


StepAction = Annotated[
    Union[ActionClick, ActionType, ActionSelect, ActionPress, ActionExtract, ActionNavigate],
    Field(discriminator="action"),
]


class CapabilityStep(BaseModel):
    id: str
    intent: str
    action: Literal["click", "type", "select", "press", "extract", "navigate"]
    target: StepTarget | None = None
    value: str | None = None
    key: str | None = None
    url: str | None = None
    extract: dict[str, ExtractField] | None = None
    expect: Expect | None = None
    requires_confirmation: bool = False
    provenance: Literal["agent", "human"] | None = None

    @model_validator(mode="after")
    def _action_fields(self) -> "CapabilityStep":
        if self.action in {"click", "type", "select"} and self.target is None:
            raise ValueError(f"step {self.id}: target required for {self.action}")
        if self.action == "type" and self.value is None:
            raise ValueError(f"step {self.id}: value required for type")
        if self.action == "extract" and not self.extract:
            raise ValueError(f"step {self.id}: extract map required")
        return self


class OutcomeSpec(BaseModel):
    kind: Literal["business", "success"] = "business"
    outputs: dict[str, Any] = Field(default_factory=dict)


class RecoverableHandle(BaseModel):
    action: Literal["click", "press"] | None = None
    target: StepTarget | None = None
    escalate: bool | None = None


class Recoverable(BaseModel):
    id: str
    detect: Expect
    handle: RecoverableHandle
    max_times: int = 3


class SuccessCondition(BaseModel):
    kind: Literal["output_present", "text_visible"] = "output_present"
    output: str | None = None
    text: str | None = None


class Provenance(BaseModel):
    discovered_by: dict[str, Any] | None = None
    goal_text: str | None = None
    reviewed_by: str | None = None


class CapabilityBody(BaseModel):
    id: str
    version: str = "1.0.0"
    title: str
    description: str = ""
    status: CapabilityStatus = CapabilityStatus.DRAFT
    app: AppInfo
    inputs: dict[str, FieldSpec] = Field(default_factory=dict)
    outputs: dict[str, FieldSpec] = Field(default_factory=dict)
    risk: RiskClass = RiskClass.READ_ONLY
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[CapabilityStep] = Field(default_factory=list)
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    recoverables: list[Recoverable] = Field(default_factory=list)
    success: list[SuccessCondition] = Field(default_factory=list)
    provenance: Provenance | None = None


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0"
    capability: CapabilityBody

    @model_validator(mode="after")
    def validate_artifact(self) -> "CapabilityArtifact":
        cap = self.capability
        ids = [s.id for s in cap.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")

        input_names = set(cap.inputs.keys())
        for step in cap.steps:
            if step.value:
                for ref in INPUT_REF.findall(step.value):
                    if ref not in input_names:
                        raise ValueError(f"step {step.id}: unknown input ref {{{{inputs.{ref}}}}}")
            # outcomes referenced in expect
            for outcome in _outcomes_in_expect(step.expect):
                if outcome not in cap.outcomes:
                    raise ValueError(f"step {step.id}: outcome '{outcome}' not declared")

            if cap.risk == RiskClass.IRREVERSIBLE and step.action in {"click", "type", "select"}:
                # irreversible capabilities: any submit/confirm-like step needs flag
                blob = " ".join(
                    filter(
                        None,
                        [
                            step.intent,
                            step.value,
                            *(
                                [
                                    str(getattr(s, "name", None) or getattr(s, "text", None) or "")
                                    for s in (step.target.strategies if step.target else [])
                                ]
                            ),
                        ],
                    )
                ).lower()
                if any(k in blob for k in ("submit", "confirm", "delete", "transfer", "close")):
                    if not step.requires_confirmation:
                        raise ValueError(
                            f"step {step.id}: irreversible risk requires requires_confirmation: true"
                        )
        return self


def _outcomes_in_expect(expect: Expect | None) -> list[str]:
    if expect is None:
        return []
    if isinstance(expect, ExpectAnyOf):
        out: list[str] = []
        for e in expect.any_of:
            out.extend(_outcomes_in_expect(e))
        return out
    outcome = getattr(expect, "outcome", None)
    return [outcome] if outcome else []
