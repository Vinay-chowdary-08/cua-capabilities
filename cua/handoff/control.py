"""Control state machine with lease tokens for human handoff."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ControlState(str, Enum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    VERIFYING = "verifying"
    ABORTED = "aborted"


class Lease(BaseModel):
    id: str
    token: str
    owner: str
    reason: str
    step_id: str | None = None
    expires_at: float


class Intervention(BaseModel):
    id: str
    capability_id: str | None = None
    goal: str | None = None
    step_id: str | None = None
    intent: str | None = None
    reason: str
    screenshot_path: str | None = None
    state: ControlState = ControlState.AWAITING_HUMAN
    created_at: float = Field(default_factory=time.time)
    note: str | None = None


class ControlSnapshot(BaseModel):
    state: ControlState
    lease: Lease | None = None
    interventions: list[Intervention] = Field(default_factory=list)


class HandoffController:
    def __init__(self, lease_seconds: int = 300) -> None:
        self.state = ControlState.AUTOMATION
        self.lease: Lease | None = None
        self.lease_seconds = lease_seconds
        self.interventions: list[Intervention] = []
        self.history: list[dict[str, Any]] = []
        self.captured_actions: list[dict[str, Any]] = []
        self._event = asyncio.Event()
        self._event.set()
        # Optional verify hook set by replay: returns True if checkpoint holds.
        self.verify_checkpoint: Any | None = None

    def owns_control(self) -> bool:
        self._expire_if_needed()
        return self.state == ControlState.AUTOMATION

    def snapshot(self) -> ControlSnapshot:
        self._expire_if_needed()
        return ControlSnapshot(
            state=self.state,
            lease=self.lease,
            interventions=list(self.interventions[-20:]),
        )

    async def request(
        self,
        reason: str,
        *,
        step_id: str | None = None,
        capability_id: str | None = None,
        goal: str | None = None,
        intent: str | None = None,
        screenshot_path: str | None = None,
    ) -> Lease:
        lease = Lease(
            id=uuid4().hex[:10],
            token=uuid4().hex,
            owner="",
            reason=reason,
            step_id=step_id,
            expires_at=time.time() + self.lease_seconds,
        )
        self.lease = lease
        self.state = ControlState.AWAITING_HUMAN
        intervention = Intervention(
            id=lease.id,
            capability_id=capability_id,
            goal=goal,
            step_id=step_id,
            intent=intent,
            reason=reason,
            screenshot_path=screenshot_path,
            state=ControlState.AWAITING_HUMAN,
        )
        self.interventions.append(intervention)
        self._note("awaiting_human", {"reason": reason, "id": lease.id})
        self._event.clear()
        return lease

    def claim(self, owner: str, token: str | None = None) -> ControlSnapshot:
        if self.state != ControlState.AWAITING_HUMAN:
            raise RuntimeError(f"cannot claim in state {self.state.value}")
        if self.lease and token and token != self.lease.token:
            raise RuntimeError("invalid lease token")
        if self.lease:
            self.lease.owner = owner
        self.state = ControlState.HUMAN
        self._update_intervention_state(ControlState.HUMAN)
        self._note("claimed", {"owner": owner})
        return self.snapshot()

    def resume(self, note: str | None = None) -> ControlSnapshot:
        if self.state not in {ControlState.HUMAN, ControlState.AWAITING_HUMAN}:
            raise RuntimeError(f"cannot resume in state {self.state.value}")
        self.state = ControlState.VERIFYING
        self._update_intervention_state(ControlState.VERIFYING, note=note)
        self._note("verifying", {"note": note})
        return self.snapshot()

    def verify_ok(self) -> ControlSnapshot:
        self.state = ControlState.AUTOMATION
        self.lease = None
        self._update_intervention_state(ControlState.AUTOMATION)
        self._note("automation_resumed", {})
        self._event.set()
        return self.snapshot()

    def verify_fail(self, reason: str) -> ControlSnapshot:
        self.state = ControlState.AWAITING_HUMAN
        self._update_intervention_state(ControlState.AWAITING_HUMAN, note=reason)
        self._note("verify_mismatch", {"reason": reason})
        self._event.clear()
        return self.snapshot()

    def abort(self) -> ControlSnapshot:
        self.state = ControlState.ABORTED
        self._update_intervention_state(ControlState.ABORTED)
        self._note("aborted", {})
        self._event.set()
        return self.snapshot()

    async def wait_until_released(self, poll: float = 0.5) -> None:
        while True:
            self._expire_if_needed()
            if self.state in {ControlState.AUTOMATION, ControlState.ABORTED}:
                return
            try:
                await asyncio.wait_for(self._event.wait(), timeout=poll)
            except asyncio.TimeoutError:
                continue

    def record_human_action(self, action: dict[str, Any]) -> None:
        self.captured_actions.append({"ts": time.time(), "actor": "human", **action})
        self._note("human_action", action)

    def _update_intervention_state(self, state: ControlState, note: str | None = None) -> None:
        if self.interventions:
            self.interventions[-1].state = state
            if note is not None:
                self.interventions[-1].note = note

    def _expire_if_needed(self) -> None:
        if self.lease and time.time() > self.lease.expires_at:
            if self.state in {
                ControlState.HUMAN,
                ControlState.AWAITING_HUMAN,
                ControlState.VERIFYING,
            }:
                self.state = ControlState.ABORTED
                self._note("lease_expired", {})
                self._event.set()

    def _note(self, kind: str, data: dict[str, Any]) -> None:
        self.history.append({"ts": time.time(), "kind": kind, "data": data})


# Process-local singleton — operator console and in-process replay share this.
SHARED_CONTROLLER = HandoffController()


def get_shared_controller() -> HandoffController:
    return SHARED_CONTROLLER
