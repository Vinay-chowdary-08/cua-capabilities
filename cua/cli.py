"""Typer CLI: serve-target / discover / approve / replay / operator / list."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console

app = typer.Typer(add_completion=False, no_args_is_help=True, help="CUA capability discovery & replay")
console = Console()


def _env() -> None:
    load_dotenv()
    os.environ.setdefault("CUA_TARGET_URL", "http://127.0.0.1:8800")
    os.environ.setdefault("CUA_TARGET_PORT", "8800")
    os.environ.setdefault("CUA_OPERATOR_PORT", "8900")
    os.environ.setdefault("CUA_MOCK_LLM", "1")
    os.environ.setdefault("CUA_TARGET_USER", "teller")
    os.environ.setdefault("CUA_TARGET_PASSWORD", "teller")


@app.command("serve-target")
def serve_target(
    variant: str = typer.Option("tenant_a", "--variant"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8800, "--port"),
) -> None:
    """Serve the hostile legacy CU core."""
    _env()
    os.environ["CUA_TARGET_TENANT"] = variant
    os.environ["CUA_TARGET_PORT"] = str(port)
    import uvicorn

    console.print(f"[cyan]Target[/cyan] http://{host}:{port} variant={variant}")
    uvicorn.run("target_app.server:app", host=host, port=port, log_level="info")


@app.command("operator")
def operator_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8900, "--port"),
) -> None:
    """Operator console."""
    _env()
    import uvicorn

    console.print(f"[cyan]Operator[/cyan] http://{host}:{port}/operator")
    uvicorn.run("cua.handoff.console:app", host=host, port=port, log_level="info")


@app.command("discover")
def discover_cmd(
    goal: str = typer.Option(..., "--goal"),
    param: list[str] = typer.Option([], "--param", help="key=value"),
    url: str = typer.Option("http://127.0.0.1:8800", "--url"),
    app_name: str = typer.Option("cucore", "--app"),
    out: str = typer.Option("capabilities", "--out"),
    name: str = typer.Option("cucore.member.read_savings_balance", "--id"),
    title: str = typer.Option("Read a member's savings balance", "--title"),
    headed: bool = typer.Option(False, "--headed"),
) -> None:
    """Discover a capability (mock LLM by default; set CUA_MOCK_LLM=0 for live)."""
    _env()
    params = _parse_params(param)
    params.setdefault("username", os.environ.get("CUA_TARGET_USER", "teller"))
    params.setdefault("password", os.environ.get("CUA_TARGET_PASSWORD", "teller"))
    asyncio.run(
        _discover(
            goal=goal,
            params=params,
            url=url,
            out=out,
            capability_id=name,
            title=title,
            headed=headed,
        )
    )


async def _discover(
    *,
    goal: str,
    params: dict[str, str],
    url: str,
    out: str,
    capability_id: str,
    title: str,
    headed: bool,
) -> None:
    from cua.agent.loop import AgentLoop
    from cua.artifact.store import ArtifactStore
    from cua.evidence.logger import EvidenceLogger
    from cua.safety.policy import Policy
    from cua.safety.redact import Redactor
    from cua.surface.web import WebSurface

    evidence = EvidenceLogger(os.environ.get("CUA_EVIDENCE_DIR", "evidence"))
    redactor = Redactor.load(params={k: v for k, v in params.items() if k != "password"})
    surface = WebSurface(headless=not headed, base_url=url)
    await surface.start()
    try:
        loop = AgentLoop(
            surface,
            Policy.load(),
            evidence=evidence,
            redactor=redactor,
        )
        art = await loop.discover(
            goal,
            start_url=url + "/",
            capability_id=capability_id,
            title=title,
            params=params,
        )
        path = ArtifactStore(out).save(art)
        ArtifactStore(out).export_json_schema()
        console.print(f"[green]Saved[/green] {art.capability.id} → {path}")
        console.print(f"evidence={evidence.run_dir}")
    finally:
        await surface.stop()


@app.command("approve")
def approve_cmd(path_or_id: str = typer.Argument(...)) -> None:
    _env()
    from cua.artifact.store import ArtifactStore

    store = ArtifactStore("capabilities")
    cap_id = path_or_id
    if path_or_id.endswith(".yaml"):
        art = store.load_path(Path(path_or_id))
        cap_id = art.capability.id
    art = store.approve(cap_id)
    console.print(f"[green]Approved[/green] {art.capability.id} status={art.capability.status.value}")


@app.command("replay")
def replay_cmd(
    capability_id: str = typer.Argument(...),
    input_kv: list[str] = typer.Option([], "--input"),
    fault: Optional[str] = typer.Option(None, "--fault", help="comma list: interstitial,slow,error500,session_expire"),
    tenant: Optional[str] = typer.Option(None, "--tenant"),
    allow_handoff: bool = typer.Option(False, "--allow-handoff"),
    confirm_irreversible: bool = typer.Option(False, "--confirm-irreversible"),
    url: str = typer.Option("http://127.0.0.1:8800", "--url"),
    headed: bool = typer.Option(False, "--headed"),
    evidence_name: Optional[str] = typer.Option(None, "--evidence-dir"),
) -> None:
    _env()
    asyncio.run(
        _replay(
            capability_id=capability_id,
            inputs=_parse_params(input_kv),
            fault=fault,
            tenant=tenant,
            allow_handoff=allow_handoff,
            confirm_irreversible=confirm_irreversible,
            url=url,
            headed=headed,
            evidence_name=evidence_name,
        )
    )


async def _replay(
    *,
    capability_id: str,
    inputs: dict[str, str],
    fault: str | None,
    tenant: str | None,
    allow_handoff: bool,
    confirm_irreversible: bool,
    url: str,
    headed: bool,
    evidence_name: str | None,
) -> None:
    from cua.artifact.store import ArtifactStore
    from cua.evidence.logger import EvidenceLogger
    from cua.handoff.capture import HumanCapture
    from cua.handoff.control import get_shared_controller
    from cua.handoff.remote import RemoteHandoffController
    from cua.replay.engine import ReplayEngine
    from cua.replay.result import ReplayStatus
    from cua.safety.policy import Policy
    from cua.surface.web import WebSurface

    if fault:
        await _set_faults(url, fault)

    store = ArtifactStore("capabilities")
    # strip path/suffix
    cid = capability_id.replace(".yaml", "").split("/")[-1]
    art = store.load(cid, tenant=tenant)
    evidence = EvidenceLogger(
        os.environ.get("CUA_EVIDENCE_DIR", "evidence"),
        run_id=evidence_name,
    )
    # Cross-process: CUA_OPERATOR_URL=http://127.0.0.1:8900 talks to the console.
    # Same-process fallback: SHARED_CONTROLLER (only works if operator imported in-proc).
    operator_url = os.environ.get("CUA_OPERATOR_URL", "").strip()
    handoff: Any = None
    if allow_handoff:
        if operator_url:
            handoff = RemoteHandoffController(operator_url)
            console.print(f"[cyan]Handoff[/cyan] remote → {operator_url}")
        else:
            handoff = get_shared_controller()
            console.print(
                "[yellow]Handoff[/yellow] in-process SHARED_CONTROLLER "
                "(set CUA_OPERATOR_URL for dual-terminal operator)"
            )
    surface = WebSurface(
        headless=not headed,
        base_url=url,
        remote_debugging_port=9222 if allow_handoff else None,
    )
    await surface.start()
    capture: HumanCapture | None = None
    if handoff is not None and not isinstance(handoff, RemoteHandoffController):
        capture = HumanCapture(handoff)
        await capture.install(surface.page)
    try:
        # Scripted login before replay
        await _login(surface, url)
        engine = ReplayEngine(
            surface,
            Policy.load(),
            evidence=evidence,
            allow_handoff=allow_handoff,
            handoff=handoff,
            confirm_irreversible=confirm_irreversible,
        )
        # Skip login steps if artifact includes them — for seeded artifact we start at nav
        result = await engine.run(art, inputs)
        console.print(json.dumps(json.loads(result.model_dump_json()), indent=2))
        if result.status == ReplayStatus.FAILED:
            raise typer.Exit(code=1)
    finally:
        await surface.stop()
        if fault:
            await _set_faults(url, "clear")


@app.command("list")
def list_cmd() -> None:
    _env()
    from cua.artifact.store import ArtifactStore

    for a in ArtifactStore("capabilities").list():
        c = a.capability
        console.print(f"{c.id}  v{c.version}  {c.status.value}  risk={c.risk.value}  steps={len(c.steps)}")


@app.command("seed")
def seed_cmd() -> None:
    """Write the canonical read_savings_balance capability + tenant_b overlay."""
    _env()
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
        FrameHint,
        OutcomeSpec,
        Provenance,
        Recoverable,
        RecoverableHandle,
        RiskClass,
        Sensitivity,
        StepTarget,
        StrategyByHeuristic,
        StrategyByLabel,
        StrategyByNameAttr,
        StrategyByRole,
        StrategyByTableCell,
        StrategyByText,
        SuccessCondition,
    )
    from cua.artifact.store import ArtifactStore

    store = ArtifactStore("capabilities")
    art = CapabilityArtifact(
        schema_version="1.0",
        capability=CapabilityBody(
            id="cucore.member.read_savings_balance",
            version="1.2.0",
            title="Read a member's savings balance",
            description="Looks up a member by number and returns the Share Savings balance.",
            status=CapabilityStatus.APPROVED,
            app=AppInfo(vendor="acme-cucore", product="CU Core Teller", compatible_versions=">=7.2,<8"),
            inputs={
                "member_number": FieldSpec(
                    type="string", pattern="^[0-9]{4,10}$", sensitivity=Sensitivity.INTERNAL_ID
                )
            },
            outputs={
                "savings_balance": FieldSpec(
                    type="number",
                    format="currency",
                    sensitivity=Sensitivity.FINANCIAL,
                    redact_in_logs=True,
                ),
            },
            risk=RiskClass.READ_ONLY,
            preconditions=[{"kind": "authenticated_session"}],
            steps=[
                CapabilityStep(
                    id="s1_open_search",
                    intent="Open member search from left nav",
                    action="click",
                    target=StepTarget(
                        frame=FrameHint(name_hint="nav", index_fallback=0),
                        strategies=[
                            StrategyByRole(role="link", name="Member Search"),
                            StrategyByText(text="Member Search", exact=True),
                            StrategyByHeuristic(text="Member Search"),
                        ],
                    ),
                ),
                CapabilityStep(
                    id="s2_type_member",
                    intent="Type member number into search field",
                    action="type",
                    value="{{inputs.member_number}}",
                    target=StepTarget(
                        frame=FrameHint(name_hint="main"),
                        strategies=[
                            StrategyByLabel(label="Member #"),
                            StrategyByNameAttr(name="qry"),
                        ],
                    ),
                ),
                CapabilityStep(
                    id="s3_submit",
                    intent="Submit member search",
                    action="click",
                    target=StepTarget(
                        frame=FrameHint(name_hint="main"),
                        strategies=[
                            StrategyByHeuristic(text="Find Member"),
                            StrategyByRole(role="button", name="Find Member"),
                        ],
                    ),
                    expect=ExpectAnyOf(
                        any_of=[
                            ExpectTextVisible(text="Test Member", then="continue"),
                            ExpectTextVisible(text="No member found", outcome="member_not_found"),
                            ExpectTextVisible(text="not authorized", outcome="permission_denied"),
                        ]
                    ),
                ),
                CapabilityStep(
                    id="s4_open",
                    intent="Open the member row",
                    action="click",
                    target=StepTarget(
                        frame=FrameHint(name_hint="main"),
                        strategies=[
                            StrategyByRole(role="link", name="Open"),
                            StrategyByText(text="Open", exact=True),
                            StrategyByHeuristic(text="Open"),
                        ],
                    ),
                    expect=ExpectAnyOf(
                        any_of=[
                            ExpectTextVisible(text="Share Savings", then="continue"),
                            ExpectTextVisible(text="No member found", outcome="member_not_found"),
                            ExpectTextVisible(text="not authorized", outcome="permission_denied"),
                        ]
                    ),
                ),
                CapabilityStep(
                    id="s5_extract",
                    intent="Extract Share Savings balance",
                    action="extract",
                    extract={
                        "savings_balance": ExtractField(
                            target=StepTarget(
                                frame=FrameHint(name_hint="main"),
                                strategies=[
                                    StrategyByTableCell(
                                        row_header="Share Savings", column_header="Balance"
                                    )
                                ],
                            ),
                            parse="currency",
                        )
                    },
                ),
            ],
            outcomes={
                "member_not_found": OutcomeSpec(kind="business"),
                "permission_denied": OutcomeSpec(kind="business"),
            },
            recoverables=[
                Recoverable(
                    id="system_notice",
                    detect=ExpectTextVisible(text="System notice"),
                    handle=RecoverableHandle(
                        action="click",
                        target=StepTarget(
                            strategies=[
                                StrategyByRole(role="button", name="Acknowledge"),
                                StrategyByHeuristic(text="Acknowledge"),
                            ]
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
            ],
            success=[SuccessCondition(kind="output_present", output="savings_balance")],
            provenance=Provenance(
                goal_text="look up member {{member_number}} and read their savings balance",
                reviewed_by="seed",
            ),
        ),
    )
    path = store.save(art)
    store.export_json_schema()

    # tenant_b overlay — keep failing tenant_a labels FIRST so drift is real, not erased.
    overlay_dir = Path("capabilities/overrides/tenant_b")
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay = {
        "steps_by_id": {
            "s1_open_search": {
                "target": {
                    "strategies": [
                        {"by": "role", "role": "link", "name": "Member Search"},
                        {"by": "role", "role": "link", "name": "Holder Lookup"},
                        {"by": "text", "text": "Holder Lookup", "exact": True},
                        {"by": "heuristic", "text": "Holder Lookup"},
                    ]
                }
            },
            "s2_type_member": {
                "target": {
                    "strategies": [
                        {"by": "label_proximity", "label": "Member #"},
                        {"by": "label_proximity", "label": "Account Holder No."},
                        {"by": "name_attr", "name": "qry"},
                    ]
                }
            },
            "s3_submit": {
                "target": {
                    "strategies": [
                        {"by": "heuristic", "text": "Find Member"},
                        {"by": "heuristic", "text": "Search"},
                        {"by": "role", "role": "button", "name": "Search"},
                    ]
                }
            },
            "s5_extract": {
                "extract": {
                    "savings_balance": {
                        "target": {
                            "frame": {"name_hint": "main"},
                            "strategies": [
                                {
                                    "by": "table_cell",
                                    "row_header": "Share Savings",
                                    "column_header": "Avail Bal",
                                },
                                {
                                    "by": "table_cell",
                                    "row_header": "Share Savings",
                                    "column_header": "Balance",
                                },
                            ],
                        },
                        "parse": "currency",
                    }
                }
            },
        },
    }
    with (overlay_dir / "cucore.member.read_savings_balance.yaml").open("w") as f:
        yaml.safe_dump(overlay, f, sort_keys=False)
    console.print(f"[green]Seeded[/green] {path} + tenant_b overlay")


def _parse_params(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got {item}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


async def _set_faults(url: str, fault: str) -> None:
    base = url.rstrip("/")
    if fault == "clear":
        async with httpx.AsyncClient() as client:
            await client.get(f"{base}/__faults", params={"clear": "true"})
        return
    params: dict[str, str] = {}
    for part in fault.split(","):
        part = part.strip()
        if part == "slow":
            params["slow"] = "true"
        elif part == "error500":
            params["error500"] = "true"
        elif part == "interstitial":
            params["interstitial"] = "true"
        elif part == "session_expire":
            params["session_expire"] = "true"
        elif part == "confirm_dialog":
            params["confirm_dialog"] = "true"
    async with httpx.AsyncClient() as client:
        await client.get(f"{base}/__faults", params=params)


async def _login(surface: Any, url: str) -> None:
    from cua.surface.base import Action, ActionKind, Target

    await surface.goto(url + "/")
    obs = await surface.observe(screenshot=False)
    # If already in app, skip
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
    # Wait until nav frame exists (DOM condition, not blind sleep)
    if hasattr(surface, "page"):
        try:
            await surface.page.locator("frame[name='nav']").wait_for(
                state="attached", timeout=5000
            )
        except Exception:
            pass


if __name__ == "__main__":
    app()
