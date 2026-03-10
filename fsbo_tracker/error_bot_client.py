"""
ErrorBot SDK client — fire-and-forget error reporting.

Drop this into any FastAPI app. Two lines to integrate:

    from error_bot_client import ErrorBotClient
    error_bot = ErrorBotClient()

Then in your global exception handler:

    error_bot.report(request, exc, request_id)

Features:
- Constructs WorkflowEvent from exception context
- Computes fingerprint locally (same algo as ErrorBot server)
- True fire-and-forget via asyncio.create_task (never adds latency to response)
- Graceful fallback — if ErrorBot is down, logs warning and moves on
- In-memory frequency counter (sliding 5-min window per fingerprint)
"""

import asyncio
import hashlib
import logging
import os
import re
import traceback as tb_module
from collections import defaultdict
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger("errorbot.sdk")

# ---------------------------------------------------------------------------
# Severity classification (deterministic, before AI)
# ---------------------------------------------------------------------------

_SEVERITY_RULES: list[tuple[str, list[str]]] = [
    ("critical", [
        "InFailedSqlTransaction", "OperationalError", "InterfaceError",
        "DatabaseError", "ConnectionRefusedError", "ConnectionResetError",
    ]),
    ("high", [
        "TimeoutError", "ReadTimeout", "ConnectTimeout",
        "HTTPStatusError", "ProxyError",
    ]),
    ("medium", [
        "ValidationError", "ValueError", "KeyError", "TypeError",
        "AttributeError",
    ]),
]


def classify_severity(
    error_class: str,
    error_message: str = "",
) -> str:
    """Deterministic severity from error type. AI refines later on the server."""
    for severity, patterns in _SEVERITY_RULES:
        for pat in patterns:
            if pat in error_class:
                return severity
    # Check message for keywords
    msg_lower = error_message.lower()
    if any(w in msg_lower for w in ("connection refused", "database", "pool")):
        return "critical"
    if any(w in msg_lower for w in ("timeout", "timed out", "429", "403")):
        return "high"
    return "low"


# ---------------------------------------------------------------------------
# Fingerprinting (mirrors server-side compute_fingerprint)
# ---------------------------------------------------------------------------

_RE_HEX = re.compile(r"0x[0-9a-fA-F]+")
_RE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RE_NUMS = re.compile(r"\d+")
_RE_QUOTED = re.compile(r"['\"][^'\"]*['\"]")

# Directories that indicate library code (skip in traceback)
_LIB_DIRS = {"site-packages", "dist-packages", "lib/python", "venv", ".venv"}


def _normalize_message(msg: str) -> str:
    msg = _RE_HEX.sub("<addr>", msg)
    msg = _RE_UUID.sub("<uuid>", msg)
    msg = _RE_NUMS.sub("<n>", msg)
    msg = _RE_QUOTED.sub("<str>", msg)
    return msg.strip()


def _extract_top_frame(traceback_str: str) -> Optional[str]:
    """Extract the top app-code frame from a traceback string."""
    lines = traceback_str.strip().splitlines()
    # Walk backwards to find last 'File "..."' line in app code
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith('File "'):
            continue
        # Skip library frames
        if any(lib in line for lib in _LIB_DIRS):
            continue
        # Extract filename and function
        match = re.match(r'File "(.+?)", line \d+, in (.+)', line)
        if match:
            filepath = match.group(1)
            func = match.group(2)
            filename = filepath.rsplit("/", 1)[-1]
            return f"{filename}:{func}"
    return None


def _strip_module_path(error_class: str) -> str:
    """psycopg2.errors.InFailedSqlTransaction -> InFailedSqlTransaction"""
    return error_class.rsplit(".", 1)[-1] if "." in error_class else error_class


def compute_fingerprint(
    error_class: Optional[str],
    error_message: Optional[str],
    traceback_str: Optional[str],
    step_id: Optional[str] = None,
) -> str:
    """SHA256 fingerprint matching server-side algorithm."""
    parts: list[str] = []

    if error_class:
        parts.append(_strip_module_path(error_class))

    if traceback_str:
        frame = _extract_top_frame(traceback_str)
        if frame:
            parts.append(frame)

    if step_id:
        parts.append(step_id)

    if not parts and error_message:
        parts.append(_normalize_message(error_message))

    if not parts:
        return "unknown"

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Frequency tracker (in-memory, sliding window)
# ---------------------------------------------------------------------------

class _FrequencyTracker:
    """Count events per fingerprint in a sliding window."""

    def __init__(self, window_seconds: int = 300):
        self._window = window_seconds
        self._events: dict[str, list[float]] = defaultdict(list)

    def record(self, fingerprint: str) -> int:
        now = monotonic()
        cutoff = now - self._window
        events = self._events[fingerprint]
        # Prune old entries
        self._events[fingerprint] = [t for t in events if t > cutoff]
        self._events[fingerprint].append(now)
        return len(self._events[fingerprint])


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ErrorBotClient:
    """
    Lightweight ErrorBot SDK client.

    Env vars:
        ERRORBOT_URL           — ErrorBot service base URL (required)
        ERRORBOT_API_KEY       — Secret API key for this app (required)
        ERRORBOT_TENANT_ID     — Tenant ID (required)
        ERRORBOT_APP_ID        — App ID (required)
        ERRORBOT_ENABLED       — "true" to enable (default: "false")
        ERRORBOT_TIMEOUT       — HTTP timeout in seconds (default: 5)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        app_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        timeout: float = 5.0,
    ):
        self.base_url = (base_url or os.getenv("ERRORBOT_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("ERRORBOT_API_KEY", "")
        self.tenant_id = tenant_id or os.getenv("ERRORBOT_TENANT_ID", "")
        self.app_id = app_id or os.getenv("ERRORBOT_APP_ID", "")
        self.timeout = float(os.getenv("ERRORBOT_TIMEOUT", str(timeout)))

        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("ERRORBOT_ENABLED", "false").lower() in ("1", "true", "yes")

        self._frequency = _FrequencyTracker()
        self._http_client = None

    def _get_client(self):
        """Lazy-init httpx.AsyncClient."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=self.timeout)
            except ImportError:
                logger.warning("httpx not installed — ErrorBot SDK disabled")
                self.enabled = False
                return None
        return self._http_client

    def report(
        self,
        request: Any,
        exc: Exception,
        request_id: str = "unknown",
        *,
        flow_id: str = "unhandled_exception",
        step_id: str = "global_handler",
        metadata: Optional[dict] = None,
    ) -> None:
        """
        Report an unhandled exception to ErrorBot.

        True fire-and-forget: spawns a background task via asyncio.create_task.
        Never blocks the response path. Never raises.
        """
        if not self.enabled:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _bg():
            try:
                await self._send_event(request, exc, request_id,
                                       flow_id=flow_id, step_id=step_id,
                                       metadata=metadata)
            except Exception as send_err:
                logger.warning("ErrorBot report failed: %s", send_err)

        loop.create_task(_bg())

    async def _send_event(
        self,
        request: Any,
        exc: Exception,
        request_id: str,
        *,
        flow_id: str,
        step_id: str,
        metadata: Optional[dict],
    ) -> Optional[dict]:
        client = self._get_client()
        if not client:
            return None

        error_class = f"{type(exc).__module__}.{type(exc).__name__}"
        error_message = str(exc)
        traceback_str = "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))
        # Truncate traceback to 4000 chars
        if len(traceback_str) > 4000:
            traceback_str = traceback_str[:4000] + "\n... truncated"

        fingerprint = compute_fingerprint(
            error_class=error_class,
            error_message=error_message,
            traceback_str=traceback_str,
            step_id=step_id,
        )

        frequency = self._frequency.record(fingerprint)
        severity = classify_severity(error_class, error_message)

        # Build request context
        request_path = ""
        request_method = ""
        user_id = None
        try:
            request_path = str(request.url.path) if hasattr(request, "url") else ""
            request_method = request.method if hasattr(request, "method") else ""
            if hasattr(request, "state") and hasattr(request.state, "user_id"):
                user_id = str(request.state.user_id)
        except Exception:
            pass

        workflow_run_id = str(uuid4())

        event = {
            "id": str(uuid4()),
            "tenant_id": self.tenant_id,
            "app_id": self.app_id,
            "environment": os.getenv("ENVIRONMENT", "production"),
            "flow_id": flow_id,
            "flow_version": "1",
            "workflow_run_id": workflow_run_id,
            "step_id": step_id,
            "outcome": "failure",
            "error_class": error_class,
            "error_code": type(exc).__name__,
            "error_message": error_message[:500],
            "traceback": traceback_str,
            "fingerprint": fingerprint,
            "severity": severity,
            "customer_visible": True,
            "retryable": False,
            "safe_to_retry": False,
            "request_id": request_id,
            "metadata": {
                "request_path": request_path,
                "request_method": request_method,
                "frequency_5m": frequency,
                **({"user_id": user_id} if user_id else {}),
                **(metadata or {}),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        url = f"{self.base_url}/api/v1/workflow-events"
        headers = {"x-errorbot-api-key": self.api_key}

        resp = await client.post(url, json=event, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def report_step(
        self,
        *,
        flow_id: str,
        workflow_run_id: str,
        step_id: str,
        outcome: str,
        error_class: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        traceback_str: Optional[str] = None,
        severity: Optional[str] = None,
        request_id: Optional[str] = None,
        side_effects: Optional[list] = None,
        credit_delta: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Report a specific workflow step outcome (not just unhandled exceptions).

        Use this for instrumented steps like token burns, source fetches, etc.
        """
        if not self.enabled:
            return None

        try:
            client = self._get_client()
            if not client:
                return None

            fingerprint = None
            if outcome in ("failure", "timeout") and (error_class or error_message):
                fingerprint = compute_fingerprint(
                    error_class=error_class,
                    error_message=error_message,
                    traceback_str=traceback_str,
                    step_id=step_id,
                )

            event = {
                "id": str(uuid4()),
                "tenant_id": self.tenant_id,
                "app_id": self.app_id,
                "environment": os.getenv("ENVIRONMENT", "production"),
                "flow_id": flow_id,
                "flow_version": "1",
                "workflow_run_id": workflow_run_id,
                "step_id": step_id,
                "outcome": outcome,
                "error_class": error_class,
                "error_code": error_code,
                "error_message": error_message[:500] if error_message else None,
                "traceback": traceback_str,
                "fingerprint": fingerprint,
                "severity": severity,
                "request_id": request_id,
                "side_effects": side_effects or [],
                "credit_delta": credit_delta,
                "metadata": metadata or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            url = f"{self.base_url}/api/v1/workflow-events"
            headers = {"x-errorbot-api-key": self.api_key}

            resp = await client.post(url, json=event, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("ErrorBot step report failed: %s", e)
            return None

    async def close(self):
        """Shutdown the HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
