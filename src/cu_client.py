"""Azure AI Content Understanding REST client (Managed Identity auth).

Submits a raw document (PDF, image, Office, text) to a CU analyzer and polls
the long-running operation until it succeeds, fails, or times out. The result
JSON is returned as a Python dict and is the same shape that
`ingestion.parse_content_understanding_json` already consumes (format "A"
analyze-result with `result.contents[*].fields`).

Why stdlib `urllib` instead of `requests`/`httpx`?
- Keeps the deployed wheel set small (Functions Y1 deploys are already heavy).
- We only need three HTTP verbs and no streaming.

Auth
----
DefaultAzureCredential (the Function's system-assigned MI in Azure, your
`az login` locally). Scope: ``https://cognitiveservices.azure.com/.default``.
The MI needs the ``Cognitive Services User`` role on the CU resource.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from azure.identity import DefaultAzureCredential

log = logging.getLogger(__name__)

_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
_DEFAULT_API_VERSION = "2024-12-01-preview"
_DEFAULT_POLL_INTERVAL_SEC = 2.0
_DEFAULT_OVERALL_TIMEOUT_SEC = 540.0  # 9 minutes (stay below host functionTimeout)

# Content Understanding accepts these MIME types directly. Anything else we
# fall back to application/octet-stream and let the service infer.
_EXT_CONTENT_TYPE = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe":  "image/jpeg",
    ".bmp":  "image/bmp",
    ".tif":  "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt":  "text/plain",
    ".html": "text/html",
    ".htm":  "text/html",
    ".md":   "text/markdown",
    ".rtf":  "application/rtf",
    ".eml":  "message/rfc822",
    ".msg":  "application/vnd.ms-outlook",
    ".xml":  "application/xml",
}


_credential: DefaultAzureCredential | None = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def _bearer_token() -> str:
    return _get_credential().get_token(_TOKEN_SCOPE).token


def content_type_for(file_name: str) -> str:
    ext = os.path.splitext(file_name)[1].lower()
    ct = _EXT_CONTENT_TYPE.get(ext)
    if ct:
        return ct
    mime, _ = mimetypes.guess_type(file_name)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CuClientError(RuntimeError):
    """Raised for any non-recoverable Content Understanding API failure."""


class CuTimeoutError(CuClientError):
    """Raised when the long-running operation does not reach a terminal state in time."""


@dataclass(frozen=True)
class CuAnalyzeResult:
    """Outcome of a single analyze call."""
    status: str                          # "Succeeded" | "Failed" | "Timeout"
    operation_location: str | None       # URL we polled
    result_json: dict[str, Any] | None   # full JSON body of the final GET (Succeeded only)
    error_message: str | None = None     # non-None when status != "Succeeded"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_analyze(
    *,
    endpoint: str,
    analyzer_id: str,
    api_version: str,
    body: bytes,
    content_type: str,
    token: str,
) -> str:
    """Kick off an analyze operation. Returns the Operation-Location URL."""
    base = endpoint.rstrip("/")
    url = f"{base}/contentunderstanding/analyzers/{analyzer_id}:analyze?api-version={api_version}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            op_location = resp.headers.get("Operation-Location") or resp.headers.get("operation-location")
            if not op_location:
                # Some accelerators return the result inline (rare); surface what we got.
                raise CuClientError(
                    f"Analyze POST returned HTTP {resp.status} but no Operation-Location header."
                )
            return op_location
    except urllib.error.HTTPError as exc:
        detail = _safe_read(exc)
        raise CuClientError(
            f"Analyze POST failed: HTTP {exc.code} {exc.reason} — {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CuClientError(f"Analyze POST network error: {exc.reason}") from exc


def _get_operation(operation_location: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        operation_location,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _safe_read(exc)
        raise CuClientError(
            f"Operation GET failed: HTTP {exc.code} {exc.reason} — {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CuClientError(f"Operation GET network error: {exc.reason}") from exc


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:  # noqa: BLE001
        return "<no body>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_file(
    *,
    endpoint: str,
    analyzer_id: str,
    file_bytes: bytes,
    file_name: str,
    api_version: str | None = None,
    poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
    overall_timeout_sec: float = _DEFAULT_OVERALL_TIMEOUT_SEC,
) -> CuAnalyzeResult:
    """Submit ``file_bytes`` to ``analyzer_id`` and wait for the result.

    Parameters
    ----------
    endpoint
        e.g. ``https://my-aiservices.cognitiveservices.azure.com`` (no trailing slash needed).
    analyzer_id
        The CU analyzer identifier — by convention this is the ``<analyzer>``
        segment of our blob layout (``incoming/<usecase>/<analyzer>/<file>``).
    file_bytes
        The raw document bytes (already quality-checked).
    file_name
        Original file name — used only to derive ``Content-Type``.
    api_version
        Defaults to ``CU_API_VERSION`` env var or ``2024-12-01-preview``.
    poll_interval_sec, overall_timeout_sec
        Polling cadence and overall wall-clock cap on the operation.

    Returns
    -------
    CuAnalyzeResult
        ``status="Succeeded"`` with ``result_json`` populated, or
        ``status="Failed"``/"Timeout"`` with ``error_message`` populated.
    """
    api_version = api_version or os.getenv("CU_API_VERSION") or _DEFAULT_API_VERSION
    content_type = content_type_for(file_name)
    token = _bearer_token()

    log.info(
        "CU analyze POST: analyzer=%s api=%s ct=%s bytes=%d",
        analyzer_id, api_version, content_type, len(file_bytes),
    )

    try:
        op_location = _post_analyze(
            endpoint=endpoint,
            analyzer_id=analyzer_id,
            api_version=api_version,
            body=file_bytes,
            content_type=content_type,
            token=token,
        )
    except CuClientError as exc:
        return CuAnalyzeResult(
            status="Failed",
            operation_location=None,
            result_json=None,
            error_message=str(exc)[:3900],
        )

    log.info("CU analyze accepted: %s", op_location)

    deadline = time.monotonic() + overall_timeout_sec
    last_payload: dict[str, Any] | None = None
    last_status = "Unknown"
    while True:
        try:
            payload = _get_operation(op_location, token)
        except CuClientError as exc:
            return CuAnalyzeResult(
                status="Failed",
                operation_location=op_location,
                result_json=None,
                error_message=str(exc)[:3900],
            )
        last_payload = payload
        last_status = str(payload.get("status") or "Unknown")
        # Terminal states (CU uses Pascal-cased values).
        if last_status.lower() == "succeeded":
            return CuAnalyzeResult(
                status="Succeeded",
                operation_location=op_location,
                result_json=payload,
                error_message=None,
            )
        if last_status.lower() in {"failed", "canceled", "cancelled"}:
            err = (
                (payload.get("error") or {}).get("message")
                or payload.get("statusMessage")
                or last_status
            )
            return CuAnalyzeResult(
                status="Failed",
                operation_location=op_location,
                result_json=payload,
                error_message=str(err)[:3900],
            )
        # Still running.
        if time.monotonic() >= deadline:
            return CuAnalyzeResult(
                status="Timeout",
                operation_location=op_location,
                result_json=last_payload,
                error_message=(
                    f"Operation did not reach terminal state within "
                    f"{overall_timeout_sec:.0f}s (last status={last_status})."
                ),
            )
        time.sleep(poll_interval_sec)
