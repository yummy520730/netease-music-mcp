#!/usr/bin/env python3
"""NetEase Music MCP entrypoint with EAPI playback and signed no-referrer redirects."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
_core = importlib.import_module("server_core")
analysis = importlib.import_module("analysis")

_AES_KEY = b"e82ckenh8dichen8"
_AUDIO_TTL_SECONDS = 120
_AUDIO_MAX_FUTURE_SECONDS = 300
_AUDIO_PATH_RE = re.compile(r"^/v1/audio/([1-9][0-9]{0,19})/([0-9]{10})/([0-9a-f]{32})$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
_CDN_RE = re.compile(r"^m([0-9]+)\.music\.126\.net$", re.IGNORECASE)

_SBOX = (
0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
)
_RCON = (0x00,0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36)


def _xtime(value: int) -> int:
    return (((value << 1) ^ 0x1B) & 0xFF) if value & 0x80 else ((value << 1) & 0xFF)


def _aes_expand(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 key required")
    words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
    for index in range(4, 44):
        temp = words[index - 1][:]
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[value] for value in temp]
            temp[0] ^= _RCON[index // 4]
        words.append([words[index - 4][offset] ^ temp[offset] for offset in range(4)])
    return [sum(words[index * 4:(index + 1) * 4], []) for index in range(11)]


def _aes_add(state: list[int], round_key: list[int]) -> None:
    for index in range(16):
        state[index] ^= round_key[index]


def _aes_sub(state: list[int]) -> None:
    for index in range(16):
        state[index] = _SBOX[state[index]]


def _aes_shift(state: list[int]) -> None:
    previous = state[:]
    for row in range(4):
        for column in range(4):
            state[4 * column + row] = previous[4 * ((column + row) % 4) + row]


def _aes_mix(state: list[int]) -> None:
    for column in range(4):
        index = 4 * column
        values = state[index:index + 4]
        total = values[0] ^ values[1] ^ values[2] ^ values[3]
        first = values[0]
        state[index] = values[0] ^ total ^ _xtime(values[0] ^ values[1])
        state[index + 1] = values[1] ^ total ^ _xtime(values[1] ^ values[2])
        state[index + 2] = values[2] ^ total ^ _xtime(values[2] ^ values[3])
        state[index + 3] = values[3] ^ total ^ _xtime(values[3] ^ first)


def _aes_encrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 16:
        raise ValueError("AES block must be 16 bytes")
    state = list(block)
    round_keys = _aes_expand(key)
    _aes_add(state, round_keys[0])
    for round_index in range(1, 10):
        _aes_sub(state)
        _aes_shift(state)
        _aes_mix(state)
        _aes_add(state, round_keys[round_index])
    _aes_sub(state)
    _aes_shift(state)
    _aes_add(state, round_keys[10])
    return bytes(state)


def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES ECB input must align to 16 bytes")
    return b"".join(_aes_encrypt_block(data[index:index + 16], key) for index in range(0, len(data), 16))


def _pkcs7(data: bytes) -> bytes:
    padding = 16 - (len(data) % 16)
    return data + bytes([padding]) * padding


def _cookie_value(name: str) -> str:
    for part in _core.NETEASE_COOKIE.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return ""


def _eapi_cipher(api_path: str, query_body: dict[str, Any], cookies: dict[str, str]) -> bytes:
    request_text = json.dumps({**query_body, "header": cookies}, separators=(",", ":"))
    message = f"nobody{api_path}use{request_text}md5forencrypt".encode("latin1")
    digest = hashlib.md5(message).hexdigest()
    plaintext = f"{api_path}-36cd479b6b5-{request_text}-36cd479b6b5-{digest}".encode("latin1")
    encrypted = _aes_ecb_encrypt(_pkcs7(plaintext), _AES_KEY)
    return b"params=" + encrypted.hex().upper().encode("ascii")


def _eapi_json(path: str, query_body: dict[str, Any]) -> dict[str, Any]:
    music_u = _cookie_value("MUSIC_U")
    cookies = {
        "osver": "undefined",
        "deviceId": "undefined",
        "appver": "8.0.0",
        "versioncode": "140",
        "mobilename": "undefined",
        "buildver": "1623435496",
        "resolution": "1920x1080",
        "__csrf": "",
        "os": "pc",
        "channel": "undefined",
        "requestId": f"{int(time.time() * 1000)}_{random.randint(0, 1000):04}",
    }
    if music_u:
        cookies["MUSIC_U"] = music_u
    api_path = "/api" + path
    request = urllib.request.Request(
        "https://interface3.music.163.com/eapi" + path,
        data=_eapi_cipher(api_path, query_body, cookies),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://music.163.com",
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(3 * 1024 * 1024 + 1)
            if len(payload) > 3 * 1024 * 1024:
                raise _core.MusicError(502, "NetEase response is too large")
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise _core.MusicError(502, "NetEase returned an invalid response")
            return parsed
    except _core.MusicError:
        raise
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _core.MusicError(502, "NetEase EAPI request failed") from error


def _browser_cdn_url(raw: str) -> str:
    value = raw.replace("http://", "https://", 1) if raw.startswith("http://") else raw
    parsed = urllib.parse.urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    match = _CDN_RE.fullmatch(hostname)
    if match:
        compatible_host = f"m{match.group(1)}c.music.126.net"
        port = f":{parsed.port}" if parsed.port else ""
        parsed = parsed._replace(netloc=compatible_host + port)
        return urllib.parse.urlunsplit(parsed)
    return value


def _patched_play_source(self, song_id: int) -> dict[str, Any]:
    modern_data = {
        "ids": json.dumps([song_id], separators=(",", ":")),
        "level": "standard",
        "encodeType": "mp3",
    }
    try:
        if self.request is _core.netease_request:
            eapi_data = {**modern_data, "encodeType": "flac"}
            payload = _core._upstream_ok(_eapi_json("/song/enhance/player/url/v1", eapi_data))
            transport = "eapi"
        else:
            payload = _core._upstream_ok(self.request(
                "https://interface3.music.163.com/eapi/song/enhance/player/url/v1",
                data=modern_data,
            ))
            transport = "test"
        data = (payload.get("data") or [None])[0] or {}
    except _core.MusicError:
        data = {}
        transport = "legacy"

    raw = str(data.get("url") or "").strip()
    source_kind = "v1"
    if not raw:
        query = urllib.parse.urlencode({
            "id": song_id,
            "ids": json.dumps([song_id], separators=(",", ":")),
            "br": 320000,
        })
        payload = _core._upstream_ok(self.request(f"https://music.163.com/api/song/enhance/player/url?{query}"))
        data = (payload.get("data") or [None])[0] or {}
        raw = str(data.get("url") or "").strip()
        source_kind = "legacy"
        transport = "legacy"
    if not raw:
        raise _core.MusicError(409, "playable source is unavailable")
    return {
        "track_id": song_id,
        "url": raw.replace("http://", "https://", 1) if raw.startswith("http://") else raw,
        "bitrate": int(data.get("br") or 0),
        "expire_seconds": int(data.get("expi") or 0),
        "format": str(data.get("type") or ""),
        "level": str(data.get("level") or ("standard" if source_kind == "v1" else "")),
        "source_kind": source_kind,
        "transport": transport,
        "song": self.song(song_id),
    }


def _audio_signature(secret: str, song_id: int, expires: int) -> str:
    message = f"audio:{song_id}:{expires}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


def _signed_audio_path(secret: str, song_id: int, *, now: int | None = None) -> str:
    timestamp = int(time.time() if now is None else now)
    expires = timestamp + _AUDIO_TTL_SECONDS
    return f"/v1/audio/{song_id}/{expires}/{_audio_signature(secret, song_id, expires)}"


def _verify_audio_path(secret: str, path: str, *, now: int | None = None) -> int | None:
    match = _AUDIO_PATH_RE.fullmatch(path)
    if not match or not secret:
        return None
    song_id = int(match.group(1))
    expires = int(match.group(2))
    timestamp = int(time.time() if now is None else now)
    if expires < timestamp or expires > timestamp + _AUDIO_MAX_FUTURE_SECONDS:
        return None
    expected = _audio_signature(secret, song_id, expires)
    if not hmac.compare_digest(match.group(3), expected):
        return None
    return song_id


def _public_origin(handler) -> str:
    host = (handler.headers.get("Host") or "").strip()
    if not _HOST_RE.fullmatch(host):
        raise _core.MusicError(400, "invalid host")
    hostname = host.split(":", 1)[0].lower()
    scheme = "http" if hostname in {"127.0.0.1", "localhost"} else "https"
    return f"{scheme}://{host}"


_original_read_route = _core.MCPHandler._read_route
_original_do_get = _core.MCPHandler.do_GET
_original_after_listening_write = _core.MCPHandler._after_listening_write


def _patched_read_route(self, path, query):
    analysis_match = re.fullmatch(r"/v1/songs/([1-9][0-9]{0,19})/analysis", path)
    if analysis_match:
        if query:
            raise _core.MusicError(400, "unknown query parameter")
        if not self.service_token:
            raise _core.MusicError(503, "music service authentication is not configured")
        try:
            return analysis.read_analysis(int(analysis_match.group(1)))
        except analysis.AnalysisError as error:
            raise _core.MusicError(error.status, error.message) from error
    result = _original_read_route(self, path, query)
    if re.fullmatch(r"/v1/songs/[1-9][0-9]{0,19}/play", path):
        song_id = int(result.get("track_id") or 0)
        if song_id < 1 or not self.service_token:
            raise _core.MusicError(503, "music service authentication is not configured")
        result = dict(result)
        result["url"] = _public_origin(self) + _signed_audio_path(self.service_token, song_id)
    return result


def _send_audio_redirect(self, target: str) -> None:
    self.send_response(302)
    self.send_header("Location", target)
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("Cache-Control", "private, no-store")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Content-Length", "0")
    self.end_headers()


def _patched_do_get(self):
    parsed = urllib.parse.urlsplit(self.path)
    if parsed.query:
        return _original_do_get(self)
    song_id = _verify_audio_path(self.service_token, parsed.path)
    if song_id is None:
        if parsed.path.startswith("/v1/audio/"):
            self._json_response({"error": "invalid or expired audio link", "server_time": _core._server_time()}, 403)
            return
        return _original_do_get(self)
    try:
        source = self.music.play_source(song_id)
        target = str(source.get("url") or "")
        if not target.startswith("https://"):
            raise _core.MusicError(502, "invalid playback source")
        _send_audio_redirect(self, _browser_cdn_url(target))
    except (_core.MusicError, _core.ListeningError) as error:
        self._error(error)


def _analysis_source_url(music, track_id: int) -> str | None:
    try:
        source = music.play_source(int(track_id))
    except Exception:
        return None
    raw = str((source or {}).get("url") or "")
    target = _browser_cdn_url(raw)
    if not target.startswith("https://"):
        return None
    if any(part in target for part in ("MUSIC_U", "Bearer ", "NETEASE_", "TINGGU_")):
        return None
    return target


def _ensure_track_analysis(music, track_id: int) -> None:
    try:
        url = _analysis_source_url(music, track_id)
        if not url:
            return
        analysis.ensure_analysis(int(track_id), url)
    except Exception:
        return


def _patched_after_listening_write(self, action, result):
    if action not in analysis.TRIGGER_ACTIONS:
        return
    listening = (result or {}).get("listening") or {}
    try:
        track_id = int(listening.get("track_id") or 0)
    except (TypeError, ValueError):
        track_id = 0
    if track_id < 1:
        return
    thread = threading.Thread(target=_ensure_track_analysis, args=(self.music, track_id), daemon=True)
    thread.start()


_core.NetEaseMusic.play_source = _patched_play_source
_core.MCPHandler._read_route = _patched_read_route
_core.MCPHandler.do_GET = _patched_do_get
_core.MCPHandler._after_listening_write = _patched_after_listening_write
_core.MCPHandler.music = _core.MUSIC

# Keep the original import surface for existing tests and callers.
for _name, _value in vars(_core).items():
    if _name not in {"__name__", "__file__", "__package__", "__loader__", "__spec__"}:
        globals().setdefault(_name, _value)


if __name__ == "__main__":
    if not _core.NETEASE_COOKIE or not _core.NETEASE_SERVICE_TOKEN:
        raise SystemExit("NETEASE_COOKIE and NETEASE_SERVICE_TOKEN are required")
    print("NetEase Music MCP v2.1 on port " + str(_core.PORT))
    print("Tools: " + str(len(_core.TOOLS)))
    _core.ThreadedHTTPServer(("0.0.0.0", _core.PORT), _core.MCPHandler).serve_forever()
