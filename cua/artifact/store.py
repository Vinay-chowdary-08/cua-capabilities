"""Load/save YAML capabilities, JSON Schema export, tenant overlay merge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from cua.artifact.schema import CapabilityArtifact, CapabilityBody, CapabilityStatus

SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


class ArtifactStore:
    def __init__(self, root: str | Path = "capabilities") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "overrides").mkdir(parents=True, exist_ok=True)

    def path_for(self, capability_id: str) -> Path:
        safe = SAFE.sub("-", capability_id).strip("-")
        return self.root / f"{safe}.yaml"

    def list(self) -> list[CapabilityArtifact]:
        arts: list[CapabilityArtifact] = []
        for path in sorted(self.root.glob("*.yaml")):
            arts.append(self.load_path(path))
        return arts

    def load(self, capability_id: str, *, tenant: str | None = None) -> CapabilityArtifact:
        path = self.path_for(capability_id)
        if not path.exists():
            matches = list(self.root.glob(f"*{capability_id}*.yaml"))
            if not matches:
                raise FileNotFoundError(f"Capability not found: {capability_id}")
            path = matches[0]
        art = self.load_path(path)
        if tenant:
            art = self.apply_overlay(art, tenant)
        return art

    def load_path(self, path: Path) -> CapabilityArtifact:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return CapabilityArtifact.model_validate(data)

    def save(self, artifact: CapabilityArtifact) -> Path:
        path = self.path_for(artifact.capability.id)
        payload = json.loads(artifact.model_dump_json())
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
        return path

    def approve(self, capability_id: str) -> CapabilityArtifact:
        art = self.load(capability_id)
        art.capability.status = CapabilityStatus.APPROVED
        self.save(art)
        return art

    def overlay_path(self, tenant: str, capability_id: str) -> Path:
        safe = SAFE.sub("-", capability_id).strip("-")
        return self.root / "overrides" / tenant / f"{safe}.yaml"

    def apply_overlay(self, artifact: CapabilityArtifact, tenant: str) -> CapabilityArtifact:
        path = self.overlay_path(tenant, artifact.capability.id)
        if not path.exists():
            return artifact.model_copy(deep=True)
        with path.open(encoding="utf-8") as f:
            patch = yaml.safe_load(f) or {}
        data = json.loads(artifact.model_dump_json())
        # JSON-merge-patch keyed by step id under steps_by_id
        steps_by_id: dict[str, Any] = patch.get("steps_by_id") or {}
        for step in data["capability"]["steps"]:
            sid = step["id"]
            if sid in steps_by_id:
                step.update(_deep_merge(step, steps_by_id[sid]))
        # optional top-level string replacements for strategies
        replacements: dict[str, str] = patch.get("string_replacements") or {}
        if replacements:
            blob = json.dumps(data)
            for old, new in replacements.items():
                blob = blob.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
            data = json.loads(blob)
        return CapabilityArtifact.model_validate(data)

    def export_json_schema(self, out: str | Path = "schema/capability.schema.json") -> Path:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        schema = CapabilityArtifact.model_json_schema()
        path.write_text(json.dumps(schema, indent=2))
        return path


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def render_template(value: str, inputs: dict[str, Any]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in inputs:
            raise KeyError(key)
        return str(inputs[key])

    return re.sub(r"\{\{inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\}\}", repl, value)
