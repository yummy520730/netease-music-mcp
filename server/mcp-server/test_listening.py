import json
import tempfile
import unittest
from pathlib import Path

from listening import ListeningError, ListeningStore, lyric_window, parse_lrc


def track(song_id=11, name="歌"):
    return {
        "id": song_id,
        "name": name,
        "artists": ["歌手"],
        "album": "专辑",
        "cover_url": "https://img.example/cover.jpg",
        "duration_ms": 180000,
    }


class LyricTests(unittest.TestCase):
    def test_parse_and_window(self):
        lines = parse_lrc("[00:00.00]前\n[00:10.50]现\n[00:20.00]后\n[00:30.00]更后")
        window = lyric_window(lines, 10500, before=1, after=2)
        self.assertEqual(window["current"]["text"], "现")
        self.assertEqual([item["text"] for item in window["before"]], ["前"])
        self.assertEqual([item["text"] for item in window["after"]], ["后", "更后"])


class ListeningStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ListeningStore(Path(self.dir.name) / "session.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_play_pause_seek_and_companion_shape(self):
        played = self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(),
            "queue": [track(), track(12, "下一首")],
            "queue_index": 0,
            "source": {"type": "search", "label": "青山"},
        }, lyrics={"lyric": "[00:00.00]秘密歌词", "lines": parse_lrc("[00:00.00]秘密歌词")})
        listening = played["listening"]
        self.assertEqual(listening["status"], "playing")
        self.assertEqual(listening["track_id"], 11)
        self.assertNotIn("lyric", listening)
        self.assertNotIn("url", listening)
        snapshot = self.store.snapshot()["listening"]
        self.assertEqual(snapshot["track_name"], "歌")
        paused = self.store.apply({
            "action": "pause",
            "playback_owner": "owner-web-1",
            "session_id": listening["session_id"],
            "position_ms": 1234,
        })
        self.assertEqual(paused["listening"]["status"], "paused")
        self.assertEqual(paused["listening"]["position_ms"], 1234)
        seeked = self.store.apply({
            "action": "seek",
            "playback_owner": "owner-web-1",
            "session_id": listening["session_id"],
            "position_ms": 90000,
        })
        self.assertEqual(seeked["listening"]["position_ms"], 90000)

    def test_stale_heartbeat_cannot_overwrite_new_track(self):
        first = self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(),
        })["listening"]
        second = self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(99, "新歌"),
        })["listening"]
        self.assertEqual(second["track_id"], 99)
        with self.assertRaises(ListeningError) as error:
            self.store.apply({
                "action": "heartbeat",
                "playback_owner": "owner-web-1",
                "session_id": first["session_id"],
                "position_ms": 50,
            })
        self.assertEqual(error.exception.status, 409)

    def test_fresh_owner_conflict_and_stale_takeover(self):
        first = self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(),
        })["listening"]
        with self.assertRaises(ListeningError) as error:
            self.store.apply({
                "action": "play",
                "playback_owner": "owner-phone-2",
                "track": track(12, "抢歌"),
            })
        self.assertEqual(error.exception.status, 409)
        self.store._session["last_heartbeat_at_ms"] = 1
        takeover = self.store.apply({
            "action": "play",
            "playback_owner": "owner-phone-2",
            "track": track(12, "抢歌"),
        })["listening"]
        self.assertEqual(takeover["playback_owner"], "owner-phone-2")
        self.assertEqual(takeover["track_id"], 12)
        self.assertNotEqual(takeover["session_id"], first["session_id"])

    def test_next_uses_queue_and_persists(self):
        played = self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(),
            "queue": [track(), track(12, "下一首")],
            "queue_index": 0,
        })["listening"]
        nxt = self.store.apply({
            "action": "next",
            "playback_owner": "owner-web-1",
            "session_id": played["session_id"],
        })["listening"]
        self.assertEqual(nxt["track_id"], 12)
        self.assertEqual(nxt["queue_index"], 1)
        restored = ListeningStore(self.store._path).snapshot()["listening"]
        self.assertEqual(restored["track_id"], 12)

    def test_session_omits_play_url_and_full_lyric(self):
        self.store.apply({
            "action": "play",
            "playback_owner": "owner-web-1",
            "track": track(),
        }, lyrics={"lyric": "FULL_LYRIC_SENTINEL", "translated_lyric": "TRANS", "lines": parse_lrc("[00:00.00]窗")})
        dumped = json.dumps(self.store.snapshot(include_lyric=True), ensure_ascii=False)
        self.assertNotIn("FULL_LYRIC_SENTINEL", dumped)
        self.assertIn("窗", dumped)


if __name__ == "__main__":
    unittest.main()
