"""Allowlist + risk gate. Confirms irreversible rather than blocking blindly."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from cua.surface.base import Action, ActionKind


class PolicyRequire(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"
    REDACT_AND_ALLOW = "redact_and_allow"


class PolicyDecision(BaseModel):
    allowed: bool
    require: PolicyRequire
    risk: str = "low"
    rule_id: str | None = None
    reason: str = ""


class Policy(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "policy/default.yaml") -> "Policy":
        with Path(path).open(encoding="utf-8") as f:
            return cls(raw=yaml.safe_load(f) or {})

    def check_origin(self, url: str) -> PolicyDecision:
        if url.startswith("/"):
            return PolicyDecision(allowed=True, require=PolicyRequire.ALLOW)
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        allowed = set(self.raw.get("allowed_origins") or [])
        if origin not in allowed:
            return PolicyDecision(
                allowed=False,
                require=PolicyRequire.BLOCK,
                risk="critical",
                reason=f"origin not allowlisted: {origin}",
            )
        return PolicyDecision(allowed=True, require=PolicyRequire.ALLOW)

    def check(
        self,
        action: Action,
        *,
        current_url: str = "",
        capability_status: str | None = None,
        confirm_irreversible: bool = False,
        mode: str = "discovery",
    ) -> PolicyDecision:
        if action.kind == ActionKind.NAVIGATE and action.url:
            d = self.check_origin(action.url if action.url.startswith("http") else current_url or action.url)
            if not d.allowed:
                return d

        allowed_actions = set(self.raw.get("allowed_actions") or [])
        if action.kind.value not in allowed_actions and action.kind != ActionKind.WAIT:
            return PolicyDecision(
                allowed=False,
                require=PolicyRequire.BLOCK,
                reason=f"action not allowed: {action.kind.value}",
            )

        text_blob = " ".join(
            filter(
                None,
                [
                    action.value,
                    action.option,
                    action.target.text if action.target else None,
                    action.target.name if action.target else None,
                ],
            )
        )

        for rule in self.raw.get("risk_rules") or []:
            match = rule.get("match") or {}
            rx = match.get("button_text_regex")
            if rx and re.search(rx, text_blob or ""):
                return self._irreversible_decision(
                    mode=mode,
                    capability_status=capability_status,
                    confirm_irreversible=confirm_irreversible,
                    rule_id=rule.get("id", "risk_rule"),
                )
            route = match.get("route")
            if route and route in (current_url or ""):
                return self._irreversible_decision(
                    mode=mode,
                    capability_status=capability_status,
                    confirm_irreversible=confirm_irreversible,
                    rule_id=rule.get("id", "route_rule"),
                )

        # credential fields
        hints = " ".join(
            filter(
                None,
                [
                    action.target.name if action.target else None,
                    action.target.css if action.target else None,
                    action.target.near_label if action.target else None,
                ],
            )
        ).lower()
        if action.kind == ActionKind.TYPE and any(h in hints for h in ("password", "ssn", "pin")):
            return PolicyDecision(
                allowed=True,
                require=PolicyRequire.REDACT_AND_ALLOW,
                risk="high",
                reason="credential-like field",
            )

        return PolicyDecision(allowed=True, require=PolicyRequire.ALLOW)

    def _irreversible_decision(
        self,
        *,
        mode: str,
        capability_status: str | None,
        confirm_irreversible: bool,
        rule_id: str,
    ) -> PolicyDecision:
        irrev = self.raw.get("irreversible_policy") or {}
        if mode == "discovery":
            # require_human_confirmation
            return PolicyDecision(
                allowed=True,
                require=PolicyRequire.CONFIRM,
                risk="irreversible",
                rule_id=rule_id,
                reason="irreversible action requires human confirmation in discovery",
            )
        # replay: allow only if approved + confirm flag
        if capability_status == "approved" and confirm_irreversible:
            return PolicyDecision(
                allowed=True,
                require=PolicyRequire.ALLOW,
                risk="irreversible",
                rule_id=rule_id,
                reason="approved + confirm_irreversible",
            )
        return PolicyDecision(
            allowed=False,
            require=PolicyRequire.CONFIRM,
            risk="irreversible",
            rule_id=rule_id,
            reason="replay irreversible requires approved capability and confirm_irreversible",
        )
