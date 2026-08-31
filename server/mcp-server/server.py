#!/usr/bin/env python3
"""NetEase Music MCP plus a narrow authenticated JSON read surface."""

from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from listening import ListeningError, ListeningStore, parse_lrc

NETEASE_COOKIE = os.environ.get("NETEASE_COOKIE", "").strip()
NETEASE_SERVICE_TOKEN = os.environ.get("NETEASE_SERVICE_TOKEN", "").strip()
PORT = int(os.environ.get("MCP_PORT", "3456"))
SESSION_ID = str(uuid.uuid4())
MAX_REQUEST_BYTES = 64 * 1024


class MusicError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def netease_request(url: str, data: dict[str, Any] | str | None = None) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://music.163.com/",
        "Cookie": NETEASE_COOKIE,
        "Content-Type": "application/x-www-form-urlencoded" if data is not None else "application/json",
    }
    if isinstance(data, dict):
        encoded = urllib.parse.urlencode(data).encode()
    elif isinstance(data, str):
        encoded = data.encode()
    else:
        encoded = None
    request = urllib.request.Request(url, data=encoded, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(3 * 1024 * 1024 + 1)
            if len(payload) > 3 * 1024 * 1024:
                raise MusicError(502, "NetEase response is too large")
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise MusicError(502, "NetEase returned an invalid response")
            return parsed
    except MusicError:
        raise
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MusicError(502, "NetEase request failed") from error


def _upstream_ok(payload: dict[str, Any], *, require_profile: bool = False) -> dict[str, Any]:
    code = payload.get("code")
    if code in (301, 302, 401, -460) or (require_profile and not payload.get("profile")):
        raise MusicError(401, "NetEase account session is unavailable")
    if code not in (None, 200):
        raise MusicError(502, "NetEase returned an error")
    return payload


def _artist_names(song: dict[str, Any]) -> list[str]:
    artists = song.get("ar") or song.get("artists") or []
    return [str(item.get("name", "")) for item in artists if isinstance(item, dict) and item.get("name")]


def _album(song: dict[str, Any]) -> dict[str, Any]:
    album = song.get("al") or song.get("album") or {}
    return album if isinstance(album, dict) else {}


def _song_view(
    song: dict[str, Any],
    liked_ids: set[int] | None = None,
    *,
    play_count: int | None = None,
    score: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    album = _album(song)
    song_id = int(song.get("id") or 0)
    value = {
        "id": song_id,
        "name": str(song.get("name") or ""),
        "artists": _artist_names(song),
        "album": str(album.get("name") or ""),
        "cover_url": str(album.get("picUrl") or ""),
        "duration_ms": int(song.get("dt") or song.get("duration") or 0),
        "liked": song_id in (liked_ids or set()),
    }
    if play_count is not None:
        value["play_count"] = play_count
    if score is not None:
        value["score"] = score
    if reason:
        value["reason"] = reason
    return value


class NetEaseMusic:
    """Structured operations backed by the same request function as MCP tools."""

    def __init__(self, request: Callable[..., dict[str, Any]] = netease_request):
        self.request = request

    def account(self) -> dict[str, Any]:
        payload = _upstream_ok(self.request("https://music.163.com/api/nuser/account/get"), require_profile=True)
        profile = payload["profile"]
        account = payload.get("account") or {}
        return {
            "user_id": int(profile.get("userId") or account.get("id") or 0),
            "nickname": str(profile.get("nickname") or ""),
            "avatar_url": str(profile.get("avatarUrl") or ""),
            "signature": str(profile.get("signature") or ""),
            "follows": int(profile.get("follows") or 0),
            "followeds": int(profile.get("followeds") or 0),
            "playlist_count": int(profile.get("playlistCount") or 0),
            "vip_type": int(profile.get("vipType") or account.get("vipType") or 0),
        }

    def liked_ids(self, user_id: int | None = None) -> set[int]:
        uid = user_id or self.account()["user_id"]
        query = urllib.parse.urlencode({"uid": uid})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/song/like/get?{query}"))
        return {int(value) for value in payload.get("ids", []) if str(value).isdigit()}

    def playlists(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        account = self.account()
        query = urllib.parse.urlencode({"uid": account["user_id"], "limit": limit, "offset": offset})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/user/playlist?{query}"))
        values = []
        for item in payload.get("playlist") or []:
            creator = item.get("creator") or {}
            values.append({
                "id": int(item.get("id") or 0),
                "name": str(item.get("name") or ""),
                "cover_url": str(item.get("coverImgUrl") or ""),
                "description": str(item.get("description") or ""),
                "track_count": int(item.get("trackCount") or 0),
                "play_count": int(item.get("playCount") or 0),
                "owned": int(creator.get("userId") or 0) == account["user_id"],
                "subscribed": bool(item.get("subscribed")),
                "updated_at_ms": int(item.get("updateTime") or 0),
            })
        return {"playlists": values, "more": bool(payload.get("more")), "offset": offset, "limit": limit}

    def playlist(self, playlist_id: int, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        query = urllib.parse.urlencode({"id": playlist_id})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/v6/playlist/detail?{query}"))
        playlist = payload.get("playlist") or {}
        if not playlist:
            raise MusicError(404, "playlist not found")
        track_ids = [int(item.get("id")) for item in playlist.get("trackIds") or [] if str(item.get("id", "")).isdigit()]
        page_ids = track_ids[offset:offset + limit]
        present = {int(item.get("id") or 0): item for item in playlist.get("tracks") or []}
        missing = [song_id for song_id in page_ids if song_id not in present]
        if missing:
            encoded_ids = urllib.parse.quote(json.dumps(missing, separators=(",", ":")))
            detail = _upstream_ok(self.request(f"https://music.163.com/api/song/detail?ids={encoded_ids}"))
            present.update({int(item.get("id") or 0): item for item in detail.get("songs") or []})
        tracks = (playlist.get("tracks") or [])[offset:offset + limit] if not track_ids else [present[song_id] for song_id in page_ids if song_id in present]
        liked = self.liked_ids()
        creator = playlist.get("creator") or {}
        return {
            "playlist": {
                "id": int(playlist.get("id") or playlist_id),
                "name": str(playlist.get("name") or ""),
                "cover_url": str(playlist.get("coverImgUrl") or ""),
                "description": str(playlist.get("description") or ""),
                "track_count": int(playlist.get("trackCount") or len(track_ids)),
                "play_count": int(playlist.get("playCount") or 0),
                "creator": str(creator.get("nickname") or ""),
                "subscribed": bool(playlist.get("subscribed")),
            },
            "songs": [_song_view(item, liked) for item in tracks],
            "offset": offset,
            "limit": limit,
            "more": offset + len(tracks) < int(playlist.get("trackCount") or len(track_ids)),
        }

    def history(self, limit: int = 30, all_time: bool = False) -> dict[str, Any]:
        account = self.account()
        query = urllib.parse.urlencode({"uid": account["user_id"], "type": 0 if all_time else 1})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/v1/play/record?{query}"))
        records = (payload.get("allData") if all_time else payload.get("weekData")) or []
        liked = self.liked_ids(account["user_id"])
        songs = [
            _song_view(item.get("song") or {}, liked, play_count=int(item.get("playCount") or 0), score=int(item.get("score") or 0))
            for item in records[:limit]
        ]
        return {"songs": songs, "period": "all" if all_time else "week", "limit": limit}

    def recommendations(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({"csrf_token": get_csrf()})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/v3/discovery/recommend/songs?{query}", data="{}"))
        liked = self.liked_ids()
        songs = [_song_view(item, liked, reason=str(item.get("reason") or "")) for item in (payload.get("data") or {}).get("dailySongs") or []]
        return {"songs": songs[:30]}

    def search(self, query_text: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        query = urllib.parse.urlencode({"s": query_text, "type": 1, "limit": limit, "offset": offset})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/search/get?{query}"))
        result = payload.get("result") or {}
        liked = self.liked_ids()
        songs = [_song_view(item, liked) for item in result.get("songs") or []]
        return {"songs": songs, "song_count": int(result.get("songCount") or len(songs)), "offset": offset, "limit": limit}

    def song(self, song_id: int) -> dict[str, Any]:
        encoded = urllib.parse.quote(json.dumps([song_id], separators=(",", ":")))
        payload = _upstream_ok(self.request(f"https://music.163.com/api/song/detail?ids={encoded}"))
        songs = payload.get("songs") or []
        if not songs:
            raise MusicError(404, "song not found")
        return _song_view(songs[0], self.liked_ids())

    def play_source(self, song_id: int) -> dict[str, Any]:
        modern_data = {
            "ids": json.dumps([song_id], separators=(",", ":")),
            "level": "standard",
            "encodeType": "mp3",
        }
        try:
            payload = _upstream_ok(self.request(
                "https://music.163.com/api/song/enhance/player/url/v1",
                data=modern_data,
            ))
            data = (payload.get("data") or [None])[0] or {}
        except MusicError:
            data = {}

        raw = str(data.get("url") or "").strip()
        source_kind = "v1"
        if not raw:
            query = urllib.parse.urlencode({
                "id": song_id,
                "ids": json.dumps([song_id], separators=(",", ":")),
                "br": 320000,
            })
            payload = _upstream_ok(self.request(f"https://music.163.com/api/song/enhance/player/url?{query}"))
            data = (payload.get("data") or [None])[0] or {}
            raw = str(data.get("url") or "").strip()
            source_kind = "legacy"
        if not raw:
            raise MusicError(409, "playable source is unavailable")
        https_url = raw.replace("http://", "https://", 1) if raw.startswith("http://") else raw
        return {
            "track_id": song_id,
            "url": https_url,
            "bitrate": int(data.get("br") or 0),
            "expire_seconds": int(data.get("expi") or 0),
            "format": str(data.get("type") or ""),
            "level": str(data.get("level") or ("standard" if source_kind == "v1" else "")),
            "source_kind": source_kind,
            "song": self.song(song_id),
        }

    def lyric(self, song_id: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"id": song_id, "lv": -1, "tv": -1, "kv": -1})
        payload = _upstream_ok(self.request(f"https://music.163.com/api/song/lyric?{query}"))
        lyric = str((payload.get("lrc") or {}).get("lyric") or "")
        translated = str((payload.get("tlyric") or {}).get("lyric") or "")
        lines = parse_lrc(lyric)
        return {
            "track_id": song_id,
            "lyric": lyric[:20_000] or None,
            "translated_lyric": translated[:20_000] or None,
            "lines": lines,
        }


MUSIC = NetEaseMusic()
LISTENING = ListeningStore()


def get_csrf():
    for part in NETEASE_COOKIE.split(";"):
        part = part.strip()
        if part.startswith("__csrf="):
            return part.split("=", 1)[1]
    return ""


def play_music(query, note=None):
    try:
        songs = MUSIC.search(str(query or "").strip(), 5)["songs"]
        if not songs:
            return "No results for '" + str(query) + "'"
        song = songs[0]
        return "[music:{}:{}:{}:{}]{}".format(song["id"], song["name"].replace(":", "："), ", ".join(song["artists"]).replace(":", "："), song["cover_url"], note or "")
    except MusicError as error:
        return "Failed: " + error.message


def create_playlist(name, description="", privacy=0):
    url = "https://music.163.com/api/playlist/create?csrf_token=" + urllib.parse.quote(get_csrf())
    data = {"name": name, "privacy": str(privacy), "type": "NORMAL"}
    if description:
        data["description"] = description
    try:
        response = _upstream_ok(netease_request(url, data=data))
        return "Created playlist '{}' (ID: {})".format(name, (response.get("playlist") or {}).get("id"))
    except MusicError as error:
        return "Failed: " + error.message


def _playlist_tracks(operation, playlist_id, song_ids):
    values = [value.strip() for value in str(song_ids).split(",") if value.strip()]
    if not values or not all(value.isdigit() for value in values):
        return "Failed: invalid song IDs"
    url = "https://music.163.com/api/playlist/manipulate/tracks?csrf_token=" + urllib.parse.quote(get_csrf())
    data = {"op": operation, "pid": str(playlist_id), "trackIds": json.dumps([int(value) for value in values])}
    try:
        response = netease_request(url, data=data)
        if response.get("code") == 502 and operation == "add":
            return "Song already in playlist"
        _upstream_ok(response)
        verb = "Added" if operation == "add" else "Removed"
        return f"{verb} {len(values)} song(s) {'to' if operation == 'add' else 'from'} playlist {playlist_id}"
    except MusicError as error:
        return "Failed: " + error.message


def add_to_playlist(playlist_id, song_ids):
    return _playlist_tracks("add", playlist_id, song_ids)


def remove_from_playlist(playlist_id, song_ids):
    return _playlist_tracks("del", playlist_id, song_ids)


def list_my_playlists():
    try:
        playlists = MUSIC.playlists()["playlists"]
        if not playlists:
            return "No playlists found"
        return "\n".join(f"ID:{item['id']} | {item['name']} | {item['track_count']} songs {'(mine)' if item['owned'] else '(collected)'}" for item in playlists)
    except MusicError as error:
        return "Failed: " + error.message


def get_playlist_songs(playlist_id):
    try:
        result = MUSIC.playlist(int(playlist_id), 50)
        songs = result["songs"]
        if not songs:
            return f"Playlist {playlist_id} is empty"
        lines = [f"Playlist: {result['playlist']['name']} ({result['playlist']['track_count']} songs)"]
        lines.extend(f"{index}. {song['name']} - {', '.join(song['artists'])} (ID:{song['id']})" for index, song in enumerate(songs, 1))
        return "\n".join(lines)
    except (MusicError, TypeError, ValueError) as error:
        return "Failed: " + (error.message if isinstance(error, MusicError) else "invalid playlist ID")


def get_play_history(limit=30, all_time=False):
    try:
        songs = MUSIC.history(max(1, min(int(limit), 100)), bool(all_time))["songs"]
        if not songs:
            return "No play history found"
        lines = ["Recent play history:"]
        lines.extend(f"{index}. {song['name']} - {', '.join(song['artists'])} (plays:{song['play_count']}, ID:{song['id']})" for index, song in enumerate(songs, 1))
        return "\n".join(lines)
    except (MusicError, TypeError, ValueError) as error:
        return "Failed: " + (error.message if isinstance(error, MusicError) else "invalid limit")


def like_song(song_id, like=True):
    query = urllib.parse.urlencode({"alg": "itembased", "trackId": song_id, "like": "true" if like else "false", "time": 25, "csrf_token": get_csrf()})
    try:
        _upstream_ok(netease_request("https://music.163.com/api/radio/like?" + query))
        return ("Liked" if like else "Unliked") + " song " + str(song_id)
    except MusicError as error:
        return "Failed: " + error.message


def daily_recommend():
    try:
        songs = MUSIC.recommendations()["songs"]
        if not songs:
            return "No daily recommendations found"
        lines = ["Today's recommendations:"]
        for index, song in enumerate(songs, 1):
            line = f"{index}. {song['name']} - {', '.join(song['artists'])} (ID:{song['id']})"
            if song.get("reason"):
                line += " [" + song["reason"] + "]"
            lines.append(line)
        return "\n".join(lines)
    except MusicError as error:
        return "Failed: " + error.message


TOOLS = [
    {"name": "play_music", "description": "Search and play a song from NetEase Cloud Music.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "note": {"type": "string"}}, "required": ["query"]}},
    {"name": "create_playlist", "description": "Create a new playlist in NetEase account.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "privacy": {"type": "integer"}}, "required": ["name"]}},
    {"name": "add_to_playlist", "description": "Add song(s) to a playlist.", "inputSchema": {"type": "object", "properties": {"playlist_id": {"type": "integer"}, "song_ids": {"type": "string"}}, "required": ["playlist_id", "song_ids"]}},
    {"name": "remove_from_playlist", "description": "Remove song(s) from a playlist.", "inputSchema": {"type": "object", "properties": {"playlist_id": {"type": "integer"}, "song_ids": {"type": "string"}}, "required": ["playlist_id", "song_ids"]}},
    {"name": "list_my_playlists", "description": "List all playlists of the logged-in user.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_playlist_songs", "description": "Get songs in a playlist.", "inputSchema": {"type": "object", "properties": {"playlist_id": {"type": "integer"}}, "required": ["playlist_id"]}},
    {"name": "get_play_history", "description": "Get real NetEase play history.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}, "all_time": {"type": "boolean"}}}},
    {"name": "like_song", "description": "Like or unlike a song.", "inputSchema": {"type": "object", "properties": {"song_id": {"type": "integer"}, "like": {"type": "boolean"}}, "required": ["song_id"]}},
    {"name": "daily_recommend", "description": "Get today's personalized recommendations.", "inputSchema": {"type": "object", "properties": {}}},
]


def handle_jsonrpc(body):
    method = body.get("method", "")
    request_id = body.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "netease-music-mcp", "version": "2.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = body.get("params", {}).get("name", "")
        arguments = body.get("params", {}).get("arguments", {})
        handlers = {
            "play_music": lambda: play_music(arguments.get("query", ""), arguments.get("note")),
            "create_playlist": lambda: create_playlist(arguments.get("name", ""), arguments.get("description", ""), arguments.get("privacy", 0)),
            "add_to_playlist": lambda: add_to_playlist(arguments.get("playlist_id"), arguments.get("song_ids", "")),
            "remove_from_playlist": lambda: remove_from_playlist(arguments.get("playlist_id"), arguments.get("song_ids", "")),
            "list_my_playlists": list_my_playlists,
            "get_playlist_songs": lambda: get_playlist_songs(arguments.get("playlist_id")),
            "get_play_history": lambda: get_play_history(arguments.get("limit", 30), arguments.get("all_time", False)),
            "like_song": lambda: like_song(arguments.get("song_id"), arguments.get("like", True)),
            "daily_recommend": daily_recommend,
        }
        if name not in handlers:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unknown tool"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": handlers[name]()}]}}
    if method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unknown method"}}


def _server_time():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _integer(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    values = query.get(name)
    if values is None:
        return default
    if len(values) != 1 or not values[0].isdigit():
        raise MusicError(400, f"invalid {name}")
    value = int(values[0])
    if value < minimum or value > maximum:
        raise MusicError(400, f"invalid {name}")
    return value


class MCPHandler(http.server.BaseHTTPRequestHandler):
    music = MUSIC
    listening = LISTENING
    service_token = NETEASE_SERVICE_TOKEN

    def _authorized(self):
        if not self.service_token:
            raise MusicError(503, "music service authentication is not configured")
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.service_token):
            raise MusicError(401, "unauthorized")

    def _json_response(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, error):
        self._json_response({"error": error.message, "server_time": _server_time()}, error.status)

    def do_OPTIONS(self):
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health" and not parsed.query:
                self._json_response({"status": "ok", "tools": len(TOOLS)})
                return
            self._authorized()
            if parsed.path == "/sse" and not parsed.query:
                self._handle_sse()
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            result = self._read_route(parsed.path, query)
            result["server_time"] = _server_time()
            self._json_response(result)
        except (MusicError, ListeningError) as error:
            self._error(error)

    def _handle_sse(self):
        """Keep the repository's legacy MCP SSE transport, now authenticated."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()
        self.wfile.write(b"event: endpoint\ndata: /message\n\n")
        self.wfile.flush()
        try:
            while True:
                time.sleep(30)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_route(self, path, query):
        if path == "/v1/account" and not query:
            return {"account": self.music.account()}
        if path == "/v1/history":
            if set(query) - {"limit", "period"}:
                raise MusicError(400, "unknown query parameter")
            limit = _integer(query, "limit", 30, 1, 100)
            period = query.get("period", ["week"])
            if len(period) != 1 or period[0] not in {"week", "all"}:
                raise MusicError(400, "invalid period")
            return self.music.history(limit, period[0] == "all")
        if path == "/v1/playlists":
            if set(query) - {"limit", "offset"}:
                raise MusicError(400, "unknown query parameter")
            return self.music.playlists(_integer(query, "limit", 50, 1, 100), _integer(query, "offset", 0, 0, 10000))
        match = re.fullmatch(r"/v1/playlists/([1-9][0-9]{0,19})", path)
        if match:
            if set(query) - {"limit", "offset"}:
                raise MusicError(400, "unknown query parameter")
            return self.music.playlist(int(match.group(1)), _integer(query, "limit", 100, 1, 100), _integer(query, "offset", 0, 0, 100000))
        if path == "/v1/recommendations/daily" and not query:
            return self.music.recommendations()
        if path == "/v1/search":
            if set(query) - {"q", "limit", "offset"}:
                raise MusicError(400, "unknown query parameter")
            values = query.get("q")
            if not values or len(values) != 1 or not values[0].strip() or len(values[0]) > 100:
                raise MusicError(400, "q must be 1-100 characters")
            return self.music.search(values[0].strip(), _integer(query, "limit", 20, 1, 30), _integer(query, "offset", 0, 0, 10000))
        play = re.fullmatch(r"/v1/songs/([1-9][0-9]{0,19})/play", path)
        if play:
            if query:
                raise MusicError(400, "unknown query parameter")
            return self.music.play_source(int(play.group(1)))
        lyric = re.fullmatch(r"/v1/songs/([1-9][0-9]{0,19})/lyric", path)
        if lyric:
            if query:
                raise MusicError(400, "unknown query parameter")
            payload = self.music.lyric(int(lyric.group(1)))
            payload.pop("lines", None)
            return payload
        if path == "/v1/listening":
            if set(query) - {"include"}:
                raise MusicError(400, "unknown query parameter")
            include = query.get("include", ["session"])
            if len(include) != 1 or include[0] not in {"session", "lyric_window"}:
                raise MusicError(400, "invalid include")
            return self.listening.snapshot(include_lyric=include[0] == "lyric_window")
        raise MusicError(404, "not found")

    def do_POST(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.query:
                raise MusicError(404, "not found")
            if parsed.path == "/v1/listening":
                self._authorized()
                self._json_response(self._write_listening())
                return
            if parsed.path not in {"/mcp", "/message"}:
                raise MusicError(404, "not found")
            self._authorized()
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise MusicError(415, "application/json is required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise MusicError(400, "invalid content length") from error
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise MusicError(413 if length > MAX_REQUEST_BYTES else 400, "invalid request size")
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise MusicError(400, "invalid JSON") from error
            if not isinstance(body, dict):
                raise MusicError(400, "JSON-RPC body must be an object")
            if body.get("method", "").startswith("notifications/") or body.get("id") is None:
                self.send_response(204)
                self.end_headers()
                return
            self._json_response(handle_jsonrpc(body))
        except (MusicError, ListeningError) as error:
            self._error(error)

    def _read_json_body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise MusicError(415, "application/json is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise MusicError(400, "invalid content length") from error
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise MusicError(413 if length > MAX_REQUEST_BYTES else 400, "invalid request size")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MusicError(400, "invalid JSON") from error
        if not isinstance(body, dict):
            raise MusicError(400, "JSON body must be an object")
        return body

    def _write_listening(self):
        body = self._read_json_body()
        lyrics = None
        action = str(body.get("action") or "")
        if action in {"play", "next", "previous"}:
            track = body.get("track") if isinstance(body.get("track"), dict) else body
            try:
                track_id = int((track or {}).get("id") or (track or {}).get("track_id") or 0)
            except (TypeError, ValueError):
                track_id = 0
            if action == "play" and track_id < 1:
                raise MusicError(400, "track is required")
            if action in {"next", "previous"}:
                current = self.listening.snapshot().get("listening") or {}
                queue = current.get("queue") or []
                index = int(current.get("queue_index") or 0)
                if action == "next":
                    index = min(len(queue) - 1, index + 1) if queue else 0
                else:
                    index = max(0, index - 1)
                if queue:
                    track_id = int(queue[index]["id"])
            if track_id >= 1:
                try:
                    lyrics = self.music.lyric(track_id)
                except MusicError:
                    lyrics = None
                if action == "play" and not (isinstance(body.get("track"), dict) and body["track"].get("name")):
                    try:
                        body = dict(body)
                        body["track"] = self.music.song(track_id)
                    except MusicError:
                        pass
        result = self.listening.apply(body, lyrics)
        result["server_time"] = _server_time()
        return result

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        thread = threading.Thread(target=self._handle, args=(request, client_address), daemon=True)
        thread.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    if not NETEASE_COOKIE or not NETEASE_SERVICE_TOKEN:
        raise SystemExit("NETEASE_COOKIE and NETEASE_SERVICE_TOKEN are required")
    print("NetEase Music MCP v2.1 on port " + str(PORT))
    print("Tools: " + str(len(TOOLS)))
    ThreadedHTTPServer(("0.0.0.0", PORT), MCPHandler).serve_forever()
