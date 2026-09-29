"""Playwright web surface — frames first-class, set-of-marks, near_label."""

from __future__ import annotations

import io
import re
from typing import Any

from cua.surface.base import (
    Action,
    ActionKind,
    BBox,
    Condition,
    ConditionKind,
    ElementRef,
    Observation,
    ResolvedHandle,
    Target,
)

# Avoid time.sleep — use Playwright waits / asyncio.wait_for patterns.
import asyncio


class WebSurface:
    def __init__(
        self,
        *,
        headless: bool = True,
        base_url: str = "http://127.0.0.1:8800",
        remote_debugging_port: int | None = None,
        enable_trace: bool = False,
        trace_path: str | None = None,
    ) -> None:
        self.headless = headless
        self.base_url = base_url.rstrip("/")
        self.remote_debugging_port = remote_debugging_port
        self.enable_trace = enable_trace
        self.trace_path = trace_path
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._last_elements: list[ElementRef] = []
        self._human_event: asyncio.Event = asyncio.Event()
        self._human_event.set()
        self._mark_handles: dict[int, Any] = {}
        self._tracing = False

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launch_args: list[str] = []
        if self.remote_debugging_port:
            launch_args.append(f"--remote-debugging-port={self.remote_debugging_port}")
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, args=launch_args
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800}
        )
        if self.enable_trace:
            await self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._tracing = True
        self._page = await self._context.new_page()

    async def stop(self) -> None:
        if self._tracing and self._context is not None:
            path = self.trace_path or "trace.zip"
            try:
                await self._context.tracing.stop(path=path)
            except Exception:
                pass
            self._tracing = False
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._page = self._context = self._browser = self._pw = None

    async def save_trace(self, path: str) -> str | None:
        """Stop tracing early and write zip; safe to call once."""
        if not self._tracing or self._context is None:
            return None
        await self._context.tracing.stop(path=path)
        self._tracing = False
        return path

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("WebSurface not started")
        return self._page

    async def goto(self, url: str) -> None:
        full = url if url.startswith("http") else f"{self.base_url}{url}"
        await self.page.goto(full, wait_until="domcontentloaded", timeout=30000)

    async def observe(self, *, screenshot: bool = True) -> Observation:
        page = self.page
        elements: list[ElementRef] = []
        self._mark_handles.clear()
        mark = 1
        text_bits: list[str] = []
        frames: list[str] = []

        scopes: list[tuple[list[str], Any]] = [([], page.main_frame)]
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            path = [fr.name] if fr.name else [f"frame-{len(scopes)}"]
            if fr.name:
                frames.append(fr.name)
            scopes.append((path, fr))

        for frame_path, frame in scopes:
            try:
                raw_items = await asyncio.wait_for(frame.evaluate(_COLLECT_JS), timeout=2.0)
            except Exception:
                continue
            try:
                body_text = await asyncio.wait_for(frame.inner_text("body"), timeout=2.0)
                prefix = f"[{'/'.join(frame_path)}] " if frame_path else ""
                text_bits.append(prefix + (body_text or "")[:3000])
            except Exception:
                pass

            # Compute near_label geometrically within this frame's items
            for item in raw_items:
                bbox = BBox(
                    x=float(item["x"]),
                    y=float(item["y"]),
                    width=float(item["w"]),
                    height=float(item["h"]),
                )
                near = _nearest_label(item, raw_items)
                ref = ElementRef(
                    mark=mark,
                    role=item.get("role") or item.get("tag") or "generic",
                    name=item.get("name"),
                    text=item.get("text"),
                    bbox=bbox,
                    frame_path=list(frame_path),
                    near_label=near,
                    tag=item.get("tag"),
                    name_attr=item.get("name_attr"),
                    value=item.get("value"),
                    meta={"selector_hint": item.get("selector_hint")},
                )
                elements.append(ref)
                # Store a locator for act-by-mark
                sel = item.get("selector_hint")
                if sel:
                    try:
                        loc = frame.locator(sel).first
                        self._mark_handles[mark] = loc
                    except Exception:
                        pass
                mark += 1

        self._last_elements = elements
        if screenshot:
            png = await page.screenshot(full_page=False, type="png")
            annotated = _draw_marks(png, elements)
        else:
            annotated = b""

        return Observation(
            url=page.url,
            title=await page.title(),
            screenshot_png=annotated,
            elements=elements,
            visible_text_excerpt="\n".join(text_bits)[:8000],
            frames=frames,
        )

    async def act(self, action: Action) -> None:
        await self._human_event.wait()
        kind = action.kind
        if kind == ActionKind.NAVIGATE:
            await self.goto(action.url or "/")
            return
        if kind == ActionKind.PRESS:
            await self.page.keyboard.press(action.key or "Enter")
            await self._settle()
            return
        if kind == ActionKind.WAIT:
            # Condition wait: DOM quiescent, not a blind sleep.
            await self._settle(timeout_ms=int(action.meta.get("ms", 500)))
            return

        handle = await self.resolve(action.target or Target(), timeout_ms=8000)
        loc = handle.meta.get("locator")
        if loc is None and handle.mark is not None:
            loc = self._mark_handles.get(handle.mark)
        if loc is None:
            raise RuntimeError(f"No locator for action {kind}")

        if kind == ActionKind.CLICK:
            await loc.click(timeout=8000)
        elif kind == ActionKind.TYPE:
            await loc.fill(action.value or "", timeout=8000)
        elif kind == ActionKind.SELECT:
            await loc.select_option(action.option or action.value or "", timeout=8000)
        elif kind == ActionKind.EXTRACT:
            return
        else:
            raise ValueError(f"Unsupported action: {kind}")
        await self._settle()

    async def resolve(self, target: Target, timeout_ms: int) -> ResolvedHandle:
        if target.mark is not None:
            el = next((e for e in self._last_elements if e.mark == target.mark), None)
            loc = self._mark_handles.get(target.mark)
            if loc is None:
                raise RuntimeError(f"Unknown mark {target.mark}")
            return ResolvedHandle(
                mark=target.mark,
                strategy_used="mark",
                frame=el.frame_path[0] if el and el.frame_path else None,
                role=el.role if el else None,
                name=el.name if el else None,
                text=el.text if el else None,
                near_label=el.near_label if el else None,
                bbox=el.bbox if el else None,
                meta={"locator": loc, "element": el},
            )

        # Strategy-based resolve (exact-one rule)
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        last_err = "no strategies"
        strategies = target.strategies or _target_to_strategies(target)
        while asyncio.get_event_loop().time() < deadline:
            for strat in strategies:
                try:
                    locs = await self._query_strategy(strat, target, timeout_ms=timeout_ms)
                    if len(locs) == 0:
                        last_err = f"{strat.get('by')}: none"
                        continue
                    if len(locs) > 1:
                        raise RuntimeError(
                            f"TARGET_AMBIGUOUS strategy={strat.get('by')} count={len(locs)}"
                        )
                    loc = locs[0]
                    await loc.wait_for(state="visible", timeout=min(1500, timeout_ms))
                    return ResolvedHandle(
                        strategy_used=str(strat.get("by")),
                        frame=target.frame
                        or (target.frame_path[0] if target.frame_path else None),
                        meta={"locator": loc, "strategy": strat},
                    )
                except RuntimeError:
                    raise
                except Exception as e:
                    last_err = str(e)
                    continue
            await self._poll_tick()
        raise RuntimeError(f"TARGET_NOT_FOUND: {last_err}")

    async def read(self, target: Target) -> str:
        handle = await self.resolve(target, timeout_ms=5000)
        loc = handle.meta.get("locator")
        if loc is None:
            return ""
        try:
            return (await loc.inner_text()).strip()
        except Exception:
            try:
                return str(await loc.input_value())
            except Exception:
                return ""

    async def wait_for(self, cond: Condition, timeout_ms: int) -> bool:
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            obs = await self.observe()
            if cond.kind == ConditionKind.TEXT_VISIBLE and cond.text:
                if cond.text.lower() in obs.visible_text_excerpt.lower():
                    return True
            elif cond.kind == ConditionKind.URL_MATCHES and cond.pattern:
                if obs.url and re.search(cond.pattern, obs.url):
                    return True
            elif cond.kind == ConditionKind.ELEMENT_VISIBLE and cond.target:
                try:
                    await self.resolve(cond.target, timeout_ms=500)
                    return True
                except Exception:
                    pass
            await self._poll_tick()
        return False

    async def _settle(self, timeout_ms: int = 1500) -> None:
        """Wait for DOM to settle after an action — load state, not sleep."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=min(800, timeout_ms))
        except Exception:
            pass

    async def _poll_tick(self) -> None:
        """One poll interval tied to a frame/microtask, not time.sleep."""
        try:
            await self.page.wait_for_timeout(0)  # flush microtasks
            await self.page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
        except Exception:
            await asyncio.sleep(0)

    async def pause_for_human(self) -> None:
        """Hands the live session over — automation stops until resume()."""
        self._human_event.clear()
        await self._human_event.wait()

    def resume_from_human(self) -> None:
        self._human_event.set()

    async def _query_strategy(
        self, strat: dict[str, Any], target: Target, *, timeout_ms: int = 3000
    ) -> list[Any]:
        scope = self.page
        frame_name = None
        if isinstance(strat.get("frame"), dict):
            frame_name = strat["frame"].get("name_hint")
        frame_name = frame_name or target.frame
        if not frame_name and target.frame_path:
            frame_name = target.frame_path[0]
        if frame_name:
            fr = None
            deadline_frame = asyncio.get_event_loop().time() + min(3.0, timeout_ms / 1000)
            while asyncio.get_event_loop().time() < deadline_frame:
                fr = self.page.frame(name=frame_name)
                if fr is not None:
                    break
                await self._poll_tick()
            if fr is None:
                return []
            scope = fr

        by = strat.get("by") or strat.get("strategy")
        if by == "role":
            role = strat.get("role") or "button"
            name = strat.get("name") or strat.get("text") or ""
            loc = scope.get_by_role(role, name=name, exact=bool(strat.get("exact", False)))
            return await _all_visible(loc)
        if by == "text":
            loc = scope.get_by_text(strat.get("text") or "", exact=bool(strat.get("exact", False)))
            return await _all_visible(loc)
        if by == "css":
            loc = scope.locator(strat.get("value") or strat.get("css") or "")
            return await _all_visible(loc)
        if by == "name_attr":
            loc = scope.locator(f"[name='{strat.get('name')}']")
            return await _all_visible(loc)
        if by == "label_proximity":
            label = strat.get("label") or ""
            loc = scope.locator(
                f"xpath=//td[contains(normalize-space(.), '{label}')]/following-sibling::td[1]//input | "
                f"//td[contains(normalize-space(.), '{label}')]/following-sibling::td[1]//select"
            )
            return await _all_visible(loc)
        if by == "table_cell":
            row = strat.get("row_header") or ""
            col = strat.get("column_header") or "Balance"
            # Only trust column-index xpath when the header cell actually exists
            header = scope.locator(
                f"xpath=//tr[td[normalize-space()='{col}'] or th[normalize-space()='{col}']]"
                f"/*[normalize-space()='{col}']"
            )
            try:
                header_ok = await header.count() > 0
            except Exception:
                header_ok = False
            if header_ok:
                loc = scope.locator(
                    f"xpath=//tr[td[normalize-space()='{row}'] or th[normalize-space()='{row}']]"
                    f"/td[count(//tr[td[normalize-space()='{col}'] or th[normalize-space()='{col}']]"
                    f"/*[normalize-space()='{col}']/preceding-sibling::*)+1]"
                )
                primary = await _all_visible(loc)
                # Reject the row-header cell itself (common false positive)
                filtered = []
                for h in primary:
                    try:
                        t = (await h.inner_text()).strip()
                    except Exception:
                        t = ""
                    if t and t != row:
                        filtered.append(h)
                if filtered:
                    return filtered
            simple = scope.locator(
                f"xpath=//tr[td[contains(normalize-space(.), '{row}')]]/td[last()]"
            )
            return await _all_visible(simple)
        if by == "heuristic":
            text = strat.get("text") or strat.get("value") or strat.get("name") or ""
            loc = scope.locator(
                f"input[type='submit'][value='{text}'], button:has-text('{text}'), a:has-text('{text}')"
            )
            return await _all_visible(loc)
        return []


async def _all_visible(loc: Any) -> list[Any]:
    try:
        count = await loc.count()
    except Exception:
        return []
    out = []
    for i in range(count):
        nth = loc.nth(i)
        try:
            if await nth.is_visible():
                out.append(nth)
        except Exception:
            continue
    return out


def _target_to_strategies(target: Target) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if target.role and (target.name or target.text):
        out.append({"by": "role", "role": target.role, "name": target.name or target.text})
    if target.near_label:
        out.append({"by": "label_proximity", "label": target.near_label})
    if target.text:
        out.append({"by": "text", "text": target.text})
    if target.css:
        out.append({"by": "css", "value": target.css})
    return out


def _nearest_label(item: dict[str, Any], all_items: list[dict[str, Any]]) -> str | None:
    ix, iy = float(item["x"]), float(item["y"])
    best: tuple[float, str] | None = None
    for other in all_items:
        if other is item:
            continue
        text = (other.get("text") or other.get("name") or "").strip()
        if not text or len(text) > 40:
            continue
        # Prefer left or above
        ox, oy = float(other["x"]), float(other["y"])
        ow, oh = float(other["w"]), float(other["h"])
        left = ox + ow <= ix + 5 and abs((oy + oh / 2) - (iy + float(item["h"]) / 2)) < 30
        above = oy + oh <= iy + 5 and abs((ox + ow / 2) - (ix + float(item["w"]) / 2)) < 80
        if not (left or above):
            continue
        dist = abs(ix - (ox + ow)) + abs(iy - (oy + oh))
        if best is None or dist < best[0]:
            best = (dist, text)
    return best[1] if best else None


def _draw_marks(png: bytes, elements: list[ElementRef]) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return png
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    for el in elements:
        x, y, w, h = el.bbox.x, el.bbox.y, el.bbox.width, el.bbox.height
        draw.rectangle([x, y, x + w, y + h], outline=(220, 60, 20, 255), width=2)
        label = str(el.mark)
        draw.rectangle([x, max(0, y - 14), x + 8 * len(label) + 4, y], fill=(220, 60, 20, 220))
        draw.text((x + 2, max(0, y - 13)), label, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


_COLLECT_JS = """() => {
  const out = [];
  const nodes = document.querySelectorAll('a, button, input, select, textarea, [role]');
  let i = 0;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('value') || '').trim().slice(0, 80);
    const role = el.getAttribute('role') ||
      (tag === 'a' ? 'link' :
       (tag === 'button' || (tag === 'input' && (el.type === 'submit' || el.type === 'button')) ? 'button' :
        (tag === 'input' || tag === 'textarea' ? 'textbox' :
         (tag === 'select' ? 'combobox' : tag))));
    let selector_hint = null;
    if (el.getAttribute('name')) selector_hint = tag + "[name='" + el.getAttribute('name') + "']";
    else if (el.getAttribute('value') && (el.type === 'submit' || tag === 'button'))
      selector_hint = tag + "[value='" + el.getAttribute('value') + "']";
    else if (tag === 'a' && text) selector_hint = "a:has-text('" + text.replace(/'/g, "\\\\'") + "')";
    out.push({
      tag, role,
      name: el.getAttribute('aria-label') || text || null,
      text: text || null,
      name_attr: el.getAttribute('name') || null,
      value: el.getAttribute('value') || null,
      x: r.x, y: r.y, w: r.width, h: r.height,
      selector_hint,
    });
    if (++i >= 60) break;
  }
  return out;
}"""
