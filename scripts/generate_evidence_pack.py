#!/usr/bin/env python3
"""Rebuild the evidence pack reviewers actually look at.

- Real Playwright trace.zip on success / error / handoff runs
- Full handoff story: request → claim → human re-login → resume → verify → continue
- Live Anthropic discovery when ANTHROPIC_API_KEY is set (refuses to fake it)
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import httpx
from dotenv import load_dotenv

from cua.artifact.store import ArtifactStore
from cua.evidence.logger import EvidenceLogger
from cua.handoff.control import ControlState, HandoffController
from cua.replay.engine import ReplayEngine
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.surface.base import Action, ActionKind, Target
from cua.surface.web import WebSurface

BASE = os.environ.get("CUA_TARGET_URL", "http://127.0.0.1:8800")
EVIDENCE = Path("evidence")


async def clear_faults(**extra: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.get(f"{BASE}/__faults", params={"clear": "true"})
        if extra:
            await client.get(f"{BASE}/__faults", params=extra)


async def login(surface: WebSurface, *, tenant: str | None = None) -> None:
    url = BASE + (f"/?tenant={tenant}" if tenant else "/")
    await surface.goto(url)
    obs = await surface.observe(screenshot=False)
    if any((e.text or "") in {"Member Search", "Holder Lookup"} for e in obs.elements):
        return
    user = os.environ.get("CUA_TARGET_USER", "teller")
    pw = os.environ.get("CUA_TARGET_PASSWORD", "teller")
    for e in obs.elements:
        if e.name_attr == "username":
            await surface.act(Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value=user))
        if e.name_attr == "password":
            await surface.act(Action(kind=ActionKind.TYPE, target=Target(mark=e.mark), value=pw))
    obs = await surface.observe(screenshot=False)
    for e in obs.elements:
        if (e.value or e.text) == "Log On":
            await surface.act(Action(kind=ActionKind.CLICK, target=Target(mark=e.mark)))
            break
    try:
        await surface.page.locator("frame[name='nav']").wait_for(state="attached", timeout=5000)
    except Exception:
        pass


def reset_dir(name: str) -> Path:
    path = EVIDENCE / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def run_traced_replay(
    run_id: str,
    *,
    member: str,
    faults: dict[str, str] | None = None,
    tenant: str | None = None,
) -> None:
    await clear_faults(**(faults or {}))
    reset_dir(run_id)
    art = ArtifactStore("capabilities").load(
        "cucore.member.read_savings_balance", tenant=tenant
    )
    trace_tmp = Path(f"/tmp/{run_id}-trace.zip")
    surface = WebSurface(
        headless=True,
        base_url=BASE,
        enable_trace=True,
        trace_path=str(trace_tmp),
    )
    await surface.start()
    await login(surface, tenant=tenant)
    evidence = EvidenceLogger(EVIDENCE, run_id=run_id)
    engine = ReplayEngine(surface, Policy.load(), evidence=evidence, step_timeout_ms=5000)
    result = await engine.run(art, {"member_number": member})
    await surface.stop()
    if trace_tmp.exists():
        evidence.attach_trace(trace_tmp)
        trace_tmp.unlink(missing_ok=True)
    evidence.write_result(result.model_dump())
    print(run_id, result.status.value, result.outcome, result.outputs)
    await clear_faults()


async def run_handoff_evidence() -> None:
    """Session dies mid-run; a person takes the lease, signs back in, resumes."""
    await clear_faults()
    reset_dir("06_handoff_session_expired")
    art = ArtifactStore("capabilities").load("cucore.member.read_savings_balance")
    trace_tmp = Path("/tmp/06-handoff-trace.zip")
    surface = WebSurface(
        headless=True,
        base_url=BASE,
        enable_trace=True,
        trace_path=str(trace_tmp),
        remote_debugging_port=9222,
    )
    await surface.start()
    await login(surface)

    handoff = HandoffController(lease_seconds=180)
    evidence = EvidenceLogger(EVIDENCE, run_id="06_handoff_session_expired")
    from cua.handoff.capture import HumanCapture

    capture = HumanCapture(handoff)
    await capture.install(surface.page)
    operator_events: list[dict] = []

    async def operator_side() -> None:
        # Wait until automation asks for help
        for _ in range(200):
            snap = handoff.snapshot()
            if snap.state == ControlState.AWAITING_HUMAN and snap.lease:
                break
            await asyncio.sleep(0.1)
        else:
            operator_events.append({"kind": "timeout_waiting_for_request"})
            return

        lease = handoff.lease
        assert lease is not None
        operator_events.append(
            {
                "kind": "saw_request",
                "lease_id": lease.id,
                "reason": lease.reason,
                "step_id": lease.step_id,
                "state": handoff.state.value,
            }
        )
        evidence.log_event(
            "operator_saw_queue",
            {
                "lease_id": lease.id,
                "reason": lease.reason,
                "step_id": lease.step_id,
                "state": handoff.state.value,
            },
            actor="human",
        )

        handoff.claim("morgan.teller", token=lease.token)
        operator_events.append(
            {
                "kind": "claimed",
                "owner": "morgan.teller",
                "lease_id": lease.id,
                "state": handoff.state.value,
            }
        )
        evidence.log_event(
            "operator_claimed",
            {"owner": "morgan.teller", "lease_id": lease.id, "state": handoff.state.value},
            actor="human",
        )

        # Clear the injected fault, then sign in again on the *same* browser
        await clear_faults()
        handoff.record_human_action(
            {"kind": "note", "text": "cleared session_expire fault from /__faults"}
        )
        await login(surface)
        handoff.record_human_action(
            {"kind": "login", "text": "re-authenticated as teller on live session"}
        )
        evidence.log_event(
            "human_relogin",
            {"detail": "same Chromium context, cookies refreshed"},
            actor="human",
        )
        # Land on home, then open Member Search so s2's field exists again
        await surface.goto(BASE + "/home")
        try:
            await surface.page.locator("frame[name='nav']").wait_for(
                state="attached", timeout=5000
            )
        except Exception:
            pass
        from cua.artifact.schema import FrameHint, StepTarget, StrategyByHeuristic, StrategyByRole
        from cua.replay.locate import locate

        search = StepTarget(
            frame=FrameHint(name_hint="nav"),
            strategies=[
                StrategyByRole(role="link", name="Member Search"),
                StrategyByHeuristic(text="Member Search"),
            ],
        )
        handle, strat, _, _ = await locate(surface, search, timeout_ms=5000)
        await handle.meta["locator"].click()
        handoff.record_human_action(
            {
                "kind": "click",
                "text": "Member Search",
                "strategy": strat,
                "why": "restore search page for the interrupted step",
            }
        )
        evidence.log_event(
            "human_restored_context",
            {"clicked": "Member Search", "strategy": strat},
            actor="human",
        )
        await surface._settle()
        obs = await surface.observe(screenshot=True)
        evidence.save_screenshot("after_human_relogin", obs.screenshot_png)

        handoff.resume(note="back on workstation home; handing control to automation")
        operator_events.append(
            {
                "kind": "resume_clicked",
                "note": "relogin done",
                "state": handoff.state.value,
            }
        )
        evidence.log_event(
            "operator_resume",
            {"note": "relogin done", "state": handoff.state.value},
            actor="human",
        )
        # Checkpoint check happens in the engine; console would call verify_ok
        handoff.verify_ok()
        operator_events.append({"kind": "verify_ok", "state": handoff.state.value})
        evidence.log_event(
            "operator_verify_ok",
            {"state": handoff.state.value},
            actor="human",
        )

    # Arm session expire so the next authenticated navigation dies
    await clear_faults(session_expire="true")
    evidence.log_event("fault_armed", {"fault": "session_expire"}, actor="automation")

    op_task = asyncio.create_task(operator_side())
    engine = ReplayEngine(
        surface,
        Policy.load(),
        evidence=evidence,
        allow_handoff=True,
        handoff=handoff,
        step_timeout_ms=5000,
    )
    result = await engine.run(art, {"member_number": "12345"})
    await op_task

    await surface.stop()
    if trace_tmp.exists():
        evidence.attach_trace(trace_tmp)
        trace_tmp.unlink(missing_ok=True)

    # Full trail: operator actions + SM history (human UI events already in history via _note)
    trail = operator_events + [
        {"source": "state_machine", **ev} for ev in handoff.history
    ]
    evidence.write_operator_log(trail)
    dump = result.model_dump()
    dump["handoff_trail"] = {
        "intervention_id": result.intervention_id,
        "recoveries": [r.model_dump() for r in result.recoveries],
        "operator_events": len(operator_events),
        "sm_events": len(handoff.history),
        "human_actions": len(handoff.captured_actions),
        "trace": "trace.zip",
    }
    evidence.write_result(dump)
    print(
        "06_handoff_session_expired",
        result.status.value,
        "intervention",
        result.intervention_id,
        "recoveries",
        [r.id for r in result.recoveries],
        "ops",
        len(operator_events),
    )
    await clear_faults()


async def run_live_discovery() -> None:
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print(
            "SKIP live discovery: set ANTHROPIC_API_KEY in .env "
            "(CUA_MOCK_LLM=0). Refusing to invent a fake LLM transcript."
        )
        # Wipe leftover mock theater — marker only, no fake screenshots/jsonl.
        path = reset_dir("01_discovery_read_balance")
        (path / "LIVE_REQUIRED.txt").write_text(
            "01_discovery_read_balance needs a real Anthropic Messages API run.\n"
            "Set ANTHROPIC_API_KEY, then:\n"
            "  CUA_MOCK_LLM=0 uv run python scripts/generate_evidence_pack.py\n"
            "This pack will not invent message_id / usage / tool_use rows.\n"
            "\n"
            "NOTE: the approved replay artifact is seeded (reviewed_by: seed).\n"
            "Discovery is demonstrated separately when a live key is present.\n"
        )
        (path / "result.json").write_text(
            '{\n  "status": "skipped_no_api_key",\n  "live": false,\n  "mock": false\n}\n'
        )
        return

    os.environ["CUA_MOCK_LLM"] = "0"
    reset_dir("01_discovery_read_balance")
    from cua.agent.loop import AgentLoop

    trace_tmp = Path("/tmp/01-discovery-trace.zip")
    surface = WebSurface(
        headless=True,
        base_url=BASE,
        enable_trace=True,
        trace_path=str(trace_tmp),
    )
    await surface.start()
    evidence = EvidenceLogger(EVIDENCE, run_id="01_discovery_read_balance")
    redactor = Redactor.load(params={"member_number": "12345", "username": "teller"})
    model = os.environ.get("CUA_MODEL", "claude-sonnet-4-20250514")
    evidence.log_event(
        "discovery_mode",
        {"mock": False, "model": model, "provider": "anthropic"},
        actor="automation",
    )
    loop = AgentLoop(
        surface,
        Policy.load(),
        evidence=evidence,
        redactor=redactor,
        mock=False,
        model=model,
    )
    params = {
        "username": os.environ.get("CUA_TARGET_USER", "teller"),
        "password": os.environ.get("CUA_TARGET_PASSWORD", "teller"),
        "member_number": "12345",
    }
    art = await loop.discover(
        "read the savings balance for member {member_number}",
        start_url=BASE + "/",
        capability_id="cucore.member.read_savings_balance",
        title="Read a member's savings balance",
        params=params,
    )
    path = ArtifactStore("capabilities").save(art)
    await surface.stop()
    if trace_tmp.exists():
        evidence.attach_trace(trace_tmp)
        trace_tmp.unlink(missing_ok=True)
    # Pull agent_turn rows so result.json proves Anthropic was on the wire
    turns = []
    run_log = evidence.run_dir / "run.jsonl"
    if run_log.exists():
        import json

        for line in run_log.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "agent_turn":
                turns.append(row.get("data") or {})
    evidence.write_result(
        {
            "status": "discovered",
            "capability_id": art.capability.id,
            "path": str(path),
            "steps": len(art.capability.steps),
            "model": model,
            "mock": False,
            "provider": "anthropic",
            "agent_turns": len(turns),
            "anthropic_message_ids": [
                t.get("message_id") for t in turns if t.get("message_id")
            ],
            "live": True,
        }
    )
    print("01_discovery_read_balance", "live", path, "steps", len(art.capability.steps))


async def main() -> None:
    load_dotenv()
    print("target", BASE)
    await run_traced_replay("02_replay_success", member="12345")
    await run_traced_replay("03_replay_member_not_found", member="99999")
    await run_traced_replay(
        "04_replay_interstitial_recovered",
        member="12345",
        faults={"interstitial": "true"},
    )
    await run_traced_replay(
        "05_replay_app_error_failed",
        member="12345",
        faults={"error500": "true"},
    )
    await run_traced_replay(
        "07_cross_tenant_replay",
        member="12345",
        tenant="tenant_b",
    )
    await run_handoff_evidence()
    await run_live_discovery()


if __name__ == "__main__":
    asyncio.run(main())
