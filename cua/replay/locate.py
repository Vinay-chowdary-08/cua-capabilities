"""Multi-strategy locate with exact-one rule."""

from __future__ import annotations

from typing import Any

from cua.artifact.schema import StepTarget
from cua.surface.base import ResolvedHandle, Surface, Target


class LocateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def strategy_label(strat: dict[str, Any]) -> str:
    """Human-readable strategy identity for drift telemetry."""
    by = str(strat.get("by") or "?")
    for key in ("name", "text", "label", "row_header", "value", "css"):
        val = strat.get(key)
        if val:
            if key == "row_header" and strat.get("column_header"):
                return f"{by}:{val}/{strat['column_header']}"
            return f"{by}:{val}"
    if strat.get("name_attr") or (by == "name_attr" and strat.get("name")):
        return f"name_attr:{strat.get('name')}"
    return by


async def locate(
    surface: Surface,
    step_target: StepTarget | None,
    *,
    timeout_ms: int = 10000,
) -> tuple[ResolvedHandle, str, bool, str | None]:
    """Returns (handle, strategy_used, degraded, preferred_strategy).

    degraded=True when a lower-priority (non-first) strategy matched.
    preferred_strategy is the label of strategies[0] (what we wanted first).
    """
    if step_target is None:
        raise LocateError("TARGET_NOT_FOUND", "missing target")
    strategies = step_target.strategies_as_dicts()
    if not strategies:
        raise LocateError("TARGET_NOT_FOUND", "no strategies")

    preferred = strategy_label(strategies[0])
    frame = step_target.frame_name()
    last_err = "none"
    failed: list[str] = []
    for idx, strat in enumerate(strategies):
        target = Target(frame=frame, strategies=[strat])
        label = strategy_label(strat)
        try:
            handle = await surface.resolve(target, timeout_ms=min(3000, timeout_ms))
            degraded = idx > 0
            if degraded:
                handle.meta["failed_preferred"] = failed
            return handle, label, degraded, preferred
        except RuntimeError as e:
            msg = str(e)
            if "TARGET_AMBIGUOUS" in msg:
                raise LocateError("TARGET_AMBIGUOUS", msg) from e
            failed.append(f"{label}:{msg}")
            last_err = msg
            continue
    raise LocateError("TARGET_NOT_FOUND", last_err)
