from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import threading
from pathlib import Path, PureWindowsPath
from typing import Any, Awaitable, Callable

from openai import OpenAI

from core import files
from core.service import get_runtime


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]

VISUAL_ANALYSIS_PROMPT_VERSION = "frontend-style-requirements"
MAX_VISUAL_IMAGE_BYTES = 10 * 1024 * 1024

# Per-image analyses are independent requests, so a small fan-in shortens the
# DESIGN critical path. The cap keeps a large reference set from tripping
# provider rate limits; tune with ARC_VISUAL_ANALYSIS_CONCURRENCY.
DEFAULT_VISUAL_ANALYSIS_CONCURRENCY = 4
MAX_VISUAL_ANALYSIS_CONCURRENCY = 8

# One client per endpoint avoids a fresh TCP+TLS handshake per image, mirroring
# the model adapter's client cache.
_VISUAL_CLIENT_CACHE: dict[tuple[str, str], OpenAI] = {}
_VISUAL_CLIENT_LOCK = threading.Lock()


def _max_visual_analysis_concurrency() -> int:
    raw = os.environ.get("ARC_VISUAL_ANALYSIS_CONCURRENCY", "").strip()
    if not raw:
        return DEFAULT_VISUAL_ANALYSIS_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_VISUAL_ANALYSIS_CONCURRENCY
    return max(1, min(value, MAX_VISUAL_ANALYSIS_CONCURRENCY))


def _get_visual_client() -> OpenAI:
    visual_base_url = _resolve_visual_base_url()
    visual_api_key = _resolve_visual_api_key()
    if not visual_base_url:
        raise RuntimeError("Visual API base URL is not configured.")
    if not visual_api_key:
        raise RuntimeError("Visual API key is not configured.")
    cache_key = (visual_base_url, visual_api_key)
    with _VISUAL_CLIENT_LOCK:
        client = _VISUAL_CLIENT_CACHE.get(cache_key)
        if client is None:
            client = OpenAI(api_key=visual_api_key, base_url=visual_base_url)
            _VISUAL_CLIENT_CACHE[cache_key] = client
        return client


def reset_visual_client_cache_for_tests() -> None:
    """Drop cached visual clients so a test can assert construction behaviour."""

    with _VISUAL_CLIENT_LOCK:
        _VISUAL_CLIENT_CACHE.clear()


def build_visual_analysis_prompt() -> str:
    return """
**ROLE:** You extract frontend style requirements from an input UI image.
**SCENARIO:** Your output will be used as the visual requirement for frontend implementation. Focus on layout, composition, component styling, interaction surfaces, and data presentation patterns. Do NOT treat the image as a source of mock business data.

**PRIMARY GOAL**
Convert the image into implementation-oriented frontend style requirements that define:
- page layout hierarchy
- section composition
- component appearance
- typography and color usage
- spacing and alignment patterns
- how future runtime data should be displayed visually

**CORE DIRECTIVES**
1. **STYLE REQUIREMENTS OVER OCR:** Focus on structure, hierarchy, spacing rhythm, grouping, visual emphasis, and component roles. Do not perform exhaustive OCR transcription.
2. **NO BUSINESS-DATA EXTRACTION:** Do NOT copy screenshot-specific records, names, phone numbers, emails, IDs, dates, prices, counts, table rows, chart values, or other instance data as future mock content.
3. **EXTRACT DATA DISPLAY PATTERNS:** If the image shows lists, tables, cards, charts, schedules, dashboards, or other data regions, describe the container structure, field types, visual encoding, density, and alignment. Do NOT reproduce actual row values.
4. **CAPTURE ONLY STRUCTURAL COPY:** Keep visible text only when it defines page chrome or interaction structure, such as navigation labels, section titles, field labels, button labels, tab names, or status categories.
5. **STRICT STRUCTURAL HIERARCHY:** Describe the page as a tree structure (Parent -> Child -> Sibling).
6. **PRECISE VISUAL SPECS:** Estimate layout mode, proportions, spacing, emphasis, colors, border treatment, shadows, and typography scale. Use approximate values where helpful.
7. **REQUIREMENT LANGUAGE:** Write as frontend style requirements, not as an image caption and not as a pixel-perfect forensic report.

**OUTPUT FORMAT (Strict Markdown)**

### 1. Frontend Style Direction
* **Colors:** Primary, secondary, surfaces, borders, emphasis states (approximate hex allowed).
* **Typography:** Font style, size scale, weight pattern.
* **Spacing Rhythm:** Dense / medium / spacious, notable gaps/padding.
* **Overall Tone:** e.g. enterprise dashboard, lightweight consumer portal, dense admin console, etc.

### 2. Page Skeleton (Top to Bottom)
For each major section:
* **Section Name**
* **Purpose**
* **Container:** width behavior, layout mode, background, border/shadow, spacing.
* **Children:** ordered structural elements and their relationships.
* **Style Notes:** corner radius, dividers, emphasis, icon usage, visual weight.

### 3. Data Presentation Style
For each data-bearing area:
* **Pattern Type:** table / cards / timeline / schedule grid / chart / etc.
* **Visual Structure:** columns, lanes, cards, badges, legends, filters, pagination, empty/loading states.
* **Field Types:** what kinds of values appear there in the future.
* **Styling Rules:** alignment, emphasis, truncation, badge colors, density.
* **Do Not Copy Actual Values:** summarize the shape only.

### 4. Interaction Surfaces
* Main forms, filters, selectors, buttons, tabs, pagination, dialogs, and feedback areas.
* Note which controls appear primary vs secondary.
* Describe the expected visual style of controls rather than concrete values shown in the image.

### 5. Frontend Requirements Summary
* **Must Preserve:** structural and style traits that should be kept in implementation.
* **Runtime-Driven Areas:** regions that must stay data-driven instead of hardcoded from the image.
* **Avoid:** screenshot-specific business content being turned into seeded UI data.
"""


async def analyze_and_attach_visual_references(
    *,
    workspace_path: str,
    requirements_dir: str,
    requirement_data: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> dict[str, Any]:
    req_id = str(requirement_data.get("req_id") or requirement_data.get("id") or "").strip()
    if not req_id:
        await _log(log_cb, "System", "Invalid requirement data for visual analysis.", "error", None)
        return requirement_data

    candidates = _collect_visual_candidates(requirement_data)
    if not candidates:
        await _log(log_cb, "System", "No image found in the description or visual_reference.", "info", req_id)
        return requirement_data

    cache = _load_visual_cache(workspace_path)
    # Payloads are written back by candidate index, so the final order matches
    # the sequential implementation regardless of analysis completion order.
    visual_references: list[dict[str, Any] | None] = [None] * len(candidates)
    pending: list[tuple[int, str, Path, str]] = []

    for index, item in enumerate(candidates):
        image_path = str(item.get("image_path") or "").strip()
        if not image_path:
            continue
        existing_analysis = str(item.get("analysis") or "").strip()
        if existing_analysis:
            visual_references[index] = _reference_payload(image_path, existing_analysis, item.get("resolved_image_path"))
            continue

        try:
            full_path = _resolve_image_path(image_path, workspace_path, requirements_dir)
        except ValueError as exc:
            await _log(log_cb, "System", f"Rejected image path {image_path}: {exc}", "warning", req_id)
            continue
        if not full_path.exists():
            await _log(log_cb, "System", f"Image not found: {full_path}", "warning", req_id)
            continue

        try:
            cache_key = _build_visual_cache_key(full_path)
        except Exception as exc:
            await _log(log_cb, "System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)
            continue
        cached_entry = cache.get(cache_key)
        if isinstance(cached_entry, dict) and cached_entry.get("analysis"):
            visual_references[index] = _reference_payload(image_path, str(cached_entry["analysis"]), str(full_path))
            await _log(log_cb, "System", f"Reusing cached visual analysis: {image_path}", None, req_id)
            continue
        pending.append((index, image_path, full_path, cache_key))

    cache_updated = await _analyze_pending_images(pending, cache, visual_references, req_id, log_cb)

    if cache_updated:
        _save_visual_cache(workspace_path, cache)

    stored_references = [payload for payload in visual_references if payload is not None]
    if stored_references:
        get_runtime().traceability.update_requirement_fields(req_id, visual_reference=stored_references)
        requirement_data["visual_reference"] = stored_references
        await _log(log_cb, "System", f"Stored {len(stored_references)} visual references for {req_id}", None, req_id)

    return requirement_data


async def _analyze_pending_images(
    pending: list[tuple[int, str, Path, str]],
    cache: dict[str, Any],
    visual_references: list[dict[str, Any] | None],
    req_id: str,
    log_cb: LogCallback | None,
) -> bool:
    """Analyze the cache-miss images with bounded concurrency.

    Every analysis is an independent request, so up to
    ``ARC_VISUAL_ANALYSIS_CONCURRENCY`` of them run at once. Results and
    per-image failures land by index, keeping the sequential ordering of
    ``visual_references``; a failing image is logged and skipped exactly as in
    the previous serial loop.
    """
    if not pending:
        return False

    semaphore = asyncio.Semaphore(_max_visual_analysis_concurrency())
    cache_updated = False

    async def analyze_one(index: int, image_path: str, full_path: Path, cache_key: str) -> None:
        nonlocal cache_updated
        try:
            async with semaphore:
                await _log(log_cb, "System", f"Analyzing visual element: {image_path}", None, req_id)
                analysis = await _request_visual_analysis(full_path)
            cache[cache_key] = {
                "image_path": image_path,
                "full_path": str(full_path),
                "prompt_version": VISUAL_ANALYSIS_PROMPT_VERSION,
                "analysis": analysis,
            }
            cache_updated = True
            visual_references[index] = _reference_payload(image_path, analysis, str(full_path))
        except Exception as exc:
            await _log(log_cb, "System", f"Failed to analyze image {image_path}: {exc}", "error", req_id)

    results = await asyncio.gather(*(analyze_one(*entry) for entry in pending), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cache_updated


def _collect_visual_candidates(requirement_data: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    visual_reference = requirement_data.get("visual_reference") or []
    if isinstance(visual_reference, list):
        for item in visual_reference:
            if not isinstance(item, dict):
                continue
            image_path = str(item.get("image_path") or "").strip()
            if image_path and image_path not in seen:
                seen.add(image_path)
                candidates.append(dict(item))

    description = str(requirement_data.get("description") or "")
    for image_path in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", description):
        normalized = image_path.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append({"image_path": normalized})
    return candidates


def _resolve_image_path(image_path: str, workspace_path: str, requirements_dir: str) -> Path:
    raw_path = str(image_path or "").strip().replace("\\", "/")
    windows_path = PureWindowsPath(raw_path)
    if not raw_path or raw_path.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("Image path must stay inside the requirements directory.")

    base_dir = Path(requirements_dir or workspace_path).expanduser().resolve()
    resolved_path = (base_dir / raw_path).resolve()
    try:
        resolved_path.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError("Image path must stay inside the requirements directory.") from exc
    return resolved_path


def _reference_payload(image_path: str, analysis: str, resolved_image_path: Any = None) -> dict[str, Any]:
    payload = {"image_path": image_path, "analysis": analysis}
    if resolved_image_path:
        payload["resolved_image_path"] = str(resolved_image_path)
    return payload


def _visual_cache_path(workspace_path: str) -> Path:
    return Path(workspace_path) / ".arc" / "visual_analysis_cache.json"


def _load_visual_cache(workspace_path: str) -> dict[str, Any]:
    return files.read_json_file(_visual_cache_path(workspace_path)) or {}


def _save_visual_cache(workspace_path: str, cache: dict[str, Any]) -> None:
    files.write_json_file(_visual_cache_path(workspace_path), cache)


def _build_visual_cache_key(full_path: Path) -> str:
    stat = full_path.stat()
    model_name = _normalize_openai_model_name(os.environ.get("VISUAL_MODEL") or os.environ.get("MODEL", ""))
    raw_key = (
        f"{full_path}::{int(stat.st_mtime_ns)}::{stat.st_size}::{VISUAL_ANALYSIS_PROMPT_VERSION}"
        f"::{_resolve_visual_base_url()}::{model_name}"
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def _request_visual_analysis(full_path: Path) -> str:
    try:
        image_size = full_path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Unable to inspect visual image: {full_path}") from exc
    if image_size > MAX_VISUAL_IMAGE_BYTES:
        raise ValueError(
            f"Visual image is too large ({image_size} bytes; maximum {MAX_VISUAL_IMAGE_BYTES} bytes)."
        )

    client = _get_visual_client()

    mime_type, _ = mimetypes.guess_type(str(full_path))
    if not mime_type:
        mime_type = "image/png"
    base64_image = base64.b64encode(full_path.read_bytes()).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_image}"
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=_normalize_openai_model_name(os.environ.get("VISUAL_MODEL") or os.environ.get("MODEL", "")),
        messages=[
            {
                "role": "system",
                "content": build_visual_analysis_prompt(),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze this UI image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    return _extract_visual_chat_completion_text(response)


def _resolve_visual_api_key() -> str:
    return os.environ.get("VISUAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")


def _resolve_visual_base_url() -> str:
    return (
        os.environ.get("VISUAL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE", "")
    ).strip()


def _extract_visual_chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise RuntimeError(f"Visual API response did not contain chat completion text: {_short_response(response)}")


def _normalize_openai_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if normalized.startswith("openai:"):
        return normalized.split(":", 1)[1].strip()
    return normalized


def _short_response(response: Any, limit: int = 800) -> str:
    text = str(response)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


async def _log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if hasattr(result, "__await__"):
        await result
