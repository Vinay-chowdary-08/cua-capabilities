"""Redaction: params → {{inputs.x}}, PII/financial masking, screenshot boxes."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from cua.surface.base import BBox, ElementRef, Observation


class Redactor(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    patterns: list[tuple[str, re.Pattern[str], str]] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "policy/default.yaml", params: dict[str, str] | None = None) -> "Redactor":
        raw: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            with p.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        patterns = []
        for item in (raw.get("redact") or {}).get("patterns") or []:
            patterns.append((item["name"], re.compile(item["regex"]), item.get("replacement", "[REDACTED]")))
        # built-in backstops
        builtins = [
            ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
            ("long_digits", re.compile(r"\b\d{10,17}\b"), "[ACCOUNT]"),
            ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
            ("phone", re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "[PHONE]"),
        ]
        names = {n for n, _, _ in patterns}
        for b in builtins:
            if b[0] not in names:
                patterns.append(b)
        return cls(patterns=patterns, params=dict(params or {}))

    def redact_text(self, text: str) -> str:
        out = text
        for key, val in self.params.items():
            if val and val in out:
                out = out.replace(val, "{{inputs." + key + "}}")
        for _name, pattern, replacement in self.patterns:
            out = pattern.sub(replacement, out)
        return out

    def mask_output(self, value: Any, *, sensitivity: str) -> Any:
        if sensitivity in {"pii", "secret"}:
            return "[REDACTED]"
        if sensitivity == "financial":
            return "$***.**"
        return value

    def redact_observation_text(self, obs: Observation) -> str:
        return self.redact_text(obs.visible_text_excerpt)

    def redact_screenshot(
        self,
        png: bytes,
        elements: list[ElementRef],
        *,
        sensitive_values: list[str] | None = None,
    ) -> bytes:
        """Black-box elements whose values match sensitive fields before saving."""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return png
        sensitive = list(sensitive_values or [])
        sensitive.extend(self.params.values())
        img = Image.open(io.BytesIO(png)).convert("RGBA")
        draw = ImageDraw.Draw(img)
        for el in elements:
            blob = " ".join(filter(None, [el.text, el.value, el.name]))
            if any(s and s in blob for s in sensitive) or re.search(r"\d{3}-\d{2}-\d{4}", blob or ""):
                b = el.bbox
                draw.rectangle(
                    [b.x, b.y, b.x + b.width, b.y + b.height],
                    fill=(0, 0, 0, 230),
                )
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
