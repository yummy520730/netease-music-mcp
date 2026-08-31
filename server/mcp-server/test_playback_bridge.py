import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


spec = importlib.util.spec_from_file_location("netease_server_bridge", Path(__file__).with_name("server.py"))
music_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(music_server)


class RedirectStubMusic:
    def play_source(self, song_id):
        return {
            "track_id": song_id,
            "url": "https://m801.music.126.net/source.mp3?token=x",
            "bitrate": 128000,
            "expire_seconds": 1200,
            "format": "mp3",
            "level": "standard",
            "source_kind": "v1",
            "transport": "eapi",
            "song": {"id": song_id, "name": "歌", "artists": ["歌手"], "album": "", "cover_url": "", "duration_ms": 1000, "liked": False},
        }

    def account(self): return {"user_id": 7, "nickname": "芥子"}
    def playlists(self, limit, offset): return {"playlists": [], "more": False, "limit": limit, "offset": offset}
    def playlist(self, playlist_id, limit, offset): return {"playlist": {"id": playlist_id}, "songs": [], "limit": limit, "offset": offset, "more": False}
    def history(self, limit, all_time): return {"songs": [], "period": "week", "limit": limit}
    def recommendations(self): return {"songs": []}
    def search(self, query, limit, offset): return {"songs": [], "song_count": 0, "limit": limit, "offset": offset}
    def song(self, song_id): return self.play_source(song_id)["song"]
    def lyric(self, song_id): return {"track_id": song_id, "lyric": None, "translated_lyric": None, "lines": []}


class Handler(music_server.MCPHandler):
    music = RedirectStubMusic()
    listening = music_server.ListeningStore(Path(tempfile.mkdtemp()) / "listening.json")
    service_token = "service-secret"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PlaybackBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = music_server.ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request_json(self, path, token=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read()), response.headers
        except urllib.error.HTTPError as error:
            body = error.read()
            return error.code, json.loads(body) if body else None, error.headers

    def test_aes_128_known_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected = "69c4e0d86a7b0430d8cdb78070b4c55a"
        self.assertEqual(music_server._aes_encrypt_block(plaintext, key).hex(), expected)

    def test_signed_audio_path_is_short_lived_and_tamper_evident(self):
        now = 1788160000
        path = music_server._signed_audio_path("secret", 11, now=now)
        self.assertEqual(music_server._verify_audio_path("secret", path, now=now + 1), 11)
        self.assertIsNone(music_server._verify_audio_path("wrong", path, now=now + 1))
        self.assertIsNone(music_server._verify_audio_path("secret", path, now=now + 121))

    def test_play_json_returns_backend_signed_url_not_cdn(self):
        status, payload, _ = self.request_json("/v1/songs/11/play", "service-secret")
        self.assertEqual(status, 200)
        self.assertTrue(payload["url"].startswith(self.base + "/v1/audio/11/"))
        self.assertNotIn("music.126.net", payload["url"])

    def test_signed_audio_get_redirects_without_referer_and_without_auth(self):
        status, payload, _ = self.request_json("/v1/songs/11/play", "service-secret")
        self.assertEqual(status, 200)
        path = urllib.parse.urlsplit(payload["url"]).path
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(self.base + path)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(request, timeout=2)
        response = raised.exception
        self.assertEqual(response.code, 302)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["Location"], "https://m801c.music.126.net/source.mp3?token=x")
        self.assertEqual(response.read(), b"")

    def test_invalid_signed_audio_path_fails_closed(self):
        status, payload, _ = self.request_json("/v1/audio/11/1788160120/00000000000000000000000000000000")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "invalid or expired audio link")


if __name__ == "__main__":
    unittest.main()
