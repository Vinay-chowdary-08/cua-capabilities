"""Surface protocol — Perception / Action / Resolve. Surface-specific code only here."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class ElementRef(BaseModel):
    """What perception sees (set-of-marks)."""

    mark: int
    role: str
    name: str | None = None
    text: str | None = None
    bbox: BBox
    frame_path: list[str] = Field(default_factory=list)
    near_label: str | None = None
    tag: str | None = None
    name_attr: str | None = None
    value: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    url: str | None = None
    title: str | None = None
    screenshot_png: bytes  # never persist unredacted
    elements: list[ElementRef] = Field(default_factory=list)
    visible_text_excerpt: str = ""
    frames: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"
    WAIT = "wait"


class Target(BaseModel):
    """Locator target — prefer mark during discovery; strategies during replay."""

    mark: int | None = None
    ref: str | None = None
    frame: str | None = None
    frame_path: list[str] = Field(default_factory=list)
    strategies: list[dict[str, Any]] = Field(default_factory=list)
    role: str | None = None
    name: str | None = None
    text: str | None = None
    css: str | None = None
    near_label: str | None = None


class Action(BaseModel):
    kind: ActionKind
    target: Target | None = None
    value: str | None = None
    key: str | None = None
    url: str | None = None
    option: str | None = None
    output_name: str | None = None
    is_param: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ConditionKind(str, Enum):
    ELEMENT_VISIBLE = "element_visible"
    TEXT_VISIBLE = "text_visible"
    URL_MATCHES = "url_matches"
    TIMEOUT = "timeout"


class Condition(BaseModel):
    kind: ConditionKind
    text: str | None = None
    pattern: str | None = None
    target: Target | None = None
    timeout_ms: int = 5000


class ResolvedHandle(BaseModel):
    """Opaque handle returned by resolve — surface-specific payload in meta."""

    mark: int | None = None
    strategy_used: str | None = None
    frame: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    near_label: str | None = None
    bbox: BBox | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Surface(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def observe(self) -> Observation: ...

    async def act(self, action: Action) -> None: ...

    async def resolve(self, target: Target, timeout_ms: int) -> ResolvedHandle: ...

    async def read(self, target: Target) -> str: ...

    async def wait_for(self, cond: Condition, timeout_ms: int) -> bool: ...

    async def pause_for_human(self) -> None: ...

    async def goto(self, url: str) -> None: ...
