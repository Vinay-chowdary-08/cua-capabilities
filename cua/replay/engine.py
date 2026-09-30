"""Deterministic capability replay — no LLM, no cua.agent imports."""

from __future__ import annotations

import re
import time
from typing import Any
from uuid import uuid4

from cua.artifact.schema import CapabilityArtifact, CapabilityStep, Expect, ExpectAnyOf
from cua.artifact.store import render_template
from cua.evidence.logger import EvidenceLogger
from cua.replay.detectors import DetectKind, detect_observation
from cua.replay.locate import LocateError, locate
from cua.replay.result import (
    DriftSignal,
    Failure,
    FailureCode,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
)
from cua.safety.policy import Policy, PolicyRequire
from cua.surface.base import Action, ActionKind, Surface, Target


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        *,
        evidence: EvidenceLogger | None = None,
        allow_handoff: bool = False,
        handoff: Any | None = None,
        confirm_irreversible: bool = False,
        step_timeout_ms: int = 8000,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.allow_handoff = allow_handoff
        self.handoff = handoff
        self.confirm_irreversible = confirm_irreversible
        self.step_timeout_ms = step_timeout_ms
        self._recovery_counts: dict[str, int] = {}
        self._intervention_id: str | None = None

    async def run(
        self,
        artifact: CapabilityArtifact,
        inputs: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> ReplayResult:
        started = time.perf_counter()
        run_id = run_id or uuid4().hex[:12]
        cap = artifact.capability
        self._intervention_id = None

        # Validate inputs before touching the browser
        invalid = _validate_inputs(cap.inputs, inputs)
        if invalid:
            return ReplayResult(
                status=ReplayStatus.FAILED,
                capability_id=cap.id,
                capability_version=cap.version,
                run_id=run_id,
                failure=Failure(code=FailureCode.INPUT_INVALID, observed=invalid),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        outputs: dict[str, Any] = {}
        recoveries: list[RecoveryEvent] = []
        drift: list[DriftSignal] = []

        if self.evidence:
            self.evidence.log_event(
                "replay_start",
                {"capability_id": cap.id, "inputs_keys": list(inputs.keys())},
                actor="automation",
            )

        try:
            for step in cap.steps:
                # Global detectors / recoverables
                det_result = await self._check_global(cap, step, recoveries)
                if isinstance(det_result, ReplayResult):
                    det_result.duration_ms = int((time.perf_counter() - started) * 1000)
                    det_result.recoveries = recoveries
                    det_result.drift_signals = drift
                    det_result.intervention_id = det_result.intervention_id or self._intervention_id
                    return det_result

                step_result = await self._run_step(artifact, step, inputs, outputs, recoveries, drift)
                if step_result is not None:
                    step_result.duration_ms = int((time.perf_counter() - started) * 1000)
                    step_result.recoveries = recoveries
                    step_result.drift_signals = drift
                    step_result.intervention_id = step_result.intervention_id or self._intervention_id
                    if self.evidence:
                        self.evidence.log_event("replay_end", step_result.model_dump(), actor="automation")
                        self.evidence.write_result(step_result.model_dump())
                    return step_result

            # Success conditions
            for cond in cap.success:
                if cond.kind == "output_present" and cond.output and cond.output not in outputs:
                    result = ReplayResult(
                        status=ReplayStatus.FAILED,
                        capability_id=cap.id,
                        capability_version=cap.version,
                        run_id=run_id,
                        outputs=outputs or None,
                        failure=Failure(
                            code=FailureCode.UNEXPECTED_STATE,
                            expected=f"output {cond.output}",
                            observed=str(list(outputs.keys())),
                        ),
                        recoveries=recoveries,
                        drift_signals=drift,
                        intervention_id=self._intervention_id,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                    if self.evidence:
                        self.evidence.write_result(result.model_dump())
                    return result

            result = ReplayResult(
                status=ReplayStatus.SUCCESS,
                capability_id=cap.id,
                capability_version=cap.version,
                run_id=run_id,
                outputs=outputs or None,
                recoveries=recoveries,
                drift_signals=drift,
                intervention_id=self._intervention_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            if self.evidence:
                self.evidence.log_event("replay_end", result.model_dump(), actor="automation")
                self.evidence.write_result(result.model_dump())
            return result
        except Exception as e:
            result = ReplayResult(
                status=ReplayStatus.FAILED,
                capability_id=cap.id,
                capability_version=cap.version,
                run_id=run_id,
                failure=Failure(code=FailureCode.UNEXPECTED_STATE, observed=str(e)),
                recoveries=recoveries,
                drift_signals=drift,
                intervention_id=self._intervention_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            if self.evidence:
                self.evidence.write_result(result.model_dump())
            return result

    async def _check_global(
        self,
        cap: Any,
        step: CapabilityStep,
        recoveries: list[RecoveryEvent],
    ) -> ReplayResult | None:
        obs = await self.surface.observe(screenshot=False)
        det = detect_observation(obs)
        if det.kind == DetectKind.APP_ERROR:
            path = None
            if self.evidence:
                shot = await self.surface.observe(screenshot=True)
                path = str(self.evidence.save_screenshot("app_error", shot.screenshot_png))
            return ReplayResult(
                status=ReplayStatus.FAILED,
                capability_id=cap.id,
                capability_version=cap.version,
                run_id=self.evidence.run_id if self.evidence else "n/a",
                failure=Failure(
                    step_id=step.id,
                    code=FailureCode.APP_ERROR,
                    observed=det.matched,
                    evidence_paths=[path] if path else [],
                ),
            )
        if det.kind == DetectKind.SESSION_EXPIRED:
            return await self._needs_human(
                cap,
                step,
                FailureCode.SESSION_EXPIRED,
                det.matched or "",
                recoveries,
            )
        if det.kind == DetectKind.INTERSTITIAL:
            # Try recoverables
            for rec in cap.recoverables:
                count = self._recovery_counts.get(rec.id, 0)
                if count >= rec.max_times:
                    continue
                if _expect_matches(rec.detect, obs):
                    if rec.handle.escalate:
                        return await self._needs_human(
                            cap,
                            step,
                            FailureCode.UNEXPECTED_STATE,
                            rec.id,
                            recoveries,
                        )
                    if rec.handle.action == "click" and rec.handle.target:
                        try:
                            await self._click_target(rec.handle.target)
                        except Exception:
                            # try main frame fallback
                            from cua.artifact.schema import FrameHint, StepTarget

                            fb = StepTarget(
                                frame=FrameHint(name_hint="main"),
                                strategies=list(rec.handle.target.strategies),
                            )
                            try:
                                await self._click_target(fb)
                            except Exception:
                                continue
                        self._recovery_counts[rec.id] = count + 1
                        recoveries.append(RecoveryEvent(id=rec.id, step_id=step.id, detail="dismissed"))
                        if self.evidence:
                            self.evidence.log_event("recovery", {"id": rec.id}, actor="automation")
                        return None
            # Fallback: click Acknowledge (often inside content frame)
            try:
                from cua.artifact.schema import FrameHint, StepTarget, StrategyByHeuristic, StrategyByRole

                for frame_hint in (None, "main", "nav"):
                    ack_target = StepTarget(
                        frame=FrameHint(name_hint=frame_hint) if frame_hint else None,
                        strategies=[
                            StrategyByRole(role="button", name="Acknowledge"),
                            StrategyByHeuristic(text="Acknowledge"),
                        ],
                    )
                    try:
                        await self._click_target(ack_target)
                        recoveries.append(
                            RecoveryEvent(id="system_notice", step_id=step.id, detail="auto-ack")
                        )
                        break
                    except Exception:
                        continue
            except Exception:
                pass
        return None

    async def _run_step(
        self,
        artifact: CapabilityArtifact,
        step: CapabilityStep,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        recoveries: list[RecoveryEvent],
        drift: list[DriftSignal],
    ) -> ReplayResult | None:
        cap = artifact.capability
        value = render_template(step.value, inputs) if step.value else None

        # Build action for policy
        action = Action(
            kind=ActionKind(step.action if step.action != "extract" else "extract"),
            value=value,
            key=step.key,
            url=step.url,
            target=Target(
                frame=step.target.frame_name() if step.target else None,
                text=_strategy_text(step),
                name=_strategy_text(step),
            )
            if step.target
            else None,
        )
        decision = self.policy.check(
            action,
            capability_status=cap.status.value,
            confirm_irreversible=self.confirm_irreversible or step.requires_confirmation,
            mode="replay",
        )
        if decision.require == PolicyRequire.BLOCK or not decision.allowed:
            if decision.require == PolicyRequire.CONFIRM:
                blocked = await self._needs_human(
                    cap,
                    step,
                    FailureCode.POLICY_BLOCKED,
                    decision.reason,
                    recoveries,
                )
                if blocked is not None:
                    return blocked
                # Human confirmed — fall through and perform the step.
            else:
                return ReplayResult(
                    status=ReplayStatus.FAILED,
                    capability_id=cap.id,
                    capability_version=cap.version,
                    run_id=self.evidence.run_id if self.evidence else "n/a",
                    failure=Failure(
                        step_id=step.id,
                        code=FailureCode.POLICY_BLOCKED,
                        observed=decision.reason,
                    ),
                )

        if step.action == "navigate":
            await self.surface.act(Action(kind=ActionKind.NAVIGATE, url=step.url or "/"))
            return None

        if step.action == "press":
            await self.surface.act(Action(kind=ActionKind.PRESS, key=step.key or "Enter"))
        elif step.action == "extract":
            for out_name, field in (step.extract or {}).items():
                handle, strat, degraded, preferred = await locate(
                    self.surface, field.target, timeout_ms=self.step_timeout_ms
                )
                if degraded:
                    drift.append(
                        DriftSignal(
                            step_id=step.id,
                            strategy_used=strat,
                            preferred_strategy=preferred,
                            detail="degraded_locator",
                            failed_preferred=list(handle.meta.get("failed_preferred") or []),
                        )
                    )
                text = ""
                if handle.meta.get("locator"):
                    try:
                        text = (await handle.meta["locator"].inner_text()).strip()
                    except Exception:
                        text = ""
                if not text:
                    text = await self.surface.read(
                        Target(
                            strategies=field.target.strategies_as_dicts(),
                            frame=field.target.frame_name(),
                        )
                    )
                outputs[out_name] = _parse_value(text, field.parse)
                if self.evidence:
                    self.evidence.log_event(
                        "step_act",
                        {
                            "action": "extract",
                            "intent": step.intent,
                            "output": out_name,
                            "locator_used": strat,
                            "preferred_strategy": preferred,
                            "degraded": degraded,
                        },
                        actor="automation",
                        step_id=step.id,
                    )
            return None
        else:
            try:
                handle, strat, degraded, preferred = await locate(
                    self.surface, step.target, timeout_ms=self.step_timeout_ms
                )
            except LocateError as e:
                # Re-check detectors
                obs = await self.surface.observe(screenshot=False)
                det = detect_observation(obs)
                if det.kind == DetectKind.APP_ERROR:
                    path = None
                    if self.evidence:
                        path = str(self.evidence.save_screenshot("app_error", obs.screenshot_png))
                    return ReplayResult(
                        status=ReplayStatus.FAILED,
                        capability_id=cap.id,
                        capability_version=cap.version,
                        run_id=self.evidence.run_id if self.evidence else "n/a",
                        failure=Failure(
                            step_id=step.id,
                            code=FailureCode.APP_ERROR,
                            evidence_paths=[path] if path else [],
                        ),
                    )
                code = FailureCode.TARGET_AMBIGUOUS if e.code == "TARGET_AMBIGUOUS" else FailureCode.TARGET_NOT_FOUND
                return ReplayResult(
                    status=ReplayStatus.FAILED,
                    capability_id=cap.id,
                    capability_version=cap.version,
                    run_id=self.evidence.run_id if self.evidence else "n/a",
                    failure=Failure(step_id=step.id, code=code, observed=str(e)),
                )

            if degraded:
                drift.append(
                    DriftSignal(
                        step_id=step.id,
                        strategy_used=strat,
                        preferred_strategy=preferred,
                        detail="degraded_locator",
                        failed_preferred=list(handle.meta.get("failed_preferred") or []),
                    )
                )
                if self.evidence:
                    self.evidence.log_event(
                        "degraded_locator",
                        {
                            "step_id": step.id,
                            "strategy_used": strat,
                            "preferred_strategy": preferred,
                            "failed_preferred": handle.meta.get("failed_preferred"),
                        },
                        actor="automation",
                    )

            if self.evidence:
                self.evidence.log_event(
                    "step_act",
                    {
                        "action": step.action,
                        "intent": step.intent,
                        "locator_used": strat,
                        "preferred_strategy": preferred,
                        "degraded": degraded,
                        "value_is_param": bool(step.value and "{{inputs." in (step.value or "")),
                    },
                    actor="automation",
                    step_id=step.id,
                )

            loc = handle.meta.get("locator")
            if step.action == "click" and loc:
                await loc.click(timeout=8000)
            elif step.action == "type" and loc:
                await loc.fill(value or "", timeout=8000)
            elif step.action == "select" and loc:
                await loc.select_option(value or "", timeout=8000)
            await self.surface.act(Action(kind=ActionKind.WAIT, meta={"ms": 250}))

            if self.evidence:
                self.evidence.log_event(
                    "step_ok",
                    {"action": step.action, "locator_used": strat},
                    actor="automation",
                    step_id=step.id,
                )

        # Evaluate expect
        if step.expect:
            outcome = await self._eval_expect(step.expect)
            if outcome:
                return ReplayResult(
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    capability_id=cap.id,
                    capability_version=cap.version,
                    run_id=self.evidence.run_id if self.evidence else "n/a",
                    outcome=outcome,
                    outputs=outputs or None,
                    recoveries=recoveries,
                    drift_signals=drift,
                )
        return None

    async def _click_target(self, step_target: Any) -> None:
        handle, _, _, _ = await locate(self.surface, step_target, timeout_ms=5000)
        loc = handle.meta.get("locator")
        if loc is None:
            raise RuntimeError("ack target missing locator")
        await loc.click(timeout=5000)
        await self.surface.act(Action(kind=ActionKind.WAIT, meta={"ms": 300}))

    async def _eval_expect(self, expect: Expect) -> str | None:
        obs = await self.surface.observe(screenshot=False)
        text = obs.visible_text_excerpt or ""
        if isinstance(expect, ExpectAnyOf):
            for e in expect.any_of:
                hit = await self._eval_expect(e)
                if hit:
                    return hit
                # continue branch
                if getattr(e, "then", None) == "continue" and getattr(e, "text", None):
                    if e.text.lower() in text.lower():
                        return None
            return None
        if getattr(expect, "outcome", None) and getattr(expect, "text", None):
            if expect.text.lower() in text.lower():
                return expect.outcome
        if getattr(expect, "kind", None) == "text_visible" and getattr(expect, "text", None):
            if expect.text.lower() in text.lower() and getattr(expect, "then", None) == "continue":
                return None
        return None

    async def _needs_human(
        self,
        cap: Any,
        step: CapabilityStep,
        code: FailureCode,
        reason: str,
        recoveries: list[RecoveryEvent] | None = None,
    ) -> ReplayResult | None:
        intervention_id = None
        if self.evidence:
            shot = await self.surface.observe(screenshot=True)
            path = str(
                self.evidence.save_screenshot(f"handoff_{step.id}", shot.screenshot_png)
            )
            self.evidence.log_event(
                "needs_human",
                {"reason": reason, "code": code.value, "screenshot": path},
                actor="automation",
                step_id=step.id,
            )
        else:
            path = None

        if self.allow_handoff and self.handoff is not None:
            lease = await self.handoff.request(
                reason=reason,
                step_id=step.id,
                capability_id=cap.id,
                screenshot_path=path,
            )
            intervention_id = lease.id
            self._intervention_id = lease.id
            if self.evidence:
                self.evidence.log_event(
                    "handoff_requested",
                    {"lease_id": lease.id, "token_suffix": lease.token[-6:]},
                    actor="automation",
                    step_id=step.id,
                )
            await self.handoff.wait_until_released()
            # verify-on-resume: session should not still look expired
            obs = await self.surface.observe(screenshot=False)
            from cua.replay.detectors import DetectKind, detect_observation

            det = detect_observation(obs)
            if det.kind == DetectKind.SESSION_EXPIRED:
                if self.evidence:
                    self.evidence.log_event(
                        "verify_failed",
                        {"reason": "still on login / session expired"},
                        actor="automation",
                        step_id=step.id,
                    )
                return ReplayResult(
                    status=ReplayStatus.NEEDS_HUMAN,
                    capability_id=cap.id,
                    capability_version=cap.version,
                    run_id=self.evidence.run_id if self.evidence else "n/a",
                    failure=Failure(step_id=step.id, code=code, observed=reason),
                    intervention_id=intervention_id,
                )
            if recoveries is not None:
                recoveries.append(
                    RecoveryEvent(
                        id="human_handoff",
                        step_id=step.id,
                        detail=f"{code.value}: operator resumed lease {lease.id}",
                    )
                )
            if self.evidence:
                self.evidence.log_event(
                    "handoff_resumed",
                    {"lease_id": intervention_id, "verified": True},
                    actor="automation",
                    step_id=step.id,
                )
            # Same step retries after the human restored the session
            return None

        return ReplayResult(
            status=ReplayStatus.NEEDS_HUMAN,
            capability_id=cap.id,
            capability_version=cap.version,
            run_id=self.evidence.run_id if self.evidence else "n/a",
            failure=Failure(step_id=step.id, code=code, observed=reason),
            intervention_id=intervention_id,
        )


def _validate_inputs(specs: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    for name, spec in specs.items():
        if name not in inputs:
            return f"missing input: {name}"
        val = str(inputs[name])
        pattern = getattr(spec, "pattern", None)
        if pattern and not re.match(pattern, val):
            return f"input {name} failed pattern {pattern}"
    return None


def _strategy_text(step: CapabilityStep) -> str | None:
    if not step.target:
        return None
    for s in step.target.strategies:
        for attr in ("name", "text", "label", "value"):
            v = getattr(s, attr, None)
            if v:
                return str(v)
    return None


def _parse_value(text: str, parse: str) -> Any:
    raw = text.strip().replace(",", "").replace("$", "")
    if parse in {"currency", "number"}:
        try:
            return float(raw)
        except ValueError:
            return text.strip()
    return text.strip()


def _expect_matches(expect: Expect, obs: Any) -> bool:
    text = (obs.visible_text_excerpt or "").lower()
    if getattr(expect, "kind", None) == "text_visible" and getattr(expect, "text", None):
        return expect.text.lower() in text
    if getattr(expect, "kind", None) == "any_of":
        return any(_expect_matches(e, obs) for e in expect.any_of)
    return False
