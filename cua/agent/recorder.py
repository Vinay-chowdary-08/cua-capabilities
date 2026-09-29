"""Recorder: derive unique multi-strategy locators from live resolved elements."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cua.artifact.schema import (
    AppInfo,
    CapabilityArtifact,
    CapabilityBody,
    CapabilityStatus,
    CapabilityStep,
    ExpectAnyOf,
    ExpectTextVisible,
    ExtractField,
    FieldSpec,
    OutcomeSpec,
    Provenance,
    Recoverable,
    RecoverableHandle,
    RiskClass,
    Sensitivity,
    StepTarget,
    StrategyByCss,
    StrategyByHeuristic,
    StrategyByLabel,
    StrategyByNameAttr,
    StrategyByRole,
    StrategyByTableCell,
    StrategyByText,
    SuccessCondition,
)
from cua.surface.base import Action, ActionKind, ElementRef, Observation, Surface, Target


class ArtifactRecorder:
    def __init__(
        self,
        *,
        capability_id: str,
        title: str,
        goal: str,
        params: dict[str, str],
        app: AppInfo | None = None,
    ) -> None:
        self.capability_id = capability_id
        self.title = title
        self.goal = goal
        self.params = params
        self.app = app or AppInfo(
            vendor="acme-cucore", product="CU Core Teller", compatible_versions=">=7.2,<8"
        )
        self.steps: list[CapabilityStep] = []
        self.outputs: dict[str, FieldSpec] = {}
        self.outcomes: dict[str, OutcomeSpec] = {
            "member_not_found": OutcomeSpec(kind="business"),
            "permission_denied": OutcomeSpec(kind="business"),
        }
        self.run_id = uuid4().hex[:12]
        self.risk = RiskClass.READ_ONLY

    async def record(
        self,
        surface: Surface,
        action: Action,
        *,
        element: ElementRef | None,
        intent: str,
        observation: Observation,
    ) -> None:
        step_id = f"s{len(self.steps)+1}_{action.kind.value}"
        target = None
        value = action.value
        is_param = action.is_param
        if value and not is_param:
            for k, v in self.params.items():
                if v and value == v:
                    is_param = k
                    break
        if is_param and value:
            value = "{{inputs." + is_param + "}}"

        if action.kind in {ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT, ActionKind.EXTRACT}:
            strategies = await self._candidate_strategies(surface, element, action, observation)
            target = StepTarget(
                frame=element.frame_path[0] if element and element.frame_path else None,
                strategies=strategies,  # type: ignore[arg-type]
            )

        requires_confirmation = False
        blob = " ".join(
            filter(None, [intent, action.value, element.text if element else None])
        ).lower()
        if any(k in blob for k in ("confirm", "submit open", "delete", "transfer")):
            # "Submit" alone is too noisy on search; irreversible words only.
            if any(k in blob for k in ("confirm", "delete", "transfer", "open sub")):
                self.risk = RiskClass.IRREVERSIBLE
                requires_confirmation = True

        step = CapabilityStep(
            id=step_id,
            intent=intent,
            action=action.kind.value,  # type: ignore[arg-type]
            target=target,
            value=value,
            key=action.key,
            url=action.url,
            requires_confirmation=requires_confirmation,
            provenance="agent",
        )
        if action.kind == ActionKind.EXTRACT and action.output_name and target:
            step.extract = {
                action.output_name: ExtractField(
                    target=target,
                    parse="currency" if "balance" in action.output_name else "text",
                )
            }
            self.outputs[action.output_name] = FieldSpec(
                type="number" if "balance" in action.output_name else "string",
                format="currency" if "balance" in action.output_name else None,
                sensitivity=Sensitivity.FINANCIAL
                if "balance" in action.output_name
                else Sensitivity.PII,
                redact_in_logs=True,
            )
        self.steps.append(step)

    async def _candidate_strategies(
        self,
        surface: Surface,
        element: ElementRef | None,
        action: Action,
        observation: Observation,
    ) -> list[Any]:
        if element is None:
            return []
        candidates: list[Any] = []
        if element.role and (element.name or element.text):
            candidates.append(
                StrategyByRole(
                    role=element.role, name=element.name or element.text or "", exact=False
                )
            )
        if element.near_label:
            candidates.append(StrategyByLabel(label=element.near_label))
        if element.name_attr:
            candidates.append(StrategyByNameAttr(name=element.name_attr))
        if element.text:
            candidates.append(StrategyByText(text=element.text, exact=True))
            candidates.append(StrategyByHeuristic(text=element.text))

        # Balance cells: prefer table_cell over CSS noise
        if action.kind == ActionKind.EXTRACT or (
            element.text and any(ch.isdigit() for ch in element.text) and "," in element.text
        ):
            row = _guess_row_header(element, observation)
            if row:
                candidates.insert(
                    0,
                    StrategyByTableCell(row_header=row, column_header="Balance"),
                )
                candidates.insert(
                    1,
                    StrategyByTableCell(row_header=row, column_header="Avail Bal"),
                )

        # CSS last — brittle, keep only as final fallback if unique
        css_candidates: list[Any] = []
        meta_sel = (element.meta or {}).get("selector_hint")
        if meta_sel:
            css_candidates.append(StrategyByCss(value=str(meta_sel)))

        verified: list[Any] = []
        frame = element.frame_path[0] if element.frame_path else None
        for strat in candidates + css_candidates:
            try:
                handle = await surface.resolve(
                    Target(frame=frame, strategies=[strat.model_dump()]),
                    timeout_ms=2000,
                )
                if handle:
                    verified.append(strat)
            except Exception:
                continue
        # Prefer non-CSS; only keep CSS if nothing else verified
        non_css = [s for s in verified if not isinstance(s, StrategyByCss)]
        return non_css or verified[:2] or candidates[:2]

    def build(self, *, summary: str | None = None) -> CapabilityArtifact:
        inputs = {
            k: FieldSpec(
                type="string",
                pattern="^[0-9]{4,10}$" if k == "member_number" else None,
                sensitivity=Sensitivity.INTERNAL_ID
                if k == "member_number"
                else Sensitivity.SECRET,
            )
            for k in self.params
            if k not in {"username", "password"}
        }
        if "member_number" not in inputs and "member_number" in self.params:
            inputs["member_number"] = FieldSpec(
                type="string", pattern="^[0-9]{4,10}$", sensitivity=Sensitivity.INTERNAL_ID
            )

        for step in self.steps:
            intent_l = (step.intent or "").lower()
            if step.action == "click" and any(
                k in intent_l for k in ("find member", "search", "submit")
            ):
                step.expect = ExpectAnyOf(
                    any_of=[
                        ExpectTextVisible(text="Open", then="continue"),
                        ExpectTextVisible(text="No member found", outcome="member_not_found"),
                        ExpectTextVisible(text="not authorized", outcome="permission_denied"),
                    ]
                )

        recoverables = [
            Recoverable(
                id="system_notice",
                detect=ExpectTextVisible(text="System notice"),
                handle=RecoverableHandle(
                    action="click",
                    target=StepTarget(
                        strategies=[StrategyByRole(role="button", name="Acknowledge")]
                    ),
                ),
                max_times=3,
            ),
            Recoverable(
                id="session_expired",
                detect=ExpectTextVisible(text="Session expired"),
                handle=RecoverableHandle(escalate=True),
                max_times=1,
            ),
        ]

        goal_text = self.goal
        for k, v in self.params.items():
            if v and v in goal_text:
                goal_text = goal_text.replace(v, "{{" + k + "}}")

        desc = summary or self.goal
        if desc and desc.startswith("ok:"):
            desc = self.goal

        body = CapabilityBody(
            id=self.capability_id,
            version="1.0.0",
            title=self.title,
            description=desc,
            status=CapabilityStatus.DRAFT,
            app=self.app,
            inputs=inputs
            or {
                "member_number": FieldSpec(
                    type="string",
                    pattern="^[0-9]{4,10}$",
                    sensitivity=Sensitivity.INTERNAL_ID,
                )
            },
            outputs=self.outputs
            or {
                "savings_balance": FieldSpec(
                    type="number",
                    format="currency",
                    sensitivity=Sensitivity.FINANCIAL,
                    redact_in_logs=True,
                )
            },
            risk=self.risk,
            preconditions=[{"kind": "authenticated_session"}],
            steps=self.steps,
            outcomes=self.outcomes,
            recoverables=recoverables,
            success=[SuccessCondition(kind="output_present", output="savings_balance")]
            if "savings_balance" in (self.outputs or {"savings_balance": 1})
            else [],
            provenance=Provenance(
                discovered_by={"run_id": self.run_id},
                goal_text=goal_text,
                reviewed_by=None,
            ),
        )
        return CapabilityArtifact(schema_version="1.0", capability=body)


def _guess_row_header(element: ElementRef, observation: Observation) -> str | None:
    if element.near_label and "share" in element.near_label.lower():
        return element.near_label
    text = observation.visible_text_excerpt or ""
    for candidate in ("Share Savings", "Checking", "Money Market"):
        if candidate in text:
            return candidate
    return "Share Savings"
