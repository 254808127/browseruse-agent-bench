#!/usr/bin/env python3
"""Reproduce browser-use cross-origin frame traversal without browser-use.

Purpose:
- Create a real Lexmount cloud-browser session with the Lexmount SDK.
- Navigate through raw CDP and reproduce the expensive part of
  ``BrowserSession.get_all_frames()``: discover page/iframe targets, collect
  every target's frame tree, then resolve each child frame's owner metadata.
- Log target/frame counts, cross-origin counts, CDP failures, domains, and
  elapsed time. ``--official-proxy`` is passed directly to the SDK's
  ``sessions.create(official_proxy=True)`` option for controlled comparisons.

Runtime dependencies:
- Python 3.11+ in this project's ``uv`` environment.
- The ``lexmount``, ``python-dotenv``, and ``websockets`` packages.
- Lexmount credentials in ``.env`` or the process environment:
  ``LEXMOUNT_API_KEY`` and ``LEXMOUNT_PROJECT_ID``. A profile such as ``en``
  reads suffixed variables first, for example ``LEXMOUNT_API_KEY_EN`` and
  ``LEXMOUNT_PROJECT_ID_EN``. ``LEXMOUNT_BASE_URL[_PROFILE]`` is optional.
- Network access to the configured Lexmount API/CDP endpoint and target URL.

Scope:
- This script does not import browser-use, bubus, an agent, or an LLM. It
  reproduces the underlying serial CDP fan-out, not the 30-second watchdog
  event itself.
- It uses real cloud resources and closes the Lexmount session by default;
  ``--keep-open`` deliberately leaves that session running.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from lexmount import Lexmount
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("lexmount_browser_use_iframe_probe")


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


def _configure_logging(verbose: bool, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        handlers=handlers,
    )


class CDPCommandError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error}")


class RawCDPClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._id_lock = asyncio.Lock()

    async def __aenter__(self) -> RawCDPClient:
        self._ws = await connect(self.url, max_size=None)
        self._reader_task = asyncio.create_task(self._read_loop(), name="raw_cdp_read_loop")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._ws is not None:
            await self._ws.close()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw_message in self._ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError as exc:
                    logger.warning("Ignoring non-JSON CDP message: %s", exc)
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, int):
                    continue
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except ConnectionClosed as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(f"CDP connection closed: {exc}"))
            self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("CDP client is not connected")

        async with self._id_lock:
            self._next_id += 1
            message_id = self._next_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message_id] = future
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id is not None:
            payload["sessionId"] = session_id

        try:
            await self._ws.send(json.dumps(payload))
            message = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise

        error = message.get("error")
        if isinstance(error, dict):
            raise CDPCommandError(method, error)
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _short_id(value: Any) -> str:
    return str(value or "")[-8:]


def _endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "<redacted>"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _is_new_tab_page(url: str) -> bool:
    return url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}


def _is_valid_target_browser_use_like(target: dict[str, Any]) -> bool:
    target_type = str(target.get("type") or "")
    url = str(target.get("url") or "")

    url_allowed = False
    if _is_new_tab_page(url):
        url_allowed = True
    if url.startswith("chrome-error://"):
        url_allowed = True
    if url == "about:blank":
        url_allowed = True
    if url.startswith(("http://", "https://")):
        url_allowed = True

    type_allowed = target_type in {"page", "tab", "iframe", "webview"}
    if target_type in {"iframe", "webview"} and not url:
        url_allowed = True

    return url_allowed and type_allowed


def _collect_frame_ids(frame_tree_node: dict[str, Any]) -> list[str]:
    frame = frame_tree_node.get("frame") if isinstance(frame_tree_node, dict) else None
    frame_id = str(frame.get("id") or "") if isinstance(frame, dict) else ""
    frame_ids = [frame_id] if frame_id else []
    child_frames = frame_tree_node.get("childFrames") if isinstance(frame_tree_node, dict) else None
    if isinstance(child_frames, list):
        for child in child_frames:
            if isinstance(child, dict):
                frame_ids.extend(_collect_frame_ids(child))
    return frame_ids


def _process_frame_tree(
    *,
    node: dict[str, Any],
    target: dict[str, Any],
    all_frames: dict[str, dict[str, Any]],
    parent_frame_id: str | None = None,
) -> None:
    frame = node.get("frame") if isinstance(node, dict) else None
    if not isinstance(frame, dict):
        return

    current_frame_id = str(frame.get("id") or "")
    if current_frame_id:
        actual_parent_id = frame.get("parentId") or parent_frame_id
        target_id = str(target.get("targetId") or "")
        frame_info: dict[str, Any] = {
            **frame,
            "frameTargetId": target_id,
            "parentFrameId": actual_parent_id,
            "childFrameIds": [],
            "isCrossOrigin": False,
            "isValidTarget": _is_valid_target_browser_use_like(target),
        }

        cross_origin_type = frame.get("crossOriginIsolatedContextType")
        if cross_origin_type and cross_origin_type != "NotIsolated":
            frame_info["isCrossOrigin"] = True
        if target.get("type") in {"iframe", "webview"}:
            frame_info["isCrossOrigin"] = True

        child_frames = node.get("childFrames") if isinstance(node, dict) else None
        if isinstance(child_frames, list):
            for child in child_frames:
                child_frame = child.get("frame") if isinstance(child, dict) else None
                child_frame_id = (
                    str(child_frame.get("id") or "") if isinstance(child_frame, dict) else ""
                )
                if child_frame_id:
                    frame_info["childFrameIds"].append(child_frame_id)

        if current_frame_id in all_frames:
            existing = all_frames[current_frame_id]
            if target.get("type") in {"iframe", "webview"}:
                existing["frameTargetId"] = target_id
                existing["isCrossOrigin"] = True
        else:
            all_frames[current_frame_id] = frame_info

        if isinstance(child_frames, list):
            for child in child_frames:
                if isinstance(child, dict):
                    _process_frame_tree(
                        node=child,
                        target=target,
                        all_frames=all_frames,
                        parent_frame_id=current_frame_id,
                    )


def _domain_counts(frames: dict[str, dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for frame_info in frames.values():
        url = str(frame_info.get("url") or "")
        hostname = urlparse(url).hostname or url or "<empty>"
        counter[hostname] += 1
    return dict(counter.most_common(12))


async def _attach_target(
    client: RawCDPClient,
    target_id: str,
    *,
    timeout_s: float,
) -> str:
    result = await client.send(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
        timeout_s=timeout_s,
    )
    session_id = str(result.get("sessionId") or "")
    if not session_id:
        raise RuntimeError(
            f"Target.attachToTarget returned no sessionId for target {_short_id(target_id)}"
        )
    return session_id


async def _get_browser_use_like_frames(
    client: RawCDPClient,
    *,
    command_timeout_s: float,
) -> dict[str, Any]:
    start = time.perf_counter()
    targets_result = await client.send("Target.getTargets", timeout_s=command_timeout_s)
    target_infos = targets_result.get("targetInfos") or []
    if not isinstance(target_infos, list):
        raise RuntimeError("Target.getTargets returned no targetInfos list")
    targets = [target for target in target_infos if isinstance(target, dict)]
    selected_targets = [target for target in targets if _is_valid_target_browser_use_like(target)]

    logger.info(
        "Target.getTargets returned total=%s selected=%s selected_types=%s",
        len(targets),
        len(selected_targets),
        dict(Counter(str(target.get("type") or "") for target in selected_targets)),
    )

    all_frames: dict[str, dict[str, Any]] = {}
    target_sessions: dict[str, str] = {}
    frame_tree_failures: Counter[str] = Counter()
    frame_tree_success = 0

    # Match browser-use's first pass: process target sessions one at a time and
    # collect Page.getFrameTree before moving to the next target.
    for index, target in enumerate(selected_targets, start=1):
        target_id = str(target.get("targetId") or "")
        if not target_id:
            continue
        target_type = str(target.get("type") or "")
        target_url = str(target.get("url") or "")
        try:
            session_id = await _attach_target(client, target_id, timeout_s=command_timeout_s)
            target_sessions[target_id] = session_id
            frame_tree = await client.send(
                "Page.getFrameTree", session_id=session_id, timeout_s=command_timeout_s
            )
        except TimeoutError:
            frame_tree_failures["TimeoutError"] += 1
            logger.debug(
                "Frame tree timeout target=%s type=%s url=%s",
                _short_id(target_id),
                target_type,
                target_url[:160],
            )
            continue
        except (CDPCommandError, RuntimeError, OSError, ValueError) as exc:
            frame_tree_failures[type(exc).__name__] += 1
            logger.debug(
                "Frame tree failed target=%s type=%s url=%s error=%s",
                _short_id(target_id),
                target_type,
                target_url[:160],
                exc,
            )
            continue

        root = frame_tree.get("frameTree") if isinstance(frame_tree, dict) else None
        if isinstance(root, dict):
            before = len(all_frames)
            _process_frame_tree(node=root, target=target, all_frames=all_frames)
            frame_tree_success += 1
            logger.debug(
                "Frame tree ok index=%s target=%s type=%s added_frames=%s url=%s",
                index,
                _short_id(target_id),
                target_type,
                len(all_frames) - before,
                target_url[:160],
            )

    metadata_success = 0
    metadata_failures: Counter[str] = Counter()
    metadata_attempts = 0
    # Match BrowserSession._populate_frame_metadata exactly: issue DOM.enable
    # and DOM.getFrameOwner serially for every child frame. Keeping this serial
    # is intentional because the remote CDP round trips are the behavior under
    # investigation.
    for frame_id, frame_info in all_frames.items():
        parent_frame_id = frame_info.get("parentFrameId")
        if not parent_frame_id or parent_frame_id not in all_frames:
            continue
        parent_frame_info = all_frames[parent_frame_id]
        parent_target_id = str(parent_frame_info.get("frameTargetId") or "")
        parent_session_id = target_sessions.get(parent_target_id)
        if not parent_session_id:
            continue

        metadata_attempts += 1
        try:
            await client.send(
                "DOM.enable", session_id=parent_session_id, timeout_s=command_timeout_s
            )
            frame_owner = await client.send(
                "DOM.getFrameOwner",
                {"frameId": frame_id},
                session_id=parent_session_id,
                timeout_s=command_timeout_s,
            )
        except TimeoutError:
            metadata_failures["TimeoutError"] += 1
            continue
        except (CDPCommandError, RuntimeError, OSError, ValueError) as exc:
            metadata_failures[type(exc).__name__] += 1
            logger.debug(
                "Frame metadata failed frame=%s parent_target=%s error=%s",
                _short_id(frame_id),
                _short_id(parent_target_id),
                exc,
            )
            continue

        metadata_success += 1
        if frame_owner:
            frame_info["backendNodeId"] = frame_owner.get("backendNodeId")
            frame_info["nodeId"] = frame_owner.get("nodeId")

    elapsed = time.perf_counter() - start
    cross_origin_count = sum(1 for frame in all_frames.values() if frame.get("isCrossOrigin"))
    logger.info(
        "browser-use-like get_all_frames completed in %.2fs targets=%s frame_tree_success=%s frame_tree_failures=%s frames=%s cross_origin=%s metadata_attempts=%s metadata_success=%s metadata_failures=%s domains=%s",
        elapsed,
        len(selected_targets),
        frame_tree_success,
        dict(frame_tree_failures),
        len(all_frames),
        cross_origin_count,
        metadata_attempts,
        metadata_success,
        dict(metadata_failures),
        _domain_counts(all_frames),
    )
    return {
        "elapsed_s": elapsed,
        "targets": len(selected_targets),
        "frame_tree_success": frame_tree_success,
        "frame_tree_failures": dict(frame_tree_failures),
        "frames": len(all_frames),
        "cross_origin": cross_origin_count,
        "metadata_attempts": metadata_attempts,
        "metadata_success": metadata_success,
        "metadata_failures": dict(metadata_failures),
        "domains": _domain_counts(all_frames),
    }


async def _get_ax_tree_for_current_frame_tree(
    client: RawCDPClient,
    *,
    session_id: str,
    command_timeout_s: float,
) -> dict[str, Any]:
    frame_tree = await client.send(
        "Page.getFrameTree", session_id=session_id, timeout_s=command_timeout_s
    )
    root = frame_tree.get("frameTree") if isinstance(frame_tree, dict) else None
    if not isinstance(root, dict):
        raise RuntimeError("Page.getFrameTree returned no frameTree")
    frame_ids = _collect_frame_ids(root)
    logger.info("Current target frame tree contains %s frame(s)", len(frame_ids))

    tasks = [
        asyncio.create_task(
            client.send(
                "Accessibility.getFullAXTree",
                {"frameId": frame_id},
                session_id=session_id,
                timeout_s=command_timeout_s,
            )
        )
        for frame_id in frame_ids
    ]
    ax_results = await asyncio.gather(*tasks, return_exceptions=True)
    failed = sum(1 for result in ax_results if isinstance(result, BaseException))
    nodes = 0
    for result in ax_results:
        if isinstance(result, dict):
            nodes += len(result.get("nodes") or [])
    logger.info("Current target all-frame AX completed failed=%s nodes=%s", failed, nodes)
    return {"nodes": nodes, "failed": failed}


async def _run_raw_probe(
    *,
    cdp_url: str,
    url: str,
    post_nav_sleep_s: float,
    command_timeout_s: float,
    traversal_rounds: int,
    traversal_interval_s: float,
) -> None:
    async with RawCDPClient(cdp_url) as client:
        await client.send(
            "Target.setDiscoverTargets", {"discover": True}, timeout_s=command_timeout_s
        )
        await client.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            timeout_s=command_timeout_s,
        )
        targets_result = await client.send("Target.getTargets", timeout_s=command_timeout_s)
        targets = [
            target for target in targets_result.get("targetInfos", []) if isinstance(target, dict)
        ]
        page_targets = [
            target
            for target in targets
            if str(target.get("type") or "") in {"page", "tab"}
            and _is_valid_target_browser_use_like(target)
        ]
        if not page_targets:
            raise RuntimeError("No page/tab target found for navigation")
        page_target = page_targets[0]
        page_target_id = str(page_target.get("targetId") or "")
        page_session_id = await _attach_target(client, page_target_id, timeout_s=command_timeout_s)
        logger.info(
            "Attached page target=%s url=%s session=%s",
            _short_id(page_target_id),
            str(page_target.get("url") or ""),
            _short_id(page_session_id),
        )

        for method in ("Page.enable", "Runtime.enable", "DOM.enable", "Network.enable"):
            start = time.perf_counter()
            await client.send(method, session_id=page_session_id, timeout_s=command_timeout_s)
            logger.info("%s completed in %.2fs", method, time.perf_counter() - start)

        await client.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False},
            session_id=page_session_id,
            timeout_s=command_timeout_s,
        )

        start = time.perf_counter()
        await client.send(
            "Page.navigate",
            {"url": url, "transitionType": "address_bar"},
            session_id=page_session_id,
            timeout_s=command_timeout_s,
        )
        logger.info("Page.navigate completed in %.2fs", time.perf_counter() - start)

        if post_nav_sleep_s > 0:
            logger.info(
                "Sleeping %.2fs after navigation to match browser-use page-state timing",
                post_nav_sleep_s,
            )
            await asyncio.sleep(post_nav_sleep_s)

        ready_state = await client.send(
            "Runtime.evaluate",
            {"expression": "document.readyState", "returnByValue": True},
            session_id=page_session_id,
            timeout_s=command_timeout_s,
        )
        logger.info("document.readyState=%s", ready_state)

        start = time.perf_counter()
        await _get_ax_tree_for_current_frame_tree(
            client,
            session_id=page_session_id,
            command_timeout_s=command_timeout_s,
        )
        logger.info("current-target AX bundle completed in %.2fs", time.perf_counter() - start)

        for round_index in range(1, traversal_rounds + 1):
            logger.info(
                "Starting browser-use-like frame traversal round %s/%s",
                round_index,
                traversal_rounds,
            )
            await _get_browser_use_like_frames(client, command_timeout_s=command_timeout_s)
            if round_index < traversal_rounds and traversal_interval_s > 0:
                await asyncio.sleep(traversal_interval_s)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reproduce browser-use cross-origin iframe traversal using only Lexmount + raw CDP."
    )
    parser.add_argument("--url", default="https://www.ign.com")
    parser.add_argument("--profile", default=_env("BUBENCH_LEXMOUNT_PROFILE"))
    parser.add_argument("--browser-mode", default="normal")
    parser.add_argument("--official-proxy", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--post-nav-sleep-s", type=float, default=8.0)
    parser.add_argument("--command-timeout-s", type=float, default=10.0)
    parser.add_argument("--traversal-rounds", type=int, default=1)
    parser.add_argument("--traversal-interval-s", type=float, default=0.0)
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("output/lexmount_cdp_probe/browser_use_iframe_latest.log"),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    _configure_logging(args.verbose, args.log_file)

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
            _run_raw_probe(
                cdp_url=cdp_url,
                url=args.url,
                post_nav_sleep_s=args.post_nav_sleep_s,
                command_timeout_s=args.command_timeout_s,
                traversal_rounds=max(1, args.traversal_rounds),
                traversal_interval_s=max(0.0, args.traversal_interval_s),
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
