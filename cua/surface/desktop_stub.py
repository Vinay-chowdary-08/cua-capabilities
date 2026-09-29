"""Desktop surface seam — same Surface protocol; not implemented.

Maps each method to platform accessibility stacks so the design does not
paint you into a web-only corner:

| Method            | Windows                         | macOS            | Cross-platform   |
|-------------------|---------------------------------|------------------|------------------|
| observe()         | UI Automation tree + screenshot | AXUIElement tree | pywinauto dump   |
| act(click/type)   | InvokePattern / ValuePattern    | AXPress / AXSet  | pywinauto wrap   |
| resolve(target)   | Condition on Name/ControlType   | AXRole + AXTitle | criteria dict    |
| read(target)      | Name/Value patterns             | AXValue          | window_text()  |
| wait_for(cond)    | UIA event hooks + poll          | AX observers     | timing loops     |
| pause_for_human() | Release input focus / overlay   | same             | same             |

Artifacts stay surface-agnostic: strategies use role/name/text — the same
vocabulary UIA ControlType and AXRole already speak.
"""

from __future__ import annotations

from cua.surface.base import Action, Condition, Observation, ResolvedHandle, Target


class DesktopStub:
    """Documented seam only — raises NotImplementedError if used."""

    async def start(self) -> None:
        raise NotImplementedError(
            "DesktopStub.start: launch app under test; attach via UIA/AX/pywinauto."
        )

    async def stop(self) -> None:
        return None

    async def observe(self) -> Observation:
        raise NotImplementedError(
            "DesktopStub.observe: walk UIA/AX tree, assign marks, screenshot+annotate."
        )

    async def act(self, action: Action) -> None:
        raise NotImplementedError(
            "DesktopStub.act: map ActionKind to InvokePattern / AXPress / type_keys."
        )

    async def resolve(self, target: Target, timeout_ms: int) -> ResolvedHandle:
        raise NotImplementedError(
            "DesktopStub.resolve: exact-one match on ControlType/Name (UIA) or AXRole/AXTitle."
        )

    async def read(self, target: Target) -> str:
        raise NotImplementedError("DesktopStub.read: ValuePattern / AXValue.")

    async def wait_for(self, cond: Condition, timeout_ms: int) -> bool:
        raise NotImplementedError("DesktopStub.wait_for: poll accessibility tree until timeout.")

    async def pause_for_human(self) -> None:
        raise NotImplementedError("DesktopStub.pause_for_human: release automation focus.")

    async def goto(self, url: str) -> None:
        raise NotImplementedError("Desktop surface has no goto(url); use launch/attach.")
