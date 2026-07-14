#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from dotenv import load_dotenv
from lexmount import Lexmount
from playwright.async_api import Browser, CDPSession, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

T = TypeVar("T")

logger = logging.getLogger("lexmount_cdp_probe")

BROWSER_USE_REQUIRED_COMPUTED_STYLES = [
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "cursor",
    "pointer-events",
    "position",
    "background-color",
]


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _profile_env(name: str, profile: str | None) -> str | None:
    if profile:
        profiled = _env(f"{name}_{profile.upper()}")
        if profiled:
            return profiled
    return _env(name)


def _short_id(value: Any) -> str:
    return str(value or "")[-8:]


def _endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "<redacted>"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )


async def _timed(
    name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    timeout_s: float,
    required: bool = True,
) -> T | None:
    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(operation(), timeout=timeout_s)
    except TimeoutError:
        elapsed = time.perf_counter() - start
        logger.error("%s timed out after %.2fs (limit %.2fs)", name, elapsed, timeout_s)
        if required:
            raise
        return None
    except (RuntimeError, OSError, ValueError, PlaywrightError) as exc:
        elapsed = time.perf_counter() - start
        logger.error("%s failed after %.2fs: %s: %s", name, elapsed, type(exc).__name__, exc)
        if required:
            raise
        return None
    elapsed = time.perf_counter() - start
    logger.info("%s completed in %.2fs", name, elapsed)
    return result


async def _get_page(browser: Browser) -> Page:
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    if context.pages:
        return context.pages[0]
    return await context.new_page()


async def _wait_for_lifecycle(
    lifecycle_events: list[dict[str, Any]],
    *,
    wait_until: str,
    timeout_s: float,
) -> str:
    acceptable_events = {"networkIdle"}
    if wait_until in ("load", "domcontentloaded"):
        acceptable_events.add("load")
    if wait_until == "domcontentloaded":
        acceptable_events.add("DOMContentLoaded")

    start = time.perf_counter()
    seen: list[str] = []
    while time.perf_counter() - start < timeout_s:
        for event in lifecycle_events:
            event_name = str(event.get("name") or "")
            loader_id = str(event.get("loaderId") or "")
            label = f"{event_name}(loader={loader_id[:8] or 'none'})"
            if label not in seen:
                seen.append(label)
            if event_name in acceptable_events:
                return f"ready via {event_name}; seen={seen[-8:]}"
        await asyncio.sleep(0.05)
    return f"timeout after {timeout_s:.1f}s waiting for {wait_until}; seen={seen[-8:]}"


def _collect_frame_ids(frame_tree_node: dict[str, Any]) -> list[str]:
    frame = frame_tree_node.get("frame") or {}
    frame_id = str(frame.get("id") or "")
    frame_ids = [frame_id] if frame_id else []
    child_frames = frame_tree_node.get("childFrames") or []
    for child_frame in child_frames:
        if isinstance(child_frame, dict):
            frame_ids.extend(_collect_frame_ids(child_frame))
    return frame_ids


async def _get_ax_tree_for_all_frames(cdp: CDPSession) -> dict[str, Any]:
    frame_tree = await cdp.send("Page.getFrameTree")
    root = frame_tree.get("frameTree") if isinstance(frame_tree, dict) else None
    if not isinstance(root, dict):
        raise RuntimeError("Page.getFrameTree returned no frameTree")

    frame_ids = _collect_frame_ids(root)
    logger.info("Frame tree contains %s frame(s)", len(frame_ids))

    requests = [
        cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id}) for frame_id in frame_ids
    ]
    ax_trees = await asyncio.gather(*requests, return_exceptions=True)

    root_result = ax_trees[0] if ax_trees else {"nodes": []}
    if isinstance(root_result, BaseException):
        raise root_result

    merged_nodes = list(root_result.get("nodes") or [])
    failed_child_frames = 0
    for frame_id, ax_tree in zip(frame_ids[1:], ax_trees[1:], strict=False):
        if isinstance(ax_tree, BaseException):
            failed_child_frames += 1
            logger.debug("Skipping child frame AX tree %s: %s", frame_id, ax_tree)
            continue
        merged_nodes.extend(ax_tree.get("nodes") or [])

    logger.info("All-frame AX tree failed child frame count: %s", failed_child_frames)
    return {"nodes": merged_nodes}


async def _run_browser_use_cdp_bundle(cdp: CDPSession) -> dict[str, Any]:
    def create_snapshot_request() -> Awaitable[dict[str, Any]]:
        return cdp.send(
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": BROWSER_USE_REQUIRED_COMPUTED_STYLES,
                "includePaintOrder": True,
                "includeDOMRects": True,
                "includeBlendedBackgroundColors": False,
                "includeTextColorOpacities": False,
            },
        )

    def create_dom_tree_request() -> Awaitable[dict[str, Any]]:
        return cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})

    def create_ax_tree_request() -> Awaitable[dict[str, Any]]:
        return _get_ax_tree_for_all_frames(cdp)

    def create_device_pixel_ratio_request() -> Awaitable[dict[str, Any]]:
        return cdp.send(
            "Runtime.evaluate",
            {"expression": "window.devicePixelRatio", "returnByValue": True},
        )

    factories: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
        "snapshot": create_snapshot_request,
        "dom_tree": create_dom_tree_request,
        "ax_tree": create_ax_tree_request,
        "device_pixel_ratio": create_device_pixel_ratio_request,
    }
    tasks = {
        name: asyncio.create_task(factory(), name=f"probe_{name}")
        for name, factory in factories.items()
    }

    await asyncio.wait(tasks.values(), timeout=10.0)
    pending_names = [name for name, task in tasks.items() if not task.done()]
    if pending_names:
        logger.warning("Browser-use CDP bundle pending after 10s: %s", ", ".join(pending_names))
        for name in pending_names:
            tasks[name].cancel()
        tasks.update(
            {
                name: asyncio.create_task(factories[name](), name=f"probe_{name}_retry")
                for name in pending_names
            }
        )
        await asyncio.wait([tasks[name] for name in pending_names], timeout=2.0)

    failed = [name for name, task in tasks.items() if not task.done() or task.cancelled()]
    if failed:
        for name in failed:
            tasks[name].cancel()
        raise TimeoutError(f"Browser-use CDP bundle failed or timed out: {', '.join(failed)}")

    results = {name: task.result() for name, task in tasks.items()}
    snapshot = results["snapshot"]
    dom_tree = results["dom_tree"]
    ax_tree = results["ax_tree"]
    logger.info(
        "Browser-use CDP bundle counts: documents=%s ax_nodes=%s dom_root_present=%s",
        len(snapshot.get("documents") or []),
        len(ax_tree.get("nodes") or []),
        bool(dom_tree.get("root")),
    )
    return results


async def _run_cdp_probe(
    *,
    cdp_url: str,
    url: str,
    output_dir: Path,
    connect_timeout_s: float,
    action_timeout_s: float,
    readiness_timeout_s: float,
    wait_until: str,
) -> None:
    async with async_playwright() as playwright:
        browser = await _timed(
            "connect_over_cdp",
            lambda: playwright.chromium.connect_over_cdp(
                cdp_url,
                timeout=connect_timeout_s * 1000,
            ),
            timeout_s=connect_timeout_s + 5,
        )
        assert browser is not None
        try:
            page = await _timed(
                "get_or_create_page",
                lambda: _get_page(browser),
                timeout_s=action_timeout_s,
            )
            assert page is not None

            cdp = await _timed(
                "new_cdp_session",
                lambda: page.context.new_cdp_session(page),
                timeout_s=action_timeout_s,
            )
            assert cdp is not None

            await _exercise_cdp(
                cdp=cdp,
                url=url,
                output_dir=output_dir,
                action_timeout_s=action_timeout_s,
                readiness_timeout_s=readiness_timeout_s,
                wait_until=wait_until,
            )
        finally:
            await browser.close()


async def _exercise_cdp(
    *,
    cdp: CDPSession,
    url: str,
    output_dir: Path,
    action_timeout_s: float,
    readiness_timeout_s: float,
    wait_until: str,
) -> None:
    lifecycle_events: list[dict[str, Any]] = []
    cdp.on("Page.lifecycleEvent", lambda params: lifecycle_events.append(dict(params)))

    for method in ("Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"):
        await _timed(method, lambda method=method: cdp.send(method), timeout_s=action_timeout_s)

    await _timed(
        "Page.setLifecycleEventsEnabled",
        lambda: cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True}),
        timeout_s=action_timeout_s,
    )

    viewport_params = {
        "width": 1920,
        "height": 1080,
        "deviceScaleFactor": 1,
        "mobile": False,
    }
    await _timed(
        "Emulation.setDeviceMetricsOverride(before_nav)",
        lambda: cdp.send("Emulation.setDeviceMetricsOverride", viewport_params),
        timeout_s=10,
    )

    nav_result = await _timed(
        "Page.navigate",
        lambda: cdp.send("Page.navigate", {"url": url, "transitionType": "address_bar"}),
        timeout_s=30,
    )
    logger.info("Page.navigate result keys: %s", sorted((nav_result or {}).keys()))

    readiness = await _timed(
        "wait_for_lifecycle",
        lambda: _wait_for_lifecycle(
            lifecycle_events,
            wait_until=wait_until,
            timeout_s=readiness_timeout_s,
        ),
        timeout_s=readiness_timeout_s + 1,
        required=False,
    )
    logger.info("Lifecycle readiness: %s", readiness)

    await _timed(
        "Emulation.setDeviceMetricsOverride(after_nav)",
        lambda: cdp.send("Emulation.setDeviceMetricsOverride", viewport_params),
        timeout_s=10,
        required=False,
    )

    ready_state = await _timed(
        "Runtime.evaluate(document.readyState)",
        lambda: cdp.send(
            "Runtime.evaluate",
            {"expression": "document.readyState", "returnByValue": True},
        ),
        timeout_s=action_timeout_s,
        required=False,
    )
    logger.info("document.readyState: %s", ready_state)

    screenshot = await _timed(
        "Page.captureScreenshot",
        lambda: cdp.send("Page.captureScreenshot", {"format": "png"}),
        timeout_s=15,
        required=False,
    )
    if screenshot and isinstance(screenshot.get("data"), str):
        import base64

        screenshot_path = output_dir / "screenshot.png"
        screenshot_path.write_bytes(base64.b64decode(screenshot["data"]))
        logger.info("Screenshot written to %s", screenshot_path)

    ax_tree = await _timed(
        "Accessibility.getFullAXTree",
        lambda: cdp.send("Accessibility.getFullAXTree"),
        timeout_s=30,
        required=False,
    )
    if isinstance(ax_tree, dict):
        logger.info("AX tree node count: %s", len(ax_tree.get("nodes") or []))

    all_frame_ax_tree = await _timed(
        "Accessibility.getFullAXTree(all_frames)",
        lambda: _get_ax_tree_for_all_frames(cdp),
        timeout_s=30,
        required=False,
    )
    if isinstance(all_frame_ax_tree, dict):
        logger.info("All-frame AX tree node count: %s", len(all_frame_ax_tree.get("nodes") or []))

    dom_snapshot = await _timed(
        "DOMSnapshot.captureSnapshot",
        lambda: cdp.send("DOMSnapshot.captureSnapshot", {"computedStyles": []}),
        timeout_s=30,
        required=False,
    )
    if isinstance(dom_snapshot, dict):
        documents = dom_snapshot.get("documents") or []
        strings = dom_snapshot.get("strings") or []
        logger.info("DOM snapshot documents=%s strings=%s", len(documents), len(strings))

    await _timed(
        "browser-use-style CDP bundle",
        lambda: _run_browser_use_cdp_bundle(cdp),
        timeout_s=30,
        required=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Lexmount CDP latency without bubench or browser-use."
    )
    parser.add_argument("--url", default="https://www.ign.com")
    parser.add_argument("--profile", default=_env("BUBENCH_LEXMOUNT_PROFILE"))
    parser.add_argument("--browser-mode", default="normal")
    parser.add_argument("--official-proxy", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument(
        "--wait-until", choices=("load", "domcontentloaded", "networkidle"), default="load"
    )
    parser.add_argument("--connect-timeout-s", type=float, default=30.0)
    parser.add_argument("--action-timeout-s", type=float, default=30.0)
    parser.add_argument("--readiness-timeout-s", type=float, default=8.0)
    parser.add_argument("--output-dir", type=Path, default=Path("output/lexmount_cdp_probe"))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    _configure_logging(args.verbose)

    profile = str(args.profile or "").strip() or None
    api_key = _profile_env("LEXMOUNT_API_KEY", profile)
    project_id = _profile_env("LEXMOUNT_PROJECT_ID", profile)
    base_url = _profile_env("LEXMOUNT_BASE_URL", profile)
    if not api_key or not project_id:
        logger.error(
            "Missing Lexmount credentials. profile=%s api_key_present=%s project_id_present=%s",
            profile or "",
            bool(api_key),
            bool(project_id),
        )
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Using Lexmount profile=%s base_url=%s", profile or "", base_url or "<sdk default>")

    client = Lexmount(
        api_key=api_key,
        project_id=project_id,
        **({} if not base_url else {"base_url": base_url}),
    )
    session = None
    try:
        start = time.perf_counter()
        session = client.sessions.create(
            browser_mode=args.browser_mode,
            official_proxy=bool(args.official_proxy),
        )
        logger.info("sessions.create completed in %.2fs", time.perf_counter() - start)
        session_id = str(getattr(session, "session_id", None) or getattr(session, "id", "") or "")
        cdp_url = str(getattr(session, "ws", None) or getattr(session, "connect_url", "") or "")
        inspect_url = str(getattr(session, "inspect_url", "") or "")
        logger.info("Lexmount session=%s", _short_id(session_id))
        logger.info("Lexmount cdp_endpoint=%s", _endpoint_label(cdp_url))
        if inspect_url:
            logger.info("Lexmount inspect_endpoint=%s", _endpoint_label(inspect_url))
        if not cdp_url:
            logger.error("Lexmount session returned no CDP URL")
            return 3

        asyncio.run(
            _run_cdp_probe(
                cdp_url=cdp_url,
                url=args.url,
                output_dir=args.output_dir,
                connect_timeout_s=args.connect_timeout_s,
                action_timeout_s=args.action_timeout_s,
                readiness_timeout_s=args.readiness_timeout_s,
                wait_until=args.wait_until,
            )
        )
        return 0
    finally:
        if session is not None and not args.keep_open:
            try:
                session.close()
                logger.info("Lexmount session closed")
            except (OSError, RuntimeError, TimeoutError) as exc:
                logger.warning("Lexmount session.close() failed: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
