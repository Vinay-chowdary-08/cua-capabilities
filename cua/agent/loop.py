"""observe → redact → model tool call → policy → act discovery loop."""

from __future__ import annotations

import base64
import os
import time
from typing import Any

from cua.agent.prompts import MOCK_READ_BALANCE, SYSTEM, TOOLS
from cua.agent.recorder import ArtifactRecorder
from cua.artifact.schema import CapabilityArtifact
from cua.evidence.logger import EvidenceLogger
from cua.handoff.control import HandoffController
from cua.safety.policy import Policy, PolicyRequire
from cua.safety.redact import Redactor
from cua.surface.base import Action, ActionKind, Observation, Surface, Target


class AgentLoop:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        *,
        evidence: EvidenceLogger | None = None,
        handoff: HandoffController | None = None,
        redactor: Redactor | None = None,
        mock: bool | None = None,
        model: str | None = None,
        max_steps: int = 25,
        wall_timeout_sec: int = 300,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.handoff = handoff
        self.redactor = redactor or Redactor.load()
        self.mock = (
            mock
            if mock is not None
            else os.environ.get("CUA_MOCK_LLM", "1") not in {"0", "false", "False"}
        )
        self.model = model or os.environ.get("CUA_MODEL", "claude-sonnet-4-20250514")
        self.max_steps = max_steps
        self.wall_timeout_sec = wall_timeout_sec
        self._mock_i = 0
        self._noop = 0
        self._last_sig: str | None = None

    async def discover(
        self,
        goal: str,
        *,
        start_url: str,
        capability_id: str,
        title: str,
        params: dict[str, str],
        scripted_login: bool = True,
    ) -> CapabilityArtifact:
        self.redactor.params = {k: v for k, v in params.items() if k != "password"}
        recorder = ArtifactRecorder(
            capability_id=capability_id, title=title, goal=goal, params=params
        )
        await self.surface.goto(start_url)
        started = time.monotonic()
        history: list[str] = []

        if scripted_login:
            await self._scripted_login(params, recorder)

        for step_i in range(self.max_steps):
            if time.monotonic() - started > self.wall_timeout_sec:
                break
            if self.handoff and not self.handoff.owns_control():
                await self.handoff.wait_until_released()

            obs = await self.surface.observe()
            sig = f"{obs.url}|{obs.title}|{obs.visible_text_excerpt[:200]}"
            if sig == self._last_sig:
                self._noop += 1
            else:
                self._noop = 0
                self._last_sig = sig
            if self._noop >= 3:
                if self.handoff:
                    await self.handoff.request(reason="stuck: repeated identical observations")
                    await self.handoff.wait_until_released()
                break

            safe_text = self.redactor.redact_observation_text(obs)
            safe_png = self.redactor.redact_screenshot(
                obs.screenshot_png, obs.elements, sensitive_values=list(params.values())
            )
            if self.evidence:
                self.evidence.save_screenshot(f"step_{step_i}", safe_png)
                self.evidence.log_event(
                    "observe",
                    {"url": obs.url, "excerpt": safe_text[:500]},
                    actor="automation",
                )

            tool_name, tool_input, turn_meta = await self._decide(
                goal, obs, safe_text, history, params, safe_png=safe_png
            )
            if self.evidence:
                self.evidence.log_event(
                    "agent_turn",
                    {
                        "step_i": step_i,
                        "tool": tool_name,
                        "input": tool_input,
                        "mock": self.mock,
                        **turn_meta,
                    },
                    actor="agent",
                )
            if tool_name == "done":
                if self.evidence:
                    self.evidence.log_event("done", tool_input, actor="agent")
                break
            if tool_name == "request_human":
                if self.handoff:
                    await self.handoff.request(reason=str(tool_input.get("reason", "requested")))
                    await self.handoff.wait_until_released()
                history.append(f"human:{tool_input}")
                continue

            action, element = self._tool_to_action(tool_name, tool_input, obs, params)
            if action is None:
                if self.evidence:
                    self.evidence.log_event(
                        "tool_unresolved",
                        {"tool": tool_name, "input": tool_input},
                        actor="automation",
                    )
                continue

            decision = self.policy.check(action, current_url=obs.url or "", mode="discovery")
            if decision.require == PolicyRequire.BLOCK or not decision.allowed:
                history.append(f"blocked:{decision.reason}")
                if self.evidence:
                    self.evidence.log_event(
                        "policy_blocked",
                        {"reason": decision.reason, "tool": tool_name},
                        actor="automation",
                    )
                continue
            if decision.require == PolicyRequire.CONFIRM:
                if self.handoff:
                    await self.handoff.request(reason=decision.reason)
                    await self.handoff.wait_until_released()
                else:
                    history.append(f"needs_confirm:{decision.reason}")
                    # still record the step boundary
                    await recorder.record(
                        self.surface, action, element=element, intent=tool_name, observation=obs
                    )
                    break

            human_intent = _human_intent(tool_name, tool_input, element)
            await recorder.record(
                self.surface,
                action,
                element=element,
                intent=human_intent,
                observation=obs,
            )
            try:
                if action.kind == ActionKind.EXTRACT and element:
                    # capture output via read
                    text = element.text or ""
                    action.meta["extracted"] = text
                else:
                    await self.surface.act(action)
                history.append(f"ok:{tool_name}:{tool_input}")
                if self.evidence:
                    self.evidence.log_event(
                        "act_ok",
                        {
                            "tool": tool_name,
                            "mark": tool_input.get("mark"),
                            "intent": human_intent,
                        },
                        actor="automation",
                    )
            except Exception as e:
                history.append(f"err:{e}")
                self._noop += 1
                if self.evidence:
                    self.evidence.log_event(
                        "act_err",
                        {"tool": tool_name, "error": str(e)},
                        actor="automation",
                    )

        artifact = recorder.build(summary=history[-1] if history else None)
        if not self.mock and artifact.capability.provenance:
            artifact.capability.provenance.discovered_by = {
                "run_id": recorder.run_id,
                "model": self.model,
                "provider": "anthropic",
                "mock": False,
            }
        if self.evidence:
            self.evidence.log_event(
                "discover_end",
                {
                    "artifact_id": artifact.capability.id,
                    "steps": len(artifact.capability.steps),
                    "mock": self.mock,
                    "model": None if self.mock else self.model,
                },
            )
            (self.evidence.run_dir / "trace_meta.json").write_text(
                __import__("json").dumps(
                    {"history": history, "artifact_id": artifact.capability.id},
                    indent=2,
                    default=str,
                )
            )
        return artifact

    async def _scripted_login(self, params: dict[str, str], recorder: ArtifactRecorder) -> None:
        """Credentials from env/params — never sent to the LLM."""
        obs = await self.surface.observe()
        user_el = next((e for e in obs.elements if e.name_attr == "username"), None)
        pw_el = next((e for e in obs.elements if e.name_attr == "password"), None)
        btn = next(
            (e for e in obs.elements if (e.value or e.text or "") == "Log On"),
            None,
        )
        if user_el:
            await self.surface.act(
                Action(
                    kind=ActionKind.TYPE,
                    target=Target(mark=user_el.mark),
                    value=params.get("username", "teller"),
                )
            )
        if pw_el:
            await self.surface.act(
                Action(
                    kind=ActionKind.TYPE,
                    target=Target(mark=pw_el.mark),
                    value=params.get("password", "teller"),
                )
            )
        if btn:
            await self.surface.act(Action(kind=ActionKind.CLICK, target=Target(mark=btn.mark)))
        # Do not record password typing into artifact steps with secrets
        if self.evidence:
            self.evidence.log_event("scripted_login", {"ok": True}, actor="automation")

    async def _decide(
        self,
        goal: str,
        obs: Observation,
        safe_text: str,
        history: list[str],
        params: dict[str, str],
        *,
        safe_png: bytes,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if self.mock:
            tool, data = self._mock_decide(obs, params)
            return tool, data, {"provider": "mock", "model": None}
        return await self._llm_decide(goal, obs, safe_text, history, safe_png=safe_png)

    def _mock_decide(
        self, obs: Observation, params: dict[str, str]
    ) -> tuple[str, dict[str, Any]]:
        if self._mock_i >= len(MOCK_READ_BALANCE):
            return "done", {"summary": "budget"}
        step = MOCK_READ_BALANCE[self._mock_i]
        self._mock_i += 1
        tool = step["tool"]
        raw = dict(step["input"])
        # Resolve symbolic marks to real marks
        mark_key = raw.get("mark")
        if isinstance(mark_key, str):
            el = _find_element(obs, mark_key)
            if el:
                raw["mark"] = el.mark
            else:
                # skip ahead if not found (e.g. already logged in)
                return self._mock_decide(obs, params)
        if "text" in raw and isinstance(raw["text"], str):
            for k, v in params.items():
                raw["text"] = raw["text"].replace("{" + k + "}", v)
        return tool, raw

    async def _llm_decide(
        self,
        goal: str,
        obs: Observation,
        safe_text: str,
        history: list[str],
        *,
        safe_png: bytes,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        import anthropic

        client = anthropic.Anthropic()
        # Element list uses already-redacted observation text cues only — never raw secrets.
        elements = "\n".join(
            f"[{e.mark}] {e.role} "
            f"name={self.redactor.redact_text(e.name or '')!r} "
            f"near={self.redactor.redact_text(e.near_label or '')!r} "
            f"frame={e.frame_path}"
            for e in obs.elements[:40]
        )
        # CRITICAL: send redacted pixels to the hosted model, not the raw shot.
        b64 = base64.standard_b64encode(safe_png).decode("ascii")
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            },
            {
                "type": "text",
                "text": (
                    f"Goal: {goal}\nHistory:\n" + "\n".join(history[-8:]) +
                    f"\nURL: {obs.url}\nElements:\n{elements}\nText:\n{safe_text[:2000]}\n"
                    "Call exactly one tool."
                ),
            },
        ]
        msg = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=[{"role": "user", "content": content}],
        )
        usage = getattr(msg, "usage", None)
        meta: dict[str, Any] = {
            "provider": "anthropic",
            "model": getattr(msg, "model", self.model),
            "message_id": getattr(msg, "id", None),
            "stop_reason": getattr(msg, "stop_reason", None),
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
                "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
            },
        }
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                meta["tool_use_id"] = getattr(block, "id", None)
                return block.name, dict(block.input), meta
        return "done", {"summary": "no_tool"}, meta

    def _tool_to_action(
        self,
        tool: str,
        data: dict[str, Any],
        obs: Observation,
        params: dict[str, str],
    ) -> tuple[Action | None, Any]:
        mark = data.get("mark")
        el = next((e for e in obs.elements if e.mark == mark), None) if mark is not None else None
        if tool == "click":
            return Action(kind=ActionKind.CLICK, target=Target(mark=mark)), el
        if tool == "type":
            return (
                Action(
                    kind=ActionKind.TYPE,
                    target=Target(mark=mark),
                    value=str(data.get("text", "")),
                    is_param=data.get("is_param"),
                ),
                el,
            )
        if tool == "select":
            return (
                Action(
                    kind=ActionKind.SELECT,
                    target=Target(mark=mark),
                    option=str(data.get("option", "")),
                ),
                el,
            )
        if tool == "press":
            return Action(kind=ActionKind.PRESS, key=str(data.get("key", "Enter"))), None
        if tool == "extract":
            return (
                Action(
                    kind=ActionKind.EXTRACT,
                    target=Target(mark=mark),
                    output_name=str(data.get("output_name", "value")),
                ),
                el,
            )
        return None, None


def _find_element(obs: Observation, key: str) -> Any:
    key_l = key.lower()
    for e in obs.elements:
        if e.name_attr == key:
            return e
        if (e.value or "").lower() == key_l:
            return e
        if (e.text or "").lower() == key_l:
            return e
        if key_l in (e.text or "").lower():
            return e
        if key_l in (e.name or "").lower():
            return e
    return None


def _human_intent(tool: str, data: dict[str, Any], element: Any) -> str:
    """Plain-language intent for the artifact — not raw tool JSON."""
    label = ""
    if element is not None:
        label = (element.text or element.name or element.near_label or element.name_attr or "").strip()
    mark = data.get("mark")
    if tool == "click":
        return f"Click {label}" if label else f"Click mark {mark}"
    if tool == "type":
        param = data.get("is_param")
        target = label or "field"
        if param:
            return f"Type {{{{inputs.{param}}}}} into {target}"
        return f"Type into {target}"
    if tool == "select":
        return f"Select {data.get('option')!r} in {label or 'dropdown'}"
    if tool == "press":
        return f"Press {data.get('key', 'Enter')}"
    if tool == "extract":
        return f"Extract {data.get('output_name', 'value')} from {label or 'cell'}"
    return tool
