"""Unit tests: schema validators, policy, redact, handoff, replay isolation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cua.artifact.schema import (
    AppInfo,
    CapabilityArtifact,
    CapabilityBody,
    CapabilityStep,
    ExpectTextVisible,
    FieldSpec,
    OutcomeSpec,
    RiskClass,
    Sensitivity,
    StepTarget,
    StrategyByRole,
)
from cua.artifact.store import ArtifactStore
from cua.handoff.control import ControlState, HandoffController
from cua.safety.policy import Policy, PolicyRequire
from cua.safety.redact import Redactor
from cua.surface.base import Action, ActionKind, Target


def test_replay_package_does_not_import_agent_or_llm() -> None:
    root = Path("cua/replay")
    forbidden = ("cua.agent", "anthropic", "openai")
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in forbidden:
                        assert not alias.name.startswith(bad), f"{path} imports {alias.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                for bad in forbidden:
                    assert not node.module.startswith(bad), f"{path} imports {node.module}"


def test_step_ids_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        CapabilityArtifact(
            capability=CapabilityBody(
                id="x",
                title="t",
                app=AppInfo(vendor="a", product="b"),
                steps=[
                    CapabilityStep(
                        id="s1",
                        intent="a",
                        action="click",
                        target=StepTarget(strategies=[StrategyByRole(role="link", name="A")]),
                    ),
                    CapabilityStep(
                        id="s1",
                        intent="b",
                        action="click",
                        target=StepTarget(strategies=[StrategyByRole(role="link", name="B")]),
                    ),
                ],
            )
        )


def test_input_ref_must_exist() -> None:
    with pytest.raises(ValueError, match="unknown input"):
        CapabilityArtifact(
            capability=CapabilityBody(
                id="x",
                title="t",
                app=AppInfo(vendor="a", product="b"),
                inputs={},
                steps=[
                    CapabilityStep(
                        id="s1",
                        intent="type",
                        action="type",
                        value="{{inputs.member_number}}",
                        target=StepTarget(strategies=[StrategyByRole(role="textbox", name="q")]),
                    )
                ],
            )
        )


def test_outcome_must_be_declared() -> None:
    with pytest.raises(ValueError, match="not declared"):
        CapabilityArtifact(
            capability=CapabilityBody(
                id="x",
                title="t",
                app=AppInfo(vendor="a", product="b"),
                inputs={"member_number": FieldSpec()},
                outcomes={},
                steps=[
                    CapabilityStep(
                        id="s1",
                        intent="go",
                        action="click",
                        target=StepTarget(strategies=[StrategyByRole(role="link", name="X")]),
                        expect=ExpectTextVisible(text="Nope", outcome="member_not_found"),
                    )
                ],
            )
        )


def test_irreversible_requires_confirmation() -> None:
    with pytest.raises(ValueError, match="requires_confirmation"):
        CapabilityArtifact(
            capability=CapabilityBody(
                id="x",
                title="t",
                app=AppInfo(vendor="a", product="b"),
                risk=RiskClass.IRREVERSIBLE,
                steps=[
                    CapabilityStep(
                        id="s1",
                        intent="Confirm open",
                        action="click",
                        target=StepTarget(
                            strategies=[StrategyByRole(role="button", name="Confirm Open")]
                        ),
                        requires_confirmation=False,
                    )
                ],
            )
        )


def test_store_roundtrip_and_schema_export(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    art = CapabilityArtifact(
        capability=CapabilityBody(
            id="demo.cap",
            title="Demo",
            app=AppInfo(vendor="a", product="b"),
            inputs={"member_number": FieldSpec(pattern="^[0-9]+$")},
            outcomes={"member_not_found": OutcomeSpec()},
            steps=[
                CapabilityStep(
                    id="s1",
                    intent="open",
                    action="click",
                    target=StepTarget(strategies=[StrategyByRole(role="link", name="Search")]),
                    expect=ExpectTextVisible(text="No member found", outcome="member_not_found"),
                )
            ],
        )
    )
    store.save(art)
    loaded = store.load("demo.cap")
    assert loaded.capability.id == "demo.cap"
    schema_path = store.export_json_schema(tmp_path / "capability.schema.json")
    assert schema_path.exists()


def test_policy_blocks_off_allowlist_origin() -> None:
    policy = Policy.load("policy/default.yaml")
    dec = policy.check(Action(kind=ActionKind.NAVIGATE, url="https://evil.example/"))
    assert not dec.allowed
    assert dec.require == PolicyRequire.BLOCK


def test_policy_confirm_on_irreversible_button() -> None:
    policy = Policy.load("policy/default.yaml")
    dec = policy.check(
        Action(kind=ActionKind.CLICK, target=Target(text="Confirm Open")),
        mode="discovery",
    )
    assert dec.require == PolicyRequire.CONFIRM


def test_redactor_masks_ssn_and_params() -> None:
    r = Redactor.load(params={"member_number": "12345"})
    text = r.redact_text("member 12345 ssn 111-22-1001 mail a@b.co")
    assert "{{inputs.member_number}}" in text
    assert "[SSN]" in text
    assert "[EMAIL]" in text
    assert r.mask_output(2418.55, sensitivity="financial") == "$***.**"


@pytest.mark.asyncio
async def test_handoff_lease_and_transitions() -> None:
    c = HandoffController(lease_seconds=60)
    assert c.owns_control()
    lease = await c.request("stuck", step_id="s1")
    assert c.state == ControlState.AWAITING_HUMAN
    c.claim("op", token=lease.token)
    assert c.state == ControlState.HUMAN
    c.resume("done")
    assert c.state == ControlState.VERIFYING
    c.verify_ok()
    assert c.state == ControlState.AUTOMATION
    assert c.owns_control()


def test_shared_controller_is_singleton() -> None:
    from cua.handoff.control import SHARED_CONTROLLER, get_shared_controller
    from cua.handoff.console import create_console_app

    assert get_shared_controller() is SHARED_CONTROLLER
    app = create_console_app()
    # Resume must NOT auto-verify — routes include explicit verify-ok
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/operator/verify-ok" in paths
    assert "/operator/resume" in paths


def test_strategy_label_distinguishes_preferred() -> None:
    from cua.replay.locate import strategy_label

    a = strategy_label({"by": "role", "role": "link", "name": "Member Search"})
    b = strategy_label({"by": "role", "role": "link", "name": "Holder Lookup"})
    assert a != b
    assert a.startswith("role:")


def test_human_intent_not_raw_json() -> None:
    from cua.agent.loop import _human_intent

    intent = _human_intent("click", {"mark": 3}, None)
    assert "mark 3" in intent
    assert "{" not in intent


def test_llm_decide_signature_requires_safe_png() -> None:
    """Guardrail: live path must accept redacted PNG (regression for raw egress)."""
    import inspect

    from cua.agent.loop import AgentLoop

    sig = inspect.signature(AgentLoop._llm_decide)
    assert "safe_png" in sig.parameters


@pytest.mark.asyncio
async def test_human_capture_ignores_automation_clicks() -> None:
    from cua.handoff.capture import HumanCapture
    from cua.handoff.control import ControlState, HandoffController

    c = HandoffController()
    cap = HumanCapture(c)

    class FakePage:
        frames: list[Any] = []

        async def expose_binding(self, name: str, fn: Any) -> None:
            self.fn = fn

        async def add_init_script(self, script: str) -> None:
            return None

    page = FakePage()
    await cap.install(page)  # type: ignore[arg-type]
    # Automation owns control — event discarded
    await page.fn(None, {"kind": "click", "text": "Find Member"})
    assert c.captured_actions == []
    # Human owns control — event recorded
    c.state = ControlState.HUMAN
    await page.fn(None, {"kind": "click", "text": "Member Search", "value_redacted": ""})
    assert len(c.captured_actions) == 1
    assert c.captured_actions[0]["text"] == "Member Search"
