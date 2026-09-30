"""HTTP client so replay in one process can use the operator console's controller."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from cua.handoff.control import ControlSnapshot, ControlState, Lease


class RemoteHandoffController:
    """Subset of HandoffController used by ReplayEngine, backed by /api/* on the operator."""

    def __init__(self, base_url: str, *, poll_sec: float = 0.5) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_sec = poll_sec
        self.lease: Lease | None = None
        self.state = ControlState.AUTOMATION
        self.captured_actions: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.verify_checkpoint: Any | None = None

    def owns_control(self) -> bool:
        return self.state == ControlState.AUTOMATION

    def snapshot(self) -> ControlSnapshot:
        return ControlSnapshot(state=self.state, lease=self.lease, interventions=[])

    async def _refresh(self) -> ControlSnapshot:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{self.base_url}/api/state")
            r.raise_for_status()
            data = r.json()
        snap = ControlSnapshot.model_validate(data)
        self.state = snap.state
        self.lease = snap.lease
        return snap

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
        payload = {
            "reason": reason,
            "step_id": step_id,
            "capability_id": capability_id,
            "goal": goal,
            "screenshot_path": screenshot_path,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self.base_url}/api/request", json=payload)
            r.raise_for_status()
            data = r.json()
        await self._refresh()
        if self.lease is None:
            self.lease = Lease(
                id=str(data["lease_id"]),
                token=str(data.get("token") or ""),
                owner="",
                reason=reason,
                step_id=step_id,
                expires_at=time.time() + 300,
            )
        self.state = ControlState.AWAITING_HUMAN
        self.history.append({"ts": time.time(), "kind": "awaiting_human", "data": {"reason": reason}})
        return self.lease

    async def wait_until_released(self, poll: float | None = None) -> None:
        interval = poll if poll is not None else self.poll_sec
        while True:
            snap = await self._refresh()
            if snap.state in {ControlState.AUTOMATION, ControlState.ABORTED}:
                self.state = snap.state
                return
            await asyncio.sleep(interval)

    def record_human_action(self, action: dict[str, Any]) -> None:
        self.captured_actions.append({"ts": time.time(), "actor": "human", **action})

    def claim(self, owner: str, token: str | None = None) -> ControlSnapshot:
        raise RuntimeError("claim via operator console UI or POST /api/claim")

    def resume(self, note: str | None = None) -> ControlSnapshot:
        raise RuntimeError("resume via operator console UI or POST /api/resume")

    def verify_ok(self) -> ControlSnapshot:
        raise RuntimeError("verify via operator console UI or POST /api/verify-ok")

    def verify_fail(self, reason: str) -> ControlSnapshot:
        raise RuntimeError("verify-fail via operator console UI")

    def abort(self) -> ControlSnapshot:
        raise RuntimeError("abort via operator console UI")
