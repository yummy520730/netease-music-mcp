import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


spec = importlib.util.spec_from_file_location("netease_server", Path(__file__).with_name("server.py"))
music_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(music_server)


def song(song_id=11, name="歌", liked=True):
    return {
        "id": song_id,
        "name": name,
        "ar": [{"name": "歌手"}],
        "al": {"name": "专辑", "picUrl": "https://img.example/cover.jpg"},
        "dt": 123000,
        "liked": liked,
    }


class FakeUpstream:
    def __init__(self, empty=False, expired=False, modern_error=False):
        self.empty = empty
        self.expired = expired
        self.modern_error = modern_error
        self.urls = []

    def __call__(self, url, data=None):
        self.urls.append((url, data))
        if "account/get" in url:
            if self.expired:
                return {"code": 301}
            return {"code": 200, "account": {"id": 7, "vipType": 11}, "profile": {"userId": 7, "nickname": "芥子", "avatarUrl": "https://img.example/a.jpg", "playlistCount": 2}}
        if "song/like/get" in url:
            return {"code": 200, "ids": [] if self.empty else [11]}
        if "user/playlist" in url:
            return {"code": 200, "playlist": [] if self.empty else [{"id": 99, "name": "喜欢的音乐", "creator": {"userId": 7}, "trackCount": 1, "subscribed": False}], "more": False}
        if "playlist/detail" in url:
            return {"code": 200, "playlist": {"id": 99, "name": "喜欢的音乐", "creator": {"nickname": "芥子"}, "trackCount": 1, "trackIds": [{"id": 11}], "tracks": [song()]}}
        if "play/record" in url:
            return {"code": 200, "weekData": [] if self.empty else [{"song": song(), "playCount": 8, "score": 99}]}
        if "recommend/songs" in url:
            return {"code": 200, "data": {"dailySongs": [] if self.empty else [{**song(), "reason": "猜你喜欢"}]}}
        if "search/get" in url:
            return {"code": 200, "result": {"songs": [] if self.empty else [song()], "songCount": 0 if self.empty else 1}}
        if "enhance/player/url/v1" in url:
            if self.modern_error:
                return {"code": 500}
            if self.empty:
                return {"code": 200, "data": [{"id": 11, "url": None, "br": 0, "expi": 0, "type": None, "level": "standard"}]}
            return {"code": 200, "data": [{"id": 11, "url": "https://m801.music.126.net/v1.mp3", "br": 128000, "expi": 1200, "type": "mp3", "level": "standard"}]}
        if "enhance/player/url" in url:
            if self.empty:
                return {"code": 200, "data": [{"id": 11, "url": None, "br": 0, "expi": 0}]}
            return {"code": 200, "data": [{"id": 11, "url": "http://m801.music.126.net/x.mp3", "br": 320000, "expi": 1200}]}
        if "song/lyric" in url:
            return {"code": 200, "lrc": {"lyric": "[00:00.00]窗前\n[00:10.00]明月光"}, "tlyric": {"lyric": ""}}
        if "song/detail" in url:
            return {"code": 200, "songs": [] if self.empty else [song()]}
        raise AssertionError(url)


class StructuredMusicTests(unittest.TestCase):
    def test_real_shape_account_lists_details_history_recommendations_and_search(self):
        upstream = FakeUpstream()
        api = music_server.NetEaseMusic(upstream)
        self.assertEqual(api.account()["nickname"], "芥子")
        self.assertTrue(api.playlists()["playlists"][0]["owned"])
        self.assertTrue(api.playlist(99)["songs"][0]["liked"])
        self.assertEqual(api.history()["songs"][0]["play_count"], 8)
        self.assertEqual(api.recommendations()["songs"][0]["reason"], "猜你喜欢")
        self.assertEqual(api.search("歌")["song_count"], 1)
        self.assertTrue(any("/api/song/like/get?uid=7" in url for url, _ in upstream.urls))
        play = api.play_source(11)
        self.assertTrue(play["url"].startswith("https://"))
        self.assertEqual(play["source_kind"], "v1")
        self.assertEqual(play["format"], "mp3")
        self.assertEqual(play["level"], "standard")
        modern = [(url, data) for url, data in upstream.urls if "enhance/player/url/v1" in url]
        self.assertEqual(len(modern), 1)
        self.assertEqual(modern[0][1]["level"], "standard")
        self.assertEqual(modern[0][1]["encodeType"], "mp3")
        self.assertEqual(modern[0][1]["ids"], "[11]")
        self.assertNotIn("MUSIC_U", json.dumps(play))
        self.assertEqual(api.lyric(11)["lines"][0]["text"], "窗前")

    def test_modern_player_url_failure_falls_back_to_legacy_source(self):
        upstream = FakeUpstream(modern_error=True)
        play = music_server.NetEaseMusic(upstream).play_source(11)
        self.assertEqual(play["source_kind"], "legacy")
        self.assertEqual(play["url"], "https://m801.music.126.net/x.mp3")
        play_calls = [(url, data) for url, data in upstream.urls if "enhance/player/url" in url]
        self.assertEqual(len(play_calls), 2)
        self.assertIn("/url/v1", play_calls[0][0])
        self.assertIn("br=320000", play_calls[1][0])

    def test_empty_lists_are_successful_empty_arrays(self):
        api = music_server.NetEaseMusic(FakeUpstream(empty=True))
        self.assertEqual(api.playlists()["playlists"], [])
        self.assertEqual(api.history()["songs"], [])
        self.assertEqual(api.recommendations()["songs"], [])
        self.assertEqual(api.search("没有")["songs"], [])

    def test_missing_playable_source_is_a_real_failure(self):
        with self.assertRaises(music_server.MusicError) as error:
            music_server.NetEaseMusic(FakeUpstream(empty=True)).play_source(11)
        self.assertEqual((error.exception.status, error.exception.message), (409, "playable source is unavailable"))

    def test_expired_account_and_upstream_errors_are_distinct(self):
        with self.assertRaises(music_server.MusicError) as expired:
            music_server.NetEaseMusic(FakeUpstream(expired=True)).account()
        self.assertEqual(expired.exception.status, 401)
        with self.assertRaises(music_server.MusicError) as upstream:
            music_server._upstream_ok({"code": 500, "message": "do not leak"})
        self.assertEqual((upstream.exception.status, upstream.exception.message), (502, "NetEase returned an error"))

    def test_existing_mcp_surface_is_preserved(self):
        names = [tool["name"] for tool in music_server.TOOLS]
        self.assertEqual(names, ["play_music", "create_playlist", "add_to_playlist", "remove_from_playlist", "list_my_playlists", "get_playlist_songs", "get_play_history", "like_song", "daily_recommend"])

    def test_existing_write_tools_still_use_the_same_cookie_backed_request(self):
        calls = []
        original = music_server.netease_request
        music_server.netease_request = lambda url, data=None: calls.append((url, data)) or {"code": 200, "playlist": {"id": 99}}
        try:
            self.assertIn("Created playlist", music_server.create_playlist("共同歌单"))
            self.assertIn("Added 2", music_server.add_to_playlist(99, "11,12"))
            self.assertIn("Removed 1", music_server.remove_from_playlist(99, "11"))
            self.assertEqual(music_server.like_song(11, False), "Unliked song 11")
        finally:
            music_server.netease_request = original
        self.assertTrue(any("/api/playlist/create" in url for url, _ in calls))
        self.assertTrue(any(data and data.get("op") == "add" for _, data in calls))
        self.assertTrue(any(data and data.get("op") == "del" for _, data in calls))
        self.assertTrue(any("like=false" in url for url, _ in calls))


class StubMusic:
    def account(self): return {"user_id": 7, "nickname": "芥子"}
    def playlists(self, limit, offset): return {"playlists": [], "more": False, "limit": limit, "offset": offset}
    def playlist(self, playlist_id, limit, offset): return {"playlist": {"id": playlist_id}, "songs": [], "limit": limit, "offset": offset, "more": False}
    def history(self, limit, all_time): return {"songs": [], "period": "all" if all_time else "week", "limit": limit}
    def recommendations(self): return {"songs": []}
    def search(self, query, limit, offset): return {"songs": [], "song_count": 0, "limit": limit, "offset": offset}
    def song(self, song_id): return {"id": song_id, "name": "歌", "artists": ["歌手"], "album": "专辑", "cover_url": "", "duration_ms": 1000, "liked": False}
    def play_source(self, song_id): return {"track_id": song_id, "url": "https://cdn.example/x.mp3", "bitrate": 128000, "expire_seconds": 1200, "format": "mp3", "level": "standard", "source_kind": "v1", "song": self.song(song_id)}
    def lyric(self, song_id): return {"track_id": song_id, "lyric": None, "translated_lyric": None, "lines": []}


class Handler(music_server.MCPHandler):
    music = StubMusic()
    listening = music_server.ListeningStore(Path(tempfile.mkdtemp()) / "listening.json")
    service_token = "service-secret"


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = music_server.ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def request(self, path, token=None, method="GET", body=None):
        headers = {}
        if token is not None: headers["Authorization"] = "Bearer " + token
        if body is not None: headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_auth_success_and_empty_list(self):
        self.assertEqual(self.request("/v1/account")[0], 401)
        status, payload = self.request("/v1/playlists?limit=10&offset=0", "service-secret")
        self.assertEqual(status, 200)
        self.assertEqual(payload["playlists"], [])

    def test_routes_queries_and_methods_fail_closed(self):
        self.assertEqual(self.request("/v1/files", "service-secret")[0], 404)
        self.assertEqual(self.request("/v1/history?extra=1", "service-secret")[0], 400)
        self.assertEqual(self.request("/v1/account", "service-secret", "POST", b"{}")[0], 404)

    def test_mcp_requires_auth_and_still_lists_nine_tools(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        self.assertEqual(self.request("/mcp", method="POST", body=body)[0], 401)
        status, payload = self.request("/mcp", "service-secret", "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]["tools"]), 9)

    def test_legacy_sse_requires_auth(self):
        self.assertEqual(self.request("/sse")[0], 401)

    def test_listening_session_http_contract(self):
        self.assertEqual(self.request("/v1/listening")[0], 401)
        status, payload = self.request("/v1/listening", "service-secret")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["listening"])
        body = json.dumps({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": {"id": 11, "name": "歌", "artists": ["歌手"], "duration_ms": 1000},
        }).encode()
        status, payload = self.request("/v1/listening", "service-secret", "POST", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["listening"]["track_id"], 11)
        self.assertNotIn("url", payload["listening"])
        stale = json.dumps({
            "action": "heartbeat",
            "playback_owner": "other-client",
            "session_id": payload["listening"]["session_id"],
            "position_ms": 10,
        }).encode()
        self.assertEqual(self.request("/v1/listening", "service-secret", "POST", stale)[0], 409)


if __name__ == "__main__":
    unittest.main()
