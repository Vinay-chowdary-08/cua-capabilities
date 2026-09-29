"""Hostile legacy credit-union back-office — framesets, tables, faults."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))
VARIANTS_DIR = APP_DIR / "variants"

SESSIONS: dict[str, dict[str, Any]] = {}
FAULTS: dict[str, Any] = {
    "slow": False,
    "slow_min_ms": 3000,
    "slow_max_ms": 8000,
    "error500": False,
    "interstitial": False,
    "session_expire": False,
    "permission_denied_member": None,  # member number string
    "confirm_dialog": False,
}


def load_variant(tenant: str) -> dict[str, Any]:
    path = VARIANTS_DIR / f"{tenant}.yaml"
    if not path.exists():
        path = VARIANTS_DIR / "tenant_a.yaml"
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def get_tenant(request: Request) -> str:
    return (
        request.query_params.get("tenant")
        or request.cookies.get("tenant")
        or os.environ.get("CUA_TARGET_TENANT", "tenant_a")
    )


def credentials() -> tuple[str, str]:
    user = os.environ.get("CUA_TARGET_USER", "teller")
    password = os.environ.get("CUA_TARGET_PASSWORD", "teller")
    return user, password


def idle_timeout_sec() -> int:
    return int(os.environ.get("CUA_SESSION_IDLE_SEC", "300"))


def _blank_session() -> dict[str, Any]:
    return {
        "authenticated": False,
        "user": None,
        "last_active": time.time(),
        "last_search": None,
        "last_member": None,
        "subaccount": None,
        "flash": None,
        "flash_error": None,
        "interstitial_pending": False,
    }


def session_id(request: Request) -> str:
    return getattr(request.state, "sid", None) or request.cookies.get("sid") or "anon"


def session_for(request: Request) -> dict[str, Any]:
    sid = session_id(request)
    if sid not in SESSIONS:
        SESSIONS[sid] = _blank_session()
    sess = SESSIONS[sid]
    # Idle timeout
    if sess.get("authenticated") and (time.time() - float(sess.get("last_active", 0))) > idle_timeout_sec():
        sess.clear()
        sess.update(_blank_session())
        sess["flash_error"] = "Session expired due to inactivity"
    elif FAULTS.get("session_expire") and sess.get("authenticated"):
        sess.clear()
        sess.update(_blank_session())
        sess["flash_error"] = "Session expired"
    else:
        sess["last_active"] = time.time()
    return sess


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, ctx)


class SessionCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        sid = request.cookies.get("sid") or uuid4().hex[:16]
        request.state.sid = sid
        # Fault: slow
        if FAULTS.get("slow") and not request.url.path.startswith("/__"):
            delay = random.uniform(
                FAULTS.get("slow_min_ms", 3000) / 1000,
                FAULTS.get("slow_max_ms", 8000) / 1000,
            )
            time.sleep(delay)  # intentional app fault injection, not agent wait
        # Fault: error500
        if FAULTS.get("error500") and request.url.path.startswith("/member"):
            return HTMLResponse("<html><body><h1>500 Internal Server Error</h1><p>Core fault injected</p></body></html>", status_code=500)
        response = await call_next(request)
        response.set_cookie("sid", sid)
        tenant = get_tenant(request)
        response.set_cookie("tenant", tenant)
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Legacy CU Core", docs_url=None, redoc_url=None)
    app.add_middleware(SessionCookieMiddleware)

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        sess = session_for(request)
        if not sess["authenticated"]:
            return render(
                request,
                "login.html",
                v=variant,
                error=sess.pop("flash_error", None) or sess.pop("flash", None),
            )
        return render(request, "shell.html", v=variant, user=sess["user"], page="home")

    @app.post("/login")
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> RedirectResponse:
        tenant = get_tenant(request)
        sess = session_for(request)
        sid = session_id(request)
        user, pw = credentials()
        # Also accept variant credentials for convenience
        variant = load_variant(tenant)
        vcreds = variant.get("credentials") or {}
        ok = (username == user and password == pw) or (
            username == vcreds.get("username") and password == vcreds.get("password")
        )
        if ok:
            sess["authenticated"] = True
            sess["user"] = username
            sess["flash_error"] = None
            if FAULTS.get("interstitial"):
                sess["interstitial_pending"] = True
            resp = RedirectResponse("/home", status_code=303)
        else:
            sess["flash_error"] = variant.get("strings", {}).get("login_error", "Logon rejected")
            resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("sid", sid)
        resp.set_cookie("tenant", tenant)
        return resp

    @app.get("/logout")
    async def logout(request: Request) -> RedirectResponse:
        sess = session_for(request)
        sess.clear()
        sess.update(_blank_session())
        return RedirectResponse("/", status_code=303)

    @app.get("/home", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return await _shell(request, "home")

    @app.get("/frames/nav", response_class=HTMLResponse)
    async def frame_nav(request: Request) -> HTMLResponse:
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        sess = session_for(request)
        if not sess["authenticated"]:
            return HTMLResponse("<html><body>Session expired</body></html>")
        nav = variant.get("nav") or [
            {"page": "home", "label_key": "menu_home"},
            {"page": "search", "label_key": "menu_search"},
            {"page": "subaccount", "label_key": "menu_subaccount"},
        ]
        return render(request, "frame_nav.html", v=variant, user=sess["user"], nav=nav)

    @app.get("/frames/main", response_class=HTMLResponse)
    async def frame_main(
        request: Request,
        page: str = "home",
        m: str = "",
    ) -> HTMLResponse:
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        sess = session_for(request)
        if not sess["authenticated"]:
            return HTMLResponse(
                "<html><body>Session expired — <a href='/' target='_top'>re-login</a></body></html>"
            )
        if FAULTS.get("interstitial") and sess.get("authenticated"):
            sess["interstitial_pending"] = True
            FAULTS["interstitial"] = False  # one-shot injection into the session
        if sess.get("interstitial_pending"):
            return render(request, "interstitial.html", v=variant, next=f"/frames/main?page={page}")

        member = sess.get("last_member")
        if page == "member" and m:
            member = _find_member(variant, m)
            sess["last_member"] = member
            denied = FAULTS.get("permission_denied_member")
            if denied and str(m) == str(denied):
                return render(
                    request,
                    "frame_main.html",
                    v=variant,
                    user=sess["user"],
                    page="permission_denied",
                    member=None,
                    flash=None,
                    flash_error="You are not authorized to view this member",
                    results=None,
                    subaccount=None,
                    confirm_dialog=False,
                )

        return render(
            request,
            "frame_main.html",
            v=variant,
            user=sess["user"],
            page=page,
            flash=sess.pop("flash", None),
            flash_error=sess.pop("flash_error", None),
            results=sess.get("last_search"),
            member=member,
            subaccount=sess.get("subaccount"),
            confirm_dialog=bool(FAULTS.get("confirm_dialog")),
            extra_field_label=(variant.get("strings") or {}).get("extra_field"),
        )

    @app.post("/interstitial/ack")
    async def interstitial_ack(request: Request, next: str = Form("/frames/main?page=home")) -> RedirectResponse:
        sess = session_for(request)
        sess["interstitial_pending"] = False
        # one-shot: clear global fault after ack if it was sticky per-session
        return RedirectResponse(next, status_code=303)

    @app.post("/search")
    async def search(
        request: Request,
        qry: str = Form(""),
        kind: str = Form("member_no"),
    ) -> RedirectResponse:
        sess = session_for(request)
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        members = variant.get("members") or []
        q = qry.strip()
        hits: list[dict[str, Any]] = []
        for mem in members:
            if kind in {"member_no", "member_number"} and q == str(mem["member_no"]):
                hits.append(mem)
            elif kind == "name" and q.lower() in str(mem["name"]).lower():
                hits.append(mem)
        sess["last_search"] = hits
        if not hits:
            sess["flash_error"] = variant.get("strings", {}).get(
                "no_results", "No member found matching criteria"
            )
        return RedirectResponse("/frames/main?page=search", status_code=303)

    @app.get("/member/{member_no}", response_class=HTMLResponse)
    async def member_detail(request: Request, member_no: str) -> RedirectResponse:
        sess = session_for(request)
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        member = _find_member(variant, member_no)
        sess["last_member"] = member
        if member is None:
            sess["flash_error"] = "No member found matching criteria"
            return RedirectResponse("/frames/main?page=search", status_code=303)
        return RedirectResponse(f"/frames/main?page=member&m={member_no}", status_code=303)

    # --- Open sub-account flow ---
    @app.post("/subaccount/start")
    async def subaccount_start(
        request: Request,
        member_no: str = Form(...),
    ) -> RedirectResponse:
        sess = session_for(request)
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        member = _find_member(variant, member_no)
        if not member:
            sess["flash_error"] = "No member found matching criteria"
            return RedirectResponse("/frames/main?page=subaccount", status_code=303)
        sess["last_member"] = member
        sess["subaccount"] = {"member_no": member_no, "stage": "form"}
        return RedirectResponse("/frames/main?page=subaccount_form", status_code=303)

    @app.post("/subaccount/review")
    async def subaccount_review(
        request: Request,
        account_type: str = Form(...),
        nickname: str = Form(""),
        initial_deposit: str = Form(""),
        branch_code: str = Form(""),
    ) -> RedirectResponse:
        sess = session_for(request)
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        # validation
        try:
            amount = float(initial_deposit.replace(",", "").replace("$", "").strip())
            if amount < 0 or amount > 100000:
                raise ValueError("range")
        except ValueError:
            sess["flash_error"] = variant.get("strings", {}).get(
                "invalid_deposit", "Invalid deposit amount"
            )
            return RedirectResponse("/frames/main?page=subaccount_form", status_code=303)

        sub = sess.get("subaccount") or {}
        sub.update(
            {
                "stage": "review",
                "account_type": account_type,
                "nickname": nickname,
                "initial_deposit": f"{amount:.2f}",
                "branch_code": branch_code,
            }
        )
        sess["subaccount"] = sub
        return RedirectResponse("/frames/main?page=subaccount_review", status_code=303)

    @app.post("/subaccount/confirm")
    async def subaccount_confirm(request: Request) -> RedirectResponse:
        sess = session_for(request)
        sub = sess.get("subaccount")
        if not sub or sub.get("stage") != "review":
            sess["flash_error"] = "Sub-account session expired"
            return RedirectResponse("/frames/main?page=subaccount", status_code=303)
        conf = f"CNF-{uuid4().hex[:8].upper()}"
        sub["stage"] = "done"
        sub["confirmation"] = conf
        sess["subaccount"] = sub
        sess["flash"] = f"Sub-account opened. Confirmation {conf}"
        return RedirectResponse("/frames/main?page=subaccount_done", status_code=303)

    # --- Fault admin ---
    @app.get("/__faults")
    @app.post("/__faults")
    async def faults_admin(
        request: Request,
        slow: bool | None = Query(None),
        error500: bool | None = Query(None),
        interstitial: bool | None = Query(None),
        session_expire: bool | None = Query(None),
        confirm_dialog: bool | None = Query(None),
        permission_denied_member: str | None = Query(None),
        clear: bool | None = Query(None),
    ) -> JSONResponse:
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
            for k, v in body.items():
                if k in FAULTS or k == "permission_denied_member":
                    FAULTS[k] = v
        if clear:
            FAULTS.update(
                {
                    "slow": False,
                    "error500": False,
                    "interstitial": False,
                    "session_expire": False,
                    "permission_denied_member": None,
                    "confirm_dialog": False,
                }
            )
        if slow is not None:
            FAULTS["slow"] = slow
        if error500 is not None:
            FAULTS["error500"] = error500
        if interstitial is not None:
            FAULTS["interstitial"] = interstitial
        if session_expire is not None:
            FAULTS["session_expire"] = session_expire
        if confirm_dialog is not None:
            FAULTS["confirm_dialog"] = confirm_dialog
        if permission_denied_member is not None:
            FAULTS["permission_denied_member"] = permission_denied_member or None
        return JSONResponse({"faults": FAULTS})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _shell(request: Request, page: str) -> HTMLResponse:
        tenant = get_tenant(request)
        variant = load_variant(tenant)
        sess = session_for(request)
        if not sess["authenticated"]:
            return RedirectResponse("/", status_code=303)
        return render(request, "shell.html", v=variant, user=sess["user"], page=page)

    return app


def _find_member(variant: dict[str, Any], member_no: str) -> dict[str, Any] | None:
    for mem in variant.get("members") or []:
        if str(mem["member_no"]) == str(member_no):
            return mem
    return None


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("CUA_TARGET_HOST", "127.0.0.1")
    port = int(os.environ.get("CUA_TARGET_PORT", "8800"))
    uvicorn.run("target_app.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
