"""Join canonical current track to tinggu analysis. Fail-open on playback."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("netease.analysis")

TINGGU_BASE_URL = os.environ.get("TINGGU_BASE_URL", "").strip()
TINGGU_BRIDGE_TOKEN = os.environ.get("TINGGU_BRIDGE_TOKEN", "").strip()
ENSURE_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 256 * 1024
OBSERVER_FIELDS = (
    "bpm",
    "bpm_note",
    "global_key",
    "energy_segments",
    "peak_energy_time",
    "burst_time",
    "burst_note",
    "dynamic_range_db",
    "brightness_hz",
    "brightness_desc",
    "shape",
    "report_zh",
)
TRIGGER_ACTIONS = frozenset({"play", "next", "previous"})


class AnalysisError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def analysis_key(track_id: int) -> str:
    return f"netease:{int(track_id)}"


def _origin(raw: str | None = None) -> str:
    value = str(raw if raw is not None else TINGGU_BASE_URL or "").strip().rstrip("/")
    if not value:
        raise AnalysisError(503, "analysis service is not configured")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise AnalysisError(503, "TINGGU_BASE_URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AnalysisError(503, "TINGGU_BASE_URL must be credential-free")
    return value


def _token(raw: str | None = None) -> str:
    value = str(raw if raw is not None else TINGGU_BRIDGE_TOKEN or "").strip()
    if not value:
        raise AnalysisError(503, "analysis service is not configured")
    return value


def observer_view(payload: dict[str, Any], track_id: int) -> dict[str, Any]:
    status = str(payload.get("status") or "missing")
    if status not in {"missing", "queued", "running", "done", "error"}:
        status = "error"
    analysis = None
    if status == "done" and isinstance(payload.get("analysis"), dict):
        raw = payload["analysis"]
        analysis = {name: raw[name] for name in OBSERVER_FIELDS if name in raw}
    out: dict[str, Any] = {
        "track_id": int(track_id),
        "status": status,
        "version": payload.get("version"),
        "analysis": analysis,
    }
    dumped = json.dumps(out, ensure_ascii=False)
    for needle in ("Bearer ", "MUSIC_U", "TINGGU_BRIDGE_TOKEN", "NETEASE_COOKIE"):
        if needle in dumped:
            raise AnalysisError(502, "analysis payload rejected")
    return out


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float,
    opener: Callable[..., Any],
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    origin = _origin(base_url)
    secret = _token(token)
    request = urllib.request.Request(
        origin + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + secret,
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
        method=method,
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        raise AnalysisError(502 if error.code < 500 else 503, "analysis service is unavailable") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AnalysisError(503, "analysis service is unavailable") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise AnalysisError(502, "analysis service response is too large")
    if status >= 400:
        raise AnalysisError(502 if status < 500 else 503, "analysis service is unavailable")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AnalysisError(502, "analysis service returned invalid JSON") from error
    if not isinstance(parsed, dict):
        raise AnalysisError(502, "analysis service returned invalid JSON")
    return parsed


def ensure_analysis(
    track_id: int,
    source_url: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    url = str(source_url or "").strip()
    if not url.startswith("https://"):
        return None
    lowered = url.lower()
    if any(part in url for part in ("MUSIC_U", "Bearer ", "NETEASE_", "TINGGU_")):
        return None
    if "cookie" in lowered:
        return None
    try:
        return _request(
            "POST",
            "/v1/analysis/ensure",
            body={"key": analysis_key(track_id), "url": url},
            timeout=ENSURE_TIMEOUT_SECONDS,
            opener=opener,
            base_url=base_url,
            token=token,
        )
    except Exception as error:
        logger.warning("analysis ensure failed for %s: %s", track_id, type(error).__name__)
        return None


def read_analysis(
    track_id: int,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    payload = _request(
        "GET",
        "/v1/analysis?key=" + urllib.parse.quote(analysis_key(track_id), safe=":_-"),
        timeout=READ_TIMEOUT_SECONDS,
        opener=opener,
        base_url=base_url,
        token=token,
    )
    return observer_view(payload, track_id)
