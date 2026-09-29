"""FastAPI operator console at /operator — intervention queue, claim/resume/verify/abort."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cua.handoff.control import ControlState, HandoffController, get_shared_controller


def create_console_app(controller: HandoffController | None = None) -> FastAPI:
    ctrl = controller or get_shared_controller()
    app = FastAPI(title="CUA Operator Console")

    @app.get("/operator", response_class=HTMLResponse)
    async def operator(request: Request) -> HTMLResponse:
        snap = ctrl.snapshot()
        rows = ""
        for it in reversed(snap.interventions[-10:]):
            rows += (
                f"<tr><td>{it.id}</td><td>{it.capability_id or '-'}</td>"
                f"<td>{it.step_id or '-'}</td><td>{it.reason}</td>"
                f"<td>{it.state.value}</td></tr>"
            )
        lease = snap.lease
        html = f"""<!DOCTYPE html>
<html><head><title>CUA Operator</title>
<meta http-equiv="refresh" content="2">
<style>
 body {{ font-family: Georgia, serif; margin: 1.5rem; background:#f4f1ea; color:#1c1a16; }}
 table {{ border-collapse: collapse; width: 100%; background:#fff; }}
 td, th {{ border:1px solid #ccc; padding:6px 8px; font-size:14px; }}
 button {{ font:inherit; padding:6px 12px; margin-right:6px; background:#1c3d5a; color:#fff; border:0; cursor:pointer; }}
 button.warn {{ background:#8a3b12; }}
 .card {{ background:#fff; border:1px solid #cfc6b6; padding:1rem; margin-bottom:1rem; max-width:720px; }}
</style></head><body>
 <h1>Operator console</h1>
 <div class="card">
  <p>State: <strong>{snap.state.value}</strong>
  {" · lease " + lease.id + " · " + lease.reason if lease else ""}</p>
  <form method="post" action="/operator/claim" style="display:inline"><button>Take control</button></form>
  <form method="post" action="/operator/resume" style="display:inline">
    <input name="note" placeholder="optional note">
    <button>Resume → VERIFYING</button>
  </form>
  <form method="post" action="/operator/verify-ok" style="display:inline">
    <button>Verify OK (hand back)</button>
  </form>
  <form method="post" action="/operator/verify-fail" style="display:inline">
    <input name="reason" placeholder="why verify failed">
    <button class="warn">Verify failed</button>
  </form>
  <form method="post" action="/operator/abort" style="display:inline"><button class="warn">Abort</button></form>
 </div>
 <h2>Interventions</h2>
 <table>
  <tr><th>id</th><th>capability</th><th>step</th><th>reason</th><th>state</th></tr>
  {rows or "<tr><td colspan=5>None</td></tr>"}
 </table>
 <p style="color:#666;font-size:12px;">Auto-refresh 2s · port {os.environ.get('CUA_OPERATOR_PORT','8900')}
 · Resume does <em>not</em> auto-verify — click Verify OK after the checkpoint holds.</p>
</body></html>"""
        return HTMLResponse(html)

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        return JSONResponse(ctrl.snapshot().model_dump())

    @app.post("/operator/claim")
    async def claim() -> RedirectResponse:
        try:
            ctrl.claim("operator")
        except RuntimeError:
            pass
        return RedirectResponse("/operator", status_code=303)

    @app.post("/operator/resume")
    async def resume(note: str = Form("")) -> RedirectResponse:
        try:
            # Enter VERIFYING only — do NOT auto-verify. Operator (or engine) must confirm.
            ctrl.resume(note or None)
        except RuntimeError:
            pass
        return RedirectResponse("/operator", status_code=303)

    @app.post("/operator/verify-ok")
    async def verify_ok() -> RedirectResponse:
        try:
            if ctrl.verify_checkpoint is not None:
                ok = ctrl.verify_checkpoint()
                if callable(ok):
                    # support sync callables; async not used from form path
                    ok = ok()
                if not ok:
                    ctrl.verify_fail("checkpoint callback rejected resume")
                    return RedirectResponse("/operator", status_code=303)
            ctrl.verify_ok()
        except RuntimeError:
            pass
        return RedirectResponse("/operator", status_code=303)

    @app.post("/operator/verify-fail")
    async def verify_fail(reason: str = Form("checkpoint mismatch")) -> RedirectResponse:
        try:
            ctrl.verify_fail(reason or "checkpoint mismatch")
        except RuntimeError:
            pass
        return RedirectResponse("/operator", status_code=303)

    @app.post("/operator/abort")
    async def abort() -> RedirectResponse:
        ctrl.abort()
        return RedirectResponse("/operator", status_code=303)

    @app.post("/api/request")
    async def api_request(payload: dict[str, Any]) -> JSONResponse:
        lease = await ctrl.request(
            reason=payload.get("reason", "manual"),
            step_id=payload.get("step_id"),
            capability_id=payload.get("capability_id"),
            goal=payload.get("goal"),
            screenshot_path=payload.get("screenshot_path"),
        )
        return JSONResponse({"lease_id": lease.id, "token": lease.token, "state": ctrl.state.value})

    @app.post("/api/claim")
    async def api_claim(payload: dict[str, Any]) -> JSONResponse:
        snap = ctrl.claim(payload.get("owner", "operator"), token=payload.get("token"))
        return JSONResponse(snap.model_dump())

    @app.post("/api/resume")
    async def api_resume(payload: dict[str, Any]) -> JSONResponse:
        snap = ctrl.resume(payload.get("note"))
        return JSONResponse(snap.model_dump())

    @app.post("/api/verify-ok")
    async def api_verify_ok() -> JSONResponse:
        snap = ctrl.verify_ok()
        return JSONResponse(snap.model_dump())

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/operator", status_code=303)

    return app


# Module app uses the process-shared controller so in-process wiring is correct.
app = create_console_app()
