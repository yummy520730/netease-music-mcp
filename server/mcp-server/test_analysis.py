import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as analysis_mod
import server as music_server


def track(song_id=11, name="歌"):
    return {
        "id": song_id,
        "name": name,
        "artists": ["歌手"],
        "album": "专辑",
        "cover_url": "https://img.example/cover.jpg",
        "duration_ms": 1000,
    }


class TingguHandler(BaseHTTPRequestHandler):
    requests = []
    status = 200
    body = {"key": "netease:11", "status": "queued", "version": "shallow-0.4.0", "cache_hit": False}
    ensure_count = 0

    def do_GET(self):
        type(self).requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        self._reply(self.body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).ensure_count += 1
        type(self).requests.append(("POST", self.path, self.headers.get("Authorization"), raw))
        self._reply(self.body)

    def _reply(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.status < 400:
            self.wfile.write(data)

    def log_message(self, *_args):
        pass


class StubMusic:
    play_calls = []

    def play_source(self, song_id):
        type(self).play_calls.append(song_id)
        return {
            "track_id": song_id,
            "url": "https://m801.music.126.net/source.mp3",
            "bitrate": 128000,
            "expire_seconds": 1200,
            "format": "mp3",
            "level": "standard",
            "source_kind": "v1",
            "song": self.song(song_id),
        }

    def song(self, song_id):
        return {"id": song_id, "name": "歌", "artists": ["歌手"], "album": "专辑", "cover_url": "", "duration_ms": 1000, "liked": False}

    def lyric(self, song_id):
        return {"track_id": song_id, "lyric": None, "translated_lyric": None, "lines": []}

    def account(self):
        return {"user_id": 7, "nickname": "芥子"}

    def playlists(self, limit, offset):
        return {"playlists": [], "more": False, "limit": limit, "offset": offset}

    def playlist(self, playlist_id, limit, offset):
        return {"playlist": {"id": playlist_id}, "songs": [], "limit": limit, "offset": offset, "more": False}

    def history(self, limit, all_time):
        return {"songs": [], "period": "week", "limit": limit}

    def recommendations(self):
        return {"songs": []}

    def search(self, query, limit, offset):
        return {"songs": [], "song_count": 0, "limit": limit, "offset": offset}


class Handler(music_server.MCPHandler):
    music = StubMusic()
    listening = music_server.ListeningStore(Path(tempfile.mkdtemp()) / "listening.json")
    service_token = "service-secret"


class AnalysisJoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tinggu = HTTPServer(("127.0.0.1", 0), TingguHandler)
        cls.tinggu_thread = threading.Thread(target=cls.tinggu.serve_forever, daemon=True)
        cls.tinggu_thread.start()
        cls.tinggu_base = f"http://127.0.0.1:{cls.tinggu.server_port}"
        cls.server = music_server.ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tinggu.shutdown()
        cls.tinggu.server_close()
        cls.tinggu_thread.join(timeout=2)

    def setUp(self):
        TingguHandler.requests = []
        TingguHandler.ensure_count = 0
        TingguHandler.status = 200
        TingguHandler.body = {
            "key": "netease:11",
            "status": "queued",
            "version": "shallow-0.4.0",
            "cache_hit": False,
        }
        StubMusic.play_calls = []
        Handler.listening = music_server.ListeningStore(Path(tempfile.mkdtemp()) / "listening.json")
        self.tinggu_env = {
            "base_url": self.tinggu_base,
            "token": "bridge-secret",
        }

    def request(self, path, token="service-secret", method="GET", body=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def play(self, song_id=11):
        body = json.dumps({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(song_id),
            "queue": [track(song_id), track(12, "下一首")],
            "queue_index": 0,
        }).encode()
        return self.request("/v1/listening", method="POST", body=body)

    def test_play_survives_tinggu_failure(self):
        with mock.patch.object(music_server.analysis, "ensure_analysis", side_effect=RuntimeError("tinggu down")):
            status, payload = self.play()
        self.assertEqual(status, 200)
        self.assertEqual(payload["listening"]["track_id"], 11)
        self.assertEqual(payload["listening"]["status"], "playing")

    def test_pause_resume_seek_heartbeat_do_not_create_analysis_job(self):
        calls = []
        with mock.patch.object(music_server.analysis, "ensure_analysis", side_effect=lambda *args, **kwargs: calls.append(args)):
            status, payload = self.play()
            self.assertEqual(status, 200)
            session = payload["listening"]["session_id"]
            for action in ("pause", "resume", "seek", "heartbeat", "stop"):
                body = json.dumps({
                    "action": action,
                    "playback_owner": "owner-web-1",
                    "session_id": session,
                    "position_ms": 10,
                }).encode()
                code, _ = self.request("/v1/listening", method="POST", body=body)
                self.assertEqual(code, 200, action)
            time.sleep(0.05)
        self.assertEqual(len(calls), 1)

    def test_play_next_previous_enqueue_analysis_with_cdn_source(self):
        calls = []

        def capture(track_id, url, **kwargs):
            calls.append((track_id, url))
            return {"status": "queued"}

        with mock.patch.object(music_server.analysis, "ensure_analysis", side_effect=capture):
            status, payload = self.play()
            self.assertEqual(status, 200)
            session = payload["listening"]["session_id"]
            for action in ("next", "previous"):
                body = json.dumps({
                    "action": action,
                    "playback_owner": "owner-web-1",
                    "session_id": session,
                }).encode()
                code, _ = self.request("/v1/listening", method="POST", body=body)
                self.assertEqual(code, 200, action)
            deadline = time.time() + 1.5
            while len(calls) < 3 and time.time() < deadline:
                time.sleep(0.01)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[0][0], 11)
        self.assertTrue(all(url.startswith("https://") for _track, url in calls))
        self.assertTrue(all("music.126.net" in url for _track, url in calls))
        self.assertTrue(all("service-secret" not in url for _track, url in calls))
        self.assertTrue(all("/v1/audio/" not in url for _track, url in calls))

    def test_analysis_endpoint_requires_auth(self):
        self.assertEqual(self.request("/v1/songs/11/analysis", token=None)[0], 401)

    def test_analysis_endpoint_maps_track_and_observer_view(self):
        TingguHandler.body = {
            "key": "netease:11",
            "status": "done",
            "version": "shallow-0.4.0",
            "cache_hit": True,
            "analysis": {
                "bpm": 96.0,
                "bpm_note": "估算值",
                "global_key": {"key": "A minor", "confidence": 0.8, "is_confident": True},
                "energy_segments": [{"index": 1, "mean": 0.2}],
                "peak_energy_time": 12,
                "burst_time": 18,
                "burst_note": "持续性抬升",
                "dynamic_range_db": 11.0,
                "brightness_hz": 2000,
                "brightness_desc": "中等亮度",
                "shape": "整体平稳",
                "report_zh": "浅听报告",
                "key_progression": [{"time_sec": 1, "key": "A minor"}],
                "structure_timeline": [{"time_sec": 8, "change_type": "lift"}],
                "url": "https://cdn.example/secret.mp3",
            },
        }
        with mock.patch.object(analysis_mod, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            analysis_mod, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ), mock.patch.object(music_server.analysis, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            music_server.analysis, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ):
            status, payload = self.request("/v1/songs/11/analysis")
        self.assertEqual(status, 200)
        self.assertEqual(payload["track_id"], 11)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["analysis"]["bpm"], 96.0)
        self.assertNotIn("key_progression", payload["analysis"])
        self.assertNotIn("structure_timeline", payload["analysis"])
        self.assertNotIn("url", payload["analysis"])
        self.assertNotIn("energy", payload["analysis"])
        self.assertEqual(TingguHandler.requests[-1][0], "GET")
        self.assertIn("key=netease:11", TingguHandler.requests[-1][1])
        self.assertEqual(TingguHandler.requests[-1][2], "Bearer bridge-secret")
        dumped = json.dumps(payload)
        self.assertNotIn("bridge-secret", dumped)
        self.assertNotIn("service-secret", dumped)
        self.assertNotIn("cdn.example", dumped)

    def test_pending_and_error_mapping(self):
        for status_name in ("queued", "running", "error", "missing"):
            TingguHandler.body = {"key": "netease:11", "status": status_name, "version": "shallow-0.4.0"}
            view = analysis_mod.observer_view(TingguHandler.body, 11)
            self.assertEqual(view["status"], status_name)
            self.assertEqual(view["track_id"], 11)
            self.assertIsNone(view["analysis"])
            self.assertNotIn("energy", view)

    def test_analysis_http_pending_and_transport_failure(self):
        TingguHandler.body = {"key": "netease:11", "status": "queued", "version": "shallow-0.4.0"}
        with mock.patch.object(analysis_mod, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            analysis_mod, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ), mock.patch.object(music_server.analysis, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            music_server.analysis, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ):
            status, payload = self.request("/v1/songs/11/analysis")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "queued")
            self.assertIsNone(payload["analysis"])
            TingguHandler.status = 500
            code, _ = self.request("/v1/songs/11/analysis")
            self.assertIn(code, {502, 503})
        TingguHandler.status = 200

    def test_listening_survives_unpatched_tinggu_http_500(self):
        TingguHandler.status = 500
        with mock.patch.object(analysis_mod, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            analysis_mod, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ), mock.patch.object(music_server.analysis, "TINGGU_BASE_URL", self.tinggu_base), mock.patch.object(
            music_server.analysis, "TINGGU_BRIDGE_TOKEN", "bridge-secret"
        ):
            status, payload = self.play()
        TingguHandler.status = 200
        self.assertEqual(status, 200)
        self.assertEqual(payload["listening"]["status"], "playing")

    def test_ensure_fail_open_and_key_identity(self):
        self.assertEqual(analysis_mod.analysis_key(11), "netease:11")
        result = analysis_mod.ensure_analysis(
            11,
            "https://cdn.example/a.mp3",
            opener=lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("down")),
            base_url=self.tinggu_base,
            token="bridge-secret",
        )
        self.assertIsNone(result)

    def test_signed_play_route_still_returns_backend_redirect_url(self):
        status, payload = self.request("/v1/songs/11/play")
        self.assertEqual(status, 200)
        self.assertIn("/v1/audio/", payload["url"])
        self.assertNotIn("MUSIC_U", json.dumps(payload))
        self.assertNotIn("service-secret", payload["url"])


if __name__ == "__main__":
    unittest.main()
