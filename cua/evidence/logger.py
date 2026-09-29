"""Evidence: JSONL + screenshots + result.json + optional trace."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cua.safety.redact import Redactor


class EvidenceLogger:
    def __init__(
        self,
        root: str | Path = "evidence",
        run_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.root = Path(root)
        self.run_id = run_id or (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        )
        self.run_dir = self.root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "screenshots").mkdir(exist_ok=True)
        self.events_path = self.run_dir / "run.jsonl"
        self.redactor = redactor or Redactor.load()
        self._write_meta({"run_id": self.run_id, "started_at": _utcnow()})

    def log_event(
        self,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        actor: str = "automation",
        step_id: str | None = None,
    ) -> None:
        payload = data or {}
        # Redact string leaves only — never regex the whole JSON blob
        safe = _redact_obj(payload, self.redactor)
        row = {
            "ts": _utcnow(),
            "actor": actor,
            "kind": kind,
            "step_id": step_id,
            "data": safe,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def save_screenshot(self, name: str, png: bytes) -> Path:
        # Caller should pass already-redacted PNG
        path = self.run_dir / "screenshots" / f"{name}.png"
        path.write_bytes(png)
        self.log_event("screenshot", {"path": str(path.relative_to(self.run_dir))})
        return path

    def write_result(self, result: dict[str, Any]) -> Path:
        path = self.run_dir / "result.json"
        path.write_text(json.dumps(result, indent=2, default=str))
        return path

    def attach_trace(self, src: str | Path) -> Path | None:
        """Copy a real Playwright trace.zip into this evidence run."""
        src_path = Path(src)
        if not src_path.exists() or src_path.stat().st_size < 64:
            return None
        dest = self.run_dir / "trace.zip"
        dest.write_bytes(src_path.read_bytes())
        self.log_event("trace", {"path": "trace.zip", "bytes": dest.stat().st_size})
        meta = json.loads((self.run_dir / "meta.json").read_text())
        meta["trace"] = "trace.zip"
        self._write_meta(meta)
        return dest

    def write_operator_log(self, events: list[dict[str, Any]]) -> Path:
        path = self.run_dir / "operator_log.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for ev in events:
                safe = _redact_obj(ev, self.redactor)
                f.write(json.dumps(safe, default=str) + "\n")
        self.log_event("operator_log", {"path": "operator_log.jsonl", "events": len(events)})
        return path

    def _write_meta(self, meta: dict[str, Any]) -> None:
        (self.run_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def _redact_obj(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_obj(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_obj(v, redactor) for v in value]
    return value


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
