"""Replay result contract."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


class FailureCode(str, Enum):
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    CHECKPOINT_TIMEOUT = "CHECKPOINT_TIMEOUT"
    APP_ERROR = "APP_ERROR"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    INPUT_INVALID = "INPUT_INVALID"
    UNEXPECTED_STATE = "UNEXPECTED_STATE"


class Failure(BaseModel):
    step_id: str | None = None
    code: FailureCode
    expected: str | None = None
    observed: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)


class RecoveryEvent(BaseModel):
    id: str
    step_id: str | None = None
    detail: str | None = None


class DriftSignal(BaseModel):
    step_id: str
    strategy_used: str
    preferred_strategy: str | None = None
    detail: str | None = None
    failed_preferred: list[str] = Field(default_factory=list)


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: str
    run_id: str
    outputs: dict[str, Any] | None = None
    outcome: str | None = None
    failure: Failure | None = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drift_signals: list[DriftSignal] = Field(default_factory=list)
    duration_ms: int = 0
    intervention_id: str | None = None
