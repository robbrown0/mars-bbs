from __future__ import annotations

import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from marsv2.musicd import Music, MusicHTTP, _player_html
from marsv2.rpc import RPCError


IDENTITY = {
    "account_id": 7,
    "userid": "listener",
    "display_name": "Listener",
    "runtime_uid": 29901,
    "roles": ["Music"],
    "capabilities": ["music.play"],
    "is_admin": False,
    "password_change_required": False,
}


class MusicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.nas01 = root / "nas01"
        self.unr01 = root / "unr01"
        self.extra = root / "extra"
        (self.nas01 / "Artist One" / "Album One").mkdir(parents=True)
        (self.unr01 / "Local Artist" / "Local Album").mkdir(parents=True)
        self.extra.mkdir()
        self.payload = b"ID3mars-audio-fixture"
        (self.nas01 / "Artist One" / "Album One" / "Track One.mp3").write_bytes(
            self.payload)
        (self.nas01 / "Artist One" / "Album One" / "Track Two.m4a").write_bytes(
            b"m4a-fixture")
        (self.unr01 / "Local Artist" / "Local Album" / "Local.flac").write_bytes(
            b"flac-fixture")
        (self.nas01 / "ignore.txt").write_text("not music", encoding="utf-8")
        recycle = self.nas01 / "#recycle"
        recycle.mkdir()
        (recycle / "deleted.mp3").write_bytes(b"deleted")
        os.symlink(self.nas01 / "Artist One", self.nas01 / "linked-artist")
        self.playlists_file = root / "playlists.json"
        self.playlists_file.write_text(json.dumps([{
            "slug": "test-favorites", "name": "Test Favorites",
            "description": "Only verified fixture tracks.",
            "tracks": [
                {"artist": "Artist One", "title": "Track Two"},
                {"artist": "Artist One", "title": "Track One"},
            ],
        }]), encoding="utf-8")
        self.music = Music(
            str(root / "music.db"), "/unused/session.sock",
            {"nas01": str(self.nas01), "unr01": str(self.unr01)},
            "https://bbs.example.test/marsamp",
            str(self.extra), str(self.playlists_file),
        )
        result = self.music.scan()
        self.assertEqual(result["tracks"], 3)

    def tearDown(self) -> None:
        self.music.db.close()
        self.temporary.cleanup()

    @staticmethod
    def request(op: str, data: dict[str, object] | None = None) -> dict[str, object]:
        return {"op": op, "data": data or {}, "token": "s" * 48}

    def redeem(self, url: str) -> str:
        ticket = url.rsplit("/", 1)[1]
        with patch("marsv2.musicd.request", return_value={
                "ok": True, "data": {"token": "m" * 48}}):
            return self.music.redeem_pair(ticket)

    def test_catalog_hides_paths_and_enforces_capability(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            result = self.music.handle(self.request("search", {"query": "Track",
                                                                "limit": 10,
                                                                "offset": 0}),
                                       (1, 29901, 29901))
        self.assertEqual(result["total"], 2)
        rendered = repr(result)
        self.assertNotIn(self.temporary.name, rendered)
        self.assertNotIn("relative_path", rendered)
        denied = dict(IDENTITY, capabilities=[])
        with patch("marsv2.musicd.validate", return_value=denied):
            with self.assertRaisesRegex(RPCError, "music access denied"):
                self.music.handle(self.request("search", {"query": "", "limit": 10,
                                                           "offset": 0}),
                                  (1, 29901, 29901))

    def test_pair_ticket_is_single_use_and_session_bound(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            paired = self.music.handle(self.request("pair"), (1, 29901, 29901))
        self.assertTrue(str(paired["url"]).startswith(
            "https://bbs.example.test/marsamp/pair/"))
        self.assertNotIn(self.temporary.name, str(paired))
        ticket = str(paired["url"]).rsplit("/", 1)[1]
        with patch("marsv2.musicd.request", return_value={
                "ok": True, "data": {"token": "m" * 48}}) as delegate:
            browser = self.music.redeem_pair(ticket)
        delegate.assert_called_once_with(
            "/unused/session.sock", "create_music_browser", {}, "s" * 48)
        with self.assertRaisesRegex(RPCError, "invalid or expired"):
            self.music.redeem_pair(ticket)
        with patch("marsv2.musicd.request",
                   return_value={"ok": True, "data": IDENTITY}) as touch:
            identity = self.music.browser_identity(browser)
        self.assertEqual(identity["account_id"], 7)
        touch.assert_called_once_with(
            "/unused/session.sock", "touch", token="m" * 48)

    def test_play_offers_pairing_until_browser_is_actively_polling(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            track_id = int(self.music.handle(
                self.request("search", {"query": "Track One", "limit": 10, "offset": 0}),
                (1, 29901, 29901))["tracks"][0]["id"])
            first = self.music.handle(
                self.request("play", {"id": track_id}), (1, 29901, 29901))
        self.assertEqual(first["action"], "play")
        self.assertIn("pair_url", first)
        browser = self.redeem(str(first["pair_url"]))
        with patch("marsv2.musicd.request",
                   return_value={"ok": True, "data": IDENTITY}):
            self.music.browser_identity(browser)
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            paired = self.music.handle(
                self.request("play", {"id": track_id}), (1, 29901, 29901))
        self.assertNotIn("pair_url", paired)

    def test_player_attempts_autoplay_and_makes_blocked_action_obvious(self) -> None:
        page = _player_html("/marsamp").decode("utf-8")
        self.assertIn("autoplay playsinline", page)
        self.assertIn("PLAY SELECTED SONG", page)
        self.assertIn("await audio.play()", page)
        self.assertIn("Your browser blocked automatic audio", page)
        self.assertIn("classList.add('attention')", page)
        self.assertIn("/api/advance", page)
        self.assertIn("/api/previous", page)
        self.assertIn('id="previous"', page)
        self.assertIn('id="next"', page)
        self.assertIn("audio.addEventListener('ended'", page)

    def test_playback_list_advances_and_wraps(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            rows = self.music.handle(self.request(
                "search", {"query": "", "limit": 10, "offset": 0}),
                (1, 29901, 29901))["tracks"]
            queue = [int(row["id"]) for row in rows]
            state = self.music.handle(self.request("play", {
                "id": queue[-1], "queue": queue, "mode": "sequential",
            }), (1, 29901, 29901))
        self.assertEqual(state["mode"], "sequential")
        self.assertNotIn("queue", state)
        advanced = self.music.advance(IDENTITY["account_id"])
        self.assertEqual(advanced["track_id"], queue[0])
        self.music.set_mode(IDENTITY["account_id"], "shuffle")
        first = self.music.advance(IDENTITY["account_id"])
        second = self.music.advance(IDENTITY["account_id"])
        self.assertNotEqual(first["track_id"], queue[0])
        self.assertNotEqual(second["track_id"], first["track_id"])
        self.assertEqual({first["track_id"], second["track_id"]}, set(queue[1:]))
        self.assertEqual(second["mode"], "shuffle")
        previous = self.music.previous(IDENTITY["account_id"])
        self.assertEqual(previous["track_id"], first["track_id"])

    def test_curated_playlists_resolve_only_available_catalog_tracks(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            playlists = self.music.handle(
                self.request("playlists"), (1, 29901, 29901))["playlists"]
            selected = self.music.handle(self.request("playlist_tracks", {
                "slug": "test-favorites", "offset": 0, "limit": 10,
            }), (1, 29901, 29901))
        self.assertEqual(playlists[0]["name"], "Test Favorites")
        self.assertEqual(playlists[0]["tracks"], 2)
        self.assertEqual([row["title"] for row in selected["tracks"]],
                         ["Track Two", "Track One"])
        self.assertNotIn("relative_path", repr(selected))

    def test_shipped_playlist_manifest_has_ten_reviewed_lists(self) -> None:
        filename = Path(__file__).resolve().parents[1] / "assets" / "music-playlists.json"
        playlists = Music._load_playlists(str(filename))
        self.assertEqual(len(playlists), 10)
        sizes = {playlist["slug"]: len(playlist["tracks"]) for playlist in playlists}
        self.assertEqual(sizes["eighties-essentials"], 100)
        self.assertTrue(all(size == 35 for slug, size in sizes.items()
                            if slug != "eighties-essentials"))
        self.assertEqual(len({playlist["slug"] for playlist in playlists}), 10)
        eighties = next(playlist for playlist in playlists
                         if playlist["slug"] == "eighties-essentials")
        self.assertEqual(len({track["title"].casefold()
                              for track in eighties["tracks"]}), 100)

    def test_advance_rpc_uses_shuffle_queue(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY), \
                patch.object(self.music, "browser_active", return_value=True):
            rows = self.music.handle(self.request(
                "search", {"query": "", "limit": 10, "offset": 0}),
                (1, 29901, 29901))["tracks"]
            queue = [int(row["id"]) for row in rows]
            self.music.handle(self.request("play", {
                "id": queue[0], "queue": queue, "mode": "shuffle",
            }), (1, 29901, 29901))
            first = self.music.handle(self.request("advance"), (1, 29901, 29901))
            second = self.music.handle(self.request("advance"), (1, 29901, 29901))
        self.assertNotEqual(first["track_id"], queue[0])
        self.assertNotEqual(second["track_id"], first["track_id"])

    def test_admin_adds_and_removes_only_approved_extra_sources(self) -> None:
        added = self.extra / "third-library" / "New Artist" / "New Album"
        added.mkdir(parents=True)
        (added / "New Track.mp3").write_bytes(b"ID3new")
        manager = dict(IDENTITY, capabilities=["music.play", "music.manage"])
        with patch("marsv2.musicd.validate", return_value=manager), \
                patch.object(self.music, "start_scan", return_value={"started": True}):
            result = self.music.handle(self.request("admin_add_source", {
                "name": "third", "location": "third-library"}), (1, 29901, 29901))
            self.assertTrue(result["added"])
            sources = self.music.handle(
                self.request("admin_sources"), (1, 29901, 29901))["sources"]
        self.assertEqual([row["name"] for row in sources], ["nas01", "unr01", "third"])
        self.assertNotIn(str(self.extra), repr(sources))
        scanned = self.music.scan()
        self.assertEqual(scanned["tracks"], 4)
        found = self.music._search("New Track", 0, 10)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["tracks"][0]["source"], "third")
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            with self.assertRaisesRegex(RPCError, "music access denied"):
                self.music.handle(self.request("admin_sources"), (1, 29901, 29901))
        with patch("marsv2.musicd.validate", return_value=manager):
            with self.assertRaisesRegex(RPCError, "relative directory"):
                self.music.handle(self.request("admin_add_source", {
                    "name": "escape", "location": "../nas01"}), (1, 29901, 29901))
            removed = self.music.handle(self.request("admin_remove_source", {
                "name": "third"}), (1, 29901, 29901))
            self.assertTrue(removed["removed"])
            with self.assertRaisesRegex(RPCError, "base sources cannot be removed"):
                self.music.handle(self.request("admin_remove_source", {
                    "name": "nas01"}), (1, 29901, 29901))

    def test_browser_range_stream_uses_opaque_track_id(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            rows = self.music.handle(self.request("search", {"query": "Track One",
                                                              "limit": 10,
                                                              "offset": 0}),
                                     (1, 29901, 29901))["tracks"]
            pair = self.music.handle(self.request("pair"), (1, 29901, 29901))
        track_id = int(rows[0]["id"])
        ticket = str(pair["url"]).rsplit("/", 1)[1]
        browser = self.redeem(str(pair["url"]))
        MusicHTTP.music = self.music
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MusicHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("marsv2.musicd.request",
                       return_value={"ok": True, "data": IDENTITY}):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                        timeout=3)
                connection.request(
                    "GET", f"/marsamp/audio/{track_id}",
                    headers={"Cookie": "marsamp_session=" + browser,
                             "Range": "bytes=3-6"})
                response = connection.getresponse()
                body = response.read()
                connection.close()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.getheader("Content-Range"),
                             f"bytes 3-6/{len(self.payload)}")
            self.assertEqual(body, self.payload[3:7])
        finally:
            server.shutdown()
            server.server_close()

    def test_browser_advances_selected_list_with_same_origin_post(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            rows = self.music.handle(self.request(
                "search", {"query": "Track", "limit": 10, "offset": 0}),
                (1, 29901, 29901))["tracks"]
            queue = [int(row["id"]) for row in rows]
            self.music.handle(self.request("play", {
                "id": queue[0], "queue": queue, "mode": "sequential",
            }), (1, 29901, 29901))
            pair = self.music.handle(self.request("pair"), (1, 29901, 29901))
        browser = self.redeem(str(pair["url"]))
        MusicHTTP.music = self.music
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MusicHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("marsv2.musicd.request",
                       return_value={"ok": True, "data": IDENTITY}):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/marsamp/api/advance", headers={
                    "Cookie": "marsamp_session=" + browser,
                    "Origin": "https://bbs.example.test",
                })
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(payload)["track_id"], queue[1])

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/marsamp/api/previous", headers={
                    "Cookie": "marsamp_session=" + browser,
                    "Origin": "https://bbs.example.test",
                })
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(payload)["track_id"], queue[0])

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/marsamp/api/advance", headers={
                    "Cookie": "marsamp_session=" + browser,
                    "Origin": "https://attacker.example",
                })
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 403)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
