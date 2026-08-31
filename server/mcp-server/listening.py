"""Canonical listening session for Xiaowo listen-together v1.

Lives in netease-music-mcp because Worker must not hold playback state,
LMC is not a position database, Companion is an observer, and tinggu is
audio analysis only. Optional file persist survives process bounce;
it is not a second music library.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class ListeningError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
LRC_LINE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)")
STALE_MS = 45_000
MAX_QUEUE = 50
ALLOWED_STATUS = {"playing", "paused", "stopped"}
ALLOWED_ACTIONS = {"play", "pause", "resume", "seek", "stop", "heartbeat", "next", "previous"}
ALLOWED_SOURCES = {"search", "playlist", "daily", "history", "queue"}
STATE_PATH = Path(os.environ.get("LISTENING_STATE_PATH", "/tmp/xiaowo-listening.json"))


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_ms(epoch_ms: int | None = None) -> str:
    value = (epoch_ms if epoch_ms is not None else now_ms()) / 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(value)) + ".%03dZ" % (int(value * 1000) % 1000)


def parse_lrc(text: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        matches = list(LRC_LINE.finditer(raw))
        if not matches:
            continue
        body = matches[-1].group(4).strip()
        if not body:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            frac = match.group(3) or "0"
            if len(frac) == 1:
                millis = int(frac) * 100
            elif len(frac) == 2:
                millis = int(frac) * 10
            else:
                millis = int(frac[:3])
            lines.append({
                "time_ms": minutes * 60_000 + seconds * 1000 + millis,
                "text": body[:200],
            })
    lines.sort(key=lambda item: item["time_ms"])
    return lines[:400]


def lyric_window(lines: list[dict[str, Any]], position_ms: int, before: int = 1, after: int = 2) -> dict[str, Any]:
    if not lines:
        return {"current": None, "before": [], "after": []}
    index = 0
    for i, line in enumerate(lines):
        if line["time_ms"] <= position_ms:
            index = i
        else:
            break
    start = max(0, index - max(0, before))
    end = min(len(lines), index + 1 + max(0, after))
    current = lines[index]
    return {
        "current": current,
        "before": lines[start:index],
        "after": lines[index + 1:end],
    }


def _queue_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    try:
        track_id = int(item.get("id") or item.get("track_id") or 0)
    except (TypeError, ValueError):
        return None
    if track_id < 1:
        return None
    artists = item.get("artists") or []
    if isinstance(artists, str):
        artists = [artists]
    names = [str(name) for name in artists if str(name).strip()][:8]
    return {
        "id": track_id,
        "name": str(item.get("name") or item.get("track_name") or "")[:120],
        "artists": names,
        "album": str(item.get("album") or "")[:120],
        "cover_url": str(item.get("cover_url") or "")[:500],
        "duration_ms": max(0, int(item.get("duration_ms") or 0)),
    }


def public_session(session: dict[str, Any] | None, *, include_lyric: bool = False) -> dict[str, Any]:
    if not session:
        return {"listening": None}
    cloned = dict(session)
    cloned.pop("lyric_lines", None)
    cloned.pop("lyric", None)
    cloned.pop("translated_lyric", None)
    cloned.pop("position_origin_ms", None)
    cloned.pop("last_heartbeat_at_ms", None)
    cloned.pop("updated_at_ms", None)
    if not include_lyric:
        cloned.pop("current_lyric_line", None)
        cloned.pop("lyric_window", None)
    return {"listening": cloned}


class ListeningStore:
    def __init__(self, path: Path | None = None):
        self._lock = threading.Lock()
        self._path = path or STATE_PATH
        self._session: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("session_id"):
                    self._session = payload
        except (OSError, json.JSONDecodeError):
            self._session = None

    def _save(self) -> None:
        if self._session is None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        persist = dict(self._session)
        persist.pop("lyric_lines", None)
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(persist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def _extrapolate(self, session: dict[str, Any]) -> dict[str, Any]:
        view = dict(session)
        view.pop("lyric_lines", None)
        duration = int(view.get("duration_ms") or 0)
        position = int(view.get("position_ms") or 0)
        if view.get("status") == "playing":
            elapsed = now_ms() - int(view.get("position_origin_ms") or view.get("updated_at_ms") or now_ms())
            position = max(0, position + max(0, elapsed))
            if duration:
                position = min(position, duration)
            view["position_ms"] = position
        lines = session.get("lyric_lines") or []
        if lines:
            window = lyric_window(lines, position)
            view["current_lyric_line"] = (window["current"] or {}).get("text")
            view["lyric_window"] = window
        return view

    def snapshot(self, *, include_lyric: bool = False) -> dict[str, Any]:
        with self._lock:
            if not self._session:
                return public_session(None)
            return public_session(self._extrapolate(self._session), include_lyric=include_lyric)

    def _stale(self, session: dict[str, Any]) -> bool:
        last = int(session.get("last_heartbeat_at_ms") or session.get("updated_at_ms") or 0)
        return now_ms() - last > STALE_MS

    def _assert_owner(self, session: dict[str, Any], owner: str, session_id: str | None, *, allow_takeover: bool) -> None:
        if session_id and session.get("session_id") != session_id:
            raise ListeningError(409, "listening session is stale")
        if session.get("playback_owner") == owner:
            return
        if allow_takeover and self._stale(session):
            return
        raise ListeningError(409, "playback owner conflict")

    def _set(self, session: dict[str, Any]) -> dict[str, Any]:
        session["revision"] = int(session.get("revision") or 0) + 1
        session["updated_at_ms"] = now_ms()
        session["updated_at"] = iso_ms(session["updated_at_ms"])
        session["last_heartbeat_at_ms"] = session["updated_at_ms"]
        session["position_origin_ms"] = session["updated_at_ms"]
        self._session = session
        self._save()
        return self._extrapolate(session)

    def apply(self, body: dict[str, Any], lyrics: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ListeningError(400, "JSON body must be an object")
        action = str(body.get("action") or "").strip()
        if action not in ALLOWED_ACTIONS:
            raise ListeningError(400, "invalid action")
        owner = str(body.get("playback_owner") or "").strip()
        if not OWNER_RE.fullmatch(owner):
            raise ListeningError(400, "invalid playback_owner")
        session_id = str(body.get("session_id") or "").strip() or None
        if session_id and not SESSION_RE.fullmatch(session_id):
            raise ListeningError(400, "invalid session_id")

        with self._lock:
            current = dict(self._session) if self._session else None
            if action == "play":
                if current and current.get("playback_owner") != owner and not self._stale(current):
                    raise ListeningError(409, "playback owner conflict")
                return public_session(self._play(current, body, owner, lyrics), include_lyric=False)
            if action in {"next", "previous"}:
                if current is None:
                    raise ListeningError(409, "no active listening session")
                self._assert_owner(current, owner, session_id, allow_takeover=self._stale(current))
                return public_session(self._play(current, body, owner, lyrics), include_lyric=False)
            if current is None:
                raise ListeningError(409, "no active listening session")
            self._assert_owner(current, owner, session_id, allow_takeover=action == "heartbeat")
            if action == "pause":
                current["status"] = "paused"
                current["position_ms"] = _position(body, current)
                return public_session(self._set(current), include_lyric=False)
            if action == "resume":
                current["status"] = "playing"
                current["position_ms"] = _position(body, current)
                return public_session(self._set(current), include_lyric=False)
            if action == "seek":
                current["position_ms"] = _position(body, current, required=True)
                if current.get("status") != "stopped":
                    current["status"] = str(body.get("status") or current.get("status") or "paused")
                    if current["status"] not in ALLOWED_STATUS:
                        raise ListeningError(400, "invalid status")
                return public_session(self._set(current), include_lyric=False)
            if action == "stop":
                current["status"] = "stopped"
                current["position_ms"] = _position(body, current)
                return public_session(self._set(current), include_lyric=False)
            current["position_ms"] = _position(body, current)
            if current.get("status") == "playing" or body.get("status") == "playing":
                current["status"] = "playing"
            return public_session(self._set(current), include_lyric=False)

    def _play(self, current: dict[str, Any] | None, body: dict[str, Any], owner: str, lyrics: dict[str, Any] | None) -> dict[str, Any]:
        queue = _normalize_queue(body.get("queue"))
        index = _index(body.get("queue_index"), queue)
        action = str(body.get("action"))
        if action == "next":
            if not current:
                raise ListeningError(409, "no active listening session")
            self._assert_owner(current, owner, str(body.get("session_id") or "") or None, allow_takeover=self._stale(current))
            queue = current.get("queue") or queue
            index = min(len(queue) - 1, int(current.get("queue_index") or 0) + 1) if queue else 0
        if action == "previous":
            if not current:
                raise ListeningError(409, "no active listening session")
            self._assert_owner(current, owner, str(body.get("session_id") or "") or None, allow_takeover=self._stale(current))
            queue = current.get("queue") or queue
            index = max(0, int(current.get("queue_index") or 0) - 1)
        track = _queue_item(body.get("track") or body)
        if action in {"next", "previous"} and queue:
            track = queue[index]
        if not track:
            raise ListeningError(400, "track is required")
        if not queue:
            queue = [track]
            index = 0
        source = _source(body.get("source"))
        started = iso_ms()
        session = {
            "session_id": current["session_id"] if current and current.get("session_id") and action in {"next", "previous"} else "lsn_" + uuid.uuid4().hex[:20],
            "revision": int(current.get("revision") or 0) if current and action in {"next", "previous"} else 0,
            "track_id": track["id"],
            "track_name": track["name"],
            "artists": track["artists"],
            "album": track["album"],
            "cover_url": track["cover_url"],
            "status": "playing",
            "position_ms": max(0, int(body.get("position_ms") or 0)),
            "duration_ms": track["duration_ms"],
            "started_at": started if action == "play" or not current else current.get("started_at") or started,
            "playback_owner": owner,
            "source": source,
            "queue": queue,
            "queue_index": index,
            "lyric": None,
            "translated_lyric": None,
            "current_lyric_line": None,
        }
        if lyrics:
            session["lyric"] = lyrics.get("lyric")
            session["translated_lyric"] = lyrics.get("translated_lyric")
            session["lyric_lines"] = lyrics.get("lines") or []
        return self._set(session)


def _position(body: dict[str, Any], current: dict[str, Any], required: bool = False) -> int:
    if "position_ms" not in body:
        if required:
            raise ListeningError(400, "position_ms is required")
        if current.get("status") == "playing":
            elapsed = now_ms() - int(current.get("position_origin_ms") or current.get("updated_at_ms") or now_ms())
            return max(0, int(current.get("position_ms") or 0) + max(0, elapsed))
        return max(0, int(current.get("position_ms") or 0))
    try:
        value = int(body.get("position_ms"))
    except (TypeError, ValueError) as error:
        raise ListeningError(400, "invalid position_ms") from error
    if value < 0 or value > 24 * 60 * 60 * 1000:
        raise ListeningError(400, "invalid position_ms")
    duration = int(current.get("duration_ms") or 0)
    if duration:
        value = min(value, duration)
    return value


def _normalize_queue(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ListeningError(400, "invalid queue")
    values = []
    for item in raw[:MAX_QUEUE]:
        parsed = _queue_item(item)
        if parsed:
            values.append(parsed)
    return values


def _index(raw: Any, queue: list[dict[str, Any]]) -> int:
    if not queue:
        return 0
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ListeningError(400, "invalid queue_index") from error
    if value < 0 or value >= len(queue):
        raise ListeningError(400, "invalid queue_index")
    return value


def _source(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"type": "queue"}
    if not isinstance(raw, dict):
        raise ListeningError(400, "invalid source")
    kind = str(raw.get("type") or "queue")
    if kind not in ALLOWED_SOURCES:
        raise ListeningError(400, "invalid source")
    source = {"type": kind}
    if raw.get("id") is not None:
        source["id"] = str(raw.get("id"))[:40]
    if raw.get("label"):
        source["label"] = str(raw.get("label"))[:80]
    return source
