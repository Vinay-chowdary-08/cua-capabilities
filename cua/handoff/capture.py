"""Capture human actions via page.expose_binding + init script across frames."""

from __future__ import annotations

from typing import Any

from cua.handoff.control import HandoffController
from cua.safety.redact import Redactor


CAPTURE_JS = """
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const send = (payload) => {
    try { window.cuaHumanAction(payload); } catch (e) {}
  };
  const desc = (el) => ({
    tag: el.tagName,
    name: el.getAttribute('name'),
    role: el.getAttribute('role'),
    value_redacted: el.type === 'password' ? '[SECRET]' : (el.value || '').slice(0, 80),
    text: (el.innerText || '').slice(0, 80),
  });
  document.addEventListener('click', (ev) => {
    const el = ev.target;
    if (!el) return;
    send({kind: 'click', ...desc(el)});
  }, true);
  document.addEventListener('change', (ev) => {
    const el = ev.target;
    if (!el) return;
    send({kind: 'change', ...desc(el)});
  }, true);
  document.addEventListener('submit', (ev) => {
    send({kind: 'submit', tag: 'FORM'});
  }, true);
})();
"""


class HumanCapture:
    def __init__(self, controller: HandoffController, redactor: Redactor | None = None) -> None:
        self.controller = controller
        self.redactor = redactor or Redactor.load()

    async def install(self, page: Any) -> None:
        async def _on_action(source: Any, payload: dict[str, Any]) -> None:  # noqa: ARG001
            if "value_redacted" in payload and payload["value_redacted"]:
                payload["value_redacted"] = self.redactor.redact_text(str(payload["value_redacted"]))
            self.controller.record_human_action(payload)

        await page.expose_binding("cuaHumanAction", _on_action)
        await page.add_init_script(CAPTURE_JS)
        # Also inject into existing frames
        for frame in page.frames:
            try:
                await frame.evaluate(CAPTURE_JS)
            except Exception:
                continue

    def dump(self) -> list[dict[str, Any]]:
        return list(self.controller.captured_actions)
