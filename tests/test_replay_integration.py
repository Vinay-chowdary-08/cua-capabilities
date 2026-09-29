"""Integration tests against local target app — fail hard if target is down."""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.asyncio

BASE = os.environ.get("CUA_TARGET_URL", "http://127.0.0.1:8800")


async def _require_target() -> None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE}/health", timeout=2.0)
            if r.status_code == 200:
                return
    except Exception as exc:
        pytest.fail(f"target must be up at {BASE}/health for integration tests: {exc}")
    pytest.fail(f"target must be up at {BASE}/health for integration tests")


async def _faults(**params: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.get(f"{BASE}/__faults", params={"clear": "true"})
        if params:
            await client.get(f"{BASE}/__faults", params=params)


async def _login_and_engine(*, allow_handoff: bool = False, tenant: str | None = None):
    from cua.artifact.store import ArtifactStore
    from cua.handoff.capture import HumanCapture
    from cua.handoff.control import HandoffController
    from cua.replay.engine import ReplayEngine
    from cua.safety.policy import Policy
    from cua.surface.base import Action, ActionKind, Target
    from cua.surface.web import WebSurface

    store = ArtifactStore("capabilities")
    art = store.load("cucore.member.read_savings_balance", tenant=tenant)
    surface = WebSurface(headless=True, base_url=BASE)
    await surface.start()
    await surface.goto(BASE + ("/?tenant=" + tenant if tenant else "/"))
    obs = await surface.observe()
    for e in obs.elements:
        if e.name_attr == "username":
            await surface.act(Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value="teller"))
        if e.name_attr == "password":
            await surface.act(Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value="teller"))
    obs = await surface.observe()
    for e in obs.elements:
        if (e.value or e.text) == "Log On":
            await surface.act(Action(kind=ActionKind.CLICK, target=Target(mark=e.mark)))
            break
    handoff = HandoffController(lease_seconds=60) if allow_handoff else None
    if handoff is not None:
        await HumanCapture(handoff).install(surface.page)
    engine = ReplayEngine(
        surface,
        Policy.load(),
        allow_handoff=allow_handoff,
        handoff=handoff,
        step_timeout_ms=5000,
    )
    return art, surface, engine, handoff


async def test_happy_path_success():
    await _require_target()
    await _faults()
    art, surface, engine, _ = await _login_and_engine()
    try:
        result = await engine.run(art, {"member_number": "12345"})
        from cua.replay.result import ReplayStatus

        assert result.status == ReplayStatus.SUCCESS
        assert result.outputs and result.outputs.get("savings_balance") == 2418.55
    finally:
        await surface.stop()


async def test_member_not_found_business_outcome():
    await _require_target()
    await _faults()
    art, surface, engine, _ = await _login_and_engine()
    try:
        result = await engine.run(art, {"member_number": "99999"})
        from cua.replay.result import ReplayStatus

        assert result.status == ReplayStatus.BUSINESS_OUTCOME
        assert result.outcome == "member_not_found"
    finally:
        await surface.stop()


async def test_invalid_input_no_browser_needed():
    from cua.artifact.store import ArtifactStore
    from cua.replay.engine import ReplayEngine
    from cua.replay.result import FailureCode, ReplayStatus
    from cua.safety.policy import Policy
    from cua.surface.web import WebSurface

    class BoomSurface(WebSurface):
        async def observe(self):  # type: ignore[override]
            raise AssertionError("browser should not be touched")

        async def goto(self, url: str) -> None:
            raise AssertionError("browser should not be touched")

    art = ArtifactStore("capabilities").load("cucore.member.read_savings_balance")
    surface = BoomSurface(headless=True, base_url=BASE)
    engine = ReplayEngine(surface, Policy.load())
    result = await engine.run(art, {"member_number": "abc"})
    assert result.status == ReplayStatus.FAILED
    assert result.failure and result.failure.code == FailureCode.INPUT_INVALID


async def test_interstitial_recovery():
    await _require_target()
    await _faults(interstitial="true")
    art, surface, engine, _ = await _login_and_engine()
    try:
        result = await engine.run(art, {"member_number": "12345"})
        from cua.replay.result import ReplayStatus

        assert result.status == ReplayStatus.SUCCESS
        assert any(r.id == "system_notice" for r in result.recoveries)
    finally:
        await surface.stop()
        await _faults()


async def test_error500_failed():
    await _require_target()
    await _faults(error500="true")
    art, surface, engine, _ = await _login_and_engine()
    try:
        result = await engine.run(art, {"member_number": "12345"})
        from cua.replay.result import FailureCode, ReplayStatus

        assert result.status == ReplayStatus.FAILED
        assert result.failure and result.failure.code == FailureCode.APP_ERROR
    finally:
        await surface.stop()
        await _faults()


async def test_cross_tenant_drift_labels_differ():
    await _require_target()
    await _faults()
    art, surface, engine, _ = await _login_and_engine(tenant="tenant_b")
    try:
        result = await engine.run(art, {"member_number": "12345"})
        from cua.replay.result import ReplayStatus

        assert result.status == ReplayStatus.SUCCESS
        assert result.outputs and result.outputs.get("savings_balance") == 2418.55
        assert result.drift_signals, "tenant_b should produce degraded-locator drift"
        # preferred (tenant_a label) must not equal used (tenant_b label)
        assert any(
            d.preferred_strategy and d.strategy_used and d.preferred_strategy != d.strategy_used
            for d in result.drift_signals
        ), result.drift_signals
    finally:
        await surface.stop()


async def test_handoff_session_expire_resume():
    await _require_target()
    await _faults()
    import asyncio

    from cua.handoff.control import ControlState
    from cua.replay.result import ReplayStatus
    from cua.surface.base import Action, ActionKind, Target

    art, surface, engine, handoff = await _login_and_engine(allow_handoff=True)
    assert handoff is not None

    async def operator() -> None:
        for _ in range(200):
            if handoff.state == ControlState.AWAITING_HUMAN and handoff.lease:
                break
            await asyncio.sleep(0.05)
        else:
            return
        lease = handoff.lease
        assert lease is not None
        handoff.claim("test.op", token=lease.token)
        await _faults()
        # re-login + restore search
        await surface.goto(BASE + "/")
        obs = await surface.observe(screenshot=False)
        for e in obs.elements:
            if e.name_attr == "username":
                await surface.act(
                    Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value="teller")
                )
            if e.name_attr == "password":
                await surface.act(
                    Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value="teller")
                )
        obs = await surface.observe(screenshot=False)
        for e in obs.elements:
            if (e.value or e.text) == "Log On":
                await surface.act(Action(kind=ActionKind.CLICK, target=Target(mark=e.mark)))
                break
        await surface.goto(BASE + "/home")
        from cua.artifact.schema import FrameHint, StepTarget, StrategyByRole
        from cua.replay.locate import locate

        search = StepTarget(
            frame=FrameHint(name_hint="nav"),
            strategies=[StrategyByRole(role="link", name="Member Search")],
        )
        handle, _, _, _ = await locate(surface, search, timeout_ms=5000)
        await handle.meta["locator"].click()
        handoff.resume("restored")
        handoff.verify_ok()

    await _faults(session_expire="true")
    try:
        op = asyncio.create_task(operator())
        result = await engine.run(art, {"member_number": "12345"})
        await op
        assert result.status == ReplayStatus.SUCCESS
        assert result.intervention_id
        assert any(r.id == "human_handoff" for r in result.recoveries)
    finally:
        await surface.stop()
        await _faults()
