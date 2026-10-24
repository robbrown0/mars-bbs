from __future__ import annotations

import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from marsv2.musicd import (
    Music, MusicHTTP, _display_album, _display_artist, _display_title, _player_html,
)
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
        self.cover = b"\xff\xd8\xff\xe0mars-cover-fixture"
        (self.nas01 / "Artist One" / "Album One" / "Track One.mp3").write_bytes(
            self.payload)
        (self.nas01 / "Artist One" / "Album One" / "Track Two.m4a").write_bytes(
            b"m4a-fixture")
        (self.nas01 / "Artist One" / "Album One" / "cover.jpg").write_bytes(
            self.cover)
        (self.unr01 / "Local Artist" / "Local Album" / "Local.flac").write_bytes(
            b"flac-fixture")
        (self.nas01 / "ignore.txt").write_text("not music", encoding="utf-8")
        recycle = self.nas01 / "#recycle"
        recycle.mkdir()
        (recycle / "deleted.mp3").write_bytes(b"deleted")
        os.symlink(self.nas01 / "Artist One", self.nas01 / "linked-artist")
        self.playlists_file = root / "playlists.json"
        self.playlists_file.write_text(json.dumps([
            {
                "slug": "test-favorites", "name": "Test Favorites",
                "description": "Only verified fixture tracks.",
                "tracks": [
                    {"artist": "Artist One", "title": "Track Two"},
                    {"artist": "Artist One", "title": "Track One"},
                ],
            },
            {
                "slug": "shuffle-everything", "name": "Shuffle Everything",
                "description": "Every available fixture track.", "dynamic": "all",
            },
        ]), encoding="utf-8")
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

    def test_track_numbers_are_hidden_only_in_presented_titles(self) -> None:
        self.assertEqual(_display_title("06 Hot For Teacher"), "Hot For Teacher")
        self.assertEqual(_display_title("02_-_Sweet_Child_O'_Mine"), "Sweet Child O' Mine")
        self.assertEqual(_display_title("1-06 - Panama"), "Panama")
        self.assertEqual(_display_title("16_dressed_up"), "dressed up")
        self.assertEqual(_display_title("1999"), "1999")
        self.assertEqual(_display_title("867-5309_Jenny"), "867-5309 Jenny")
        self.assertEqual(
            _display_title("Metallica - Master of Puppets - (02) - Master Of Puppets"),
            "Master Of Puppets",
        )
        self.assertEqual(
            _display_title("(disc_2)_05_-_Always_On_My_Mind"),
            "Always On My Mind",
        )
        with self.music.db:
            self.music.db.execute(
                "UPDATE tracks SET title=? WHERE title=?",
                ("06 Track One", "Track One"),
            )
        presented = self.music._search("Track One", 0, 10)["tracks"][0]
        self.assertEqual(presented["title"], "Track One")
        stored = self.music.db.execute(
            "SELECT title FROM tracks WHERE id=?", (presented["id"],)
        ).fetchone()[0]
        self.assertEqual(stored, "06 Track One")

    def test_path_fallback_metadata_is_cleaned_consistently(self) -> None:
        self.assertEqual(_display_title("- _Sweet_Child_O'_Mine"), "Sweet Child O' Mine")
        self.assertEqual(_display_artist("Guns_N'_Roses"), "Guns N' Roses")
        self.assertEqual(_display_album("Greatest_Hits_[Explicit]"), "Greatest Hits")

    def test_canonical_metadata_is_separate_and_invalidates_when_file_changes(self) -> None:
        raw = self.music.db.execute(
            "SELECT id,title,modified_ns FROM tracks WHERE title='Track One'"
        ).fetchone()
        with self.music.db:
            self.music.db.execute(
                "INSERT INTO track_metadata(track_id,title,artist,album,album_artist,"
                "track_number,disc_number,year,source_type,confidence,file_modified_ns,"
                "updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (raw["id"], "Canonical Song", "Canonical Artist", "Canonical Album", "",
                 "1", "1", "1984", "embedded", 90, raw["modified_ns"], 1, "test"),
            )
        found = self.music._search("Canonical Song", 0, 10)
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["tracks"][0]["artist"], "Canonical Artist")
        stored = self.music.db.execute(
            "SELECT title FROM tracks WHERE id=?", (raw["id"],)
        ).fetchone()[0]
        self.assertEqual(stored, "Track One")
        with self.music.db:
            self.music.db.execute(
                "UPDATE tracks SET modified_ns=modified_ns+1 WHERE id=?", (raw["id"],)
            )
        self.assertEqual(self.music._search("Canonical Song", 0, 10)["total"], 0)
        self.assertEqual(self.music._search("Track One", 0, 10)["total"], 1)

    def test_tag_refresh_accepts_real_names_and_rejects_placeholders(self) -> None:
        tagged = SimpleNamespace(
            title="Tagged Song", artist="Tagged Artist", album="Tagged Album",
            albumartist="Tagged Artist", track=1, disc=1, year="1984",
        )
        with patch("marsv2.musicd.TinyTag.get", return_value=tagged):
            result = self.music.refresh_metadata()
        self.assertEqual(result["updated"], 3)
        self.assertEqual(self.music._search("Tagged Song", 0, 10)["total"], 3)
        with self.music.db:
            self.music.db.execute("DELETE FROM track_metadata")
        placeholder = SimpleNamespace(
            title="(real_artist)-real_song", artist="artist", album="title",
            albumartist="", track=1, disc=1, year="",
        )
        with patch("marsv2.musicd.TinyTag.get", return_value=placeholder):
            result = self.music.refresh_metadata()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(self.music._search("real_song", 0, 10)["total"], 0)

    def test_admin_reviewed_metadata_is_confirmed_and_audited(self) -> None:
        manager = dict(IDENTITY, capabilities=["music.play", "music.manage"])
        track_id = int(self.music._search("Track One", 0, 10)["tracks"][0]["id"])
        with patch("marsv2.musicd.validate", return_value=manager):
            saved = self.music.handle(self.request("admin_metadata_update", {
                "track_id": track_id, "title": "Reviewed Title",
                "artist": "Reviewed Artist", "album": "Reviewed Album",
                "confirm": "SAVE",
            }), (1, 29901, 29901))
        self.assertEqual(saved["canonical"]["title"], "Reviewed Title")
        self.assertEqual(saved["canonical"]["metadata_source"], "reviewed")
        audit = self.music.db.execute(
            "SELECT action,changed_by FROM track_metadata_audit WHERE track_id=?",
            (track_id,),
        ).fetchone()
        self.assertEqual(tuple(audit), ("review", "listener"))
        with patch("marsv2.musicd.validate", return_value=manager):
            reset = self.music.handle(self.request("admin_metadata_reset", {
                "track_id": track_id, "confirm": "RESET",
            }), (1, 29901, 29901))
        self.assertEqual(reset["canonical"]["title"], "Track One")
        self.assertEqual(reset["canonical"]["metadata_source"], "path")
        self.assertEqual(self.music.db.execute(
            "SELECT group_concat(action,',') FROM track_metadata_audit WHERE track_id=? "
            "ORDER BY id", (track_id,),
        ).fetchone()[0], "review,reset")
        with patch("marsv2.musicd.validate", return_value=manager):
            with self.assertRaisesRegex(RPCError, "confirmation"):
                self.music.handle(self.request("admin_metadata_update", {
                    "track_id": track_id, "title": "Bad", "artist": "Bad",
                    "album": "", "confirm": "NO",
                }), (1, 29901, 29901))

    def test_playlist_match_uses_the_song_part_of_verbose_filenames(self) -> None:
        wanted = {"artists": ["Metallica"], "title": "Master of Puppets", "album": ""}
        battery = {"artist": "Metallica", "album": "Master of Puppets",
                   "title": "Metallica - Master of Puppets - (01) - Battery"}
        title_track = {"artist": "Metallica", "album": "Master of Puppets",
                       "title": "Metallica - Master of Puppets - (02) - Master Of Puppets"}
        self.assertEqual(Music._playlist_match(battery, wanted), -1)
        self.assertEqual(Music._playlist_match(title_track, wanted), 100)

    def test_playlist_match_retains_raw_identity_after_tag_normalization(self) -> None:
        wanted = {"artists": ["Original Artist"], "title": "Original Song",
                  "album": "Original Album"}
        normalized = {
            "artist": "Featured Performer", "title": "Canonical Recording",
            "album": "Compilation", "raw_artist": "Original Artist",
            "raw_title": "01 - Original Song", "raw_album": "Original Album",
        }
        self.assertEqual(Music._playlist_match(normalized, wanted), 130)

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
        self.assertIn('aria-label="Play">▶ PLAY', page)
        self.assertIn("await audio.play()", page)
        self.assertIn("Your browser blocked automatic audio", page)
        self.assertIn("classList.add('attention')", page)
        self.assertIn("function hideHint(){hint.hidden=true", page)
        self.assertNotIn("Audio enabled. MarsAmp will continue", page)
        self.assertIn("/api/advance", page)
        self.assertIn("/api/previous", page)
        self.assertIn('id="previous"', page)
        self.assertIn('id="next"', page)
        self.assertIn('id="queue-panel"', page)
        self.assertIn("/api/queue", page)
        self.assertIn("MediaMetadata", page)
        self.assertIn("/api/artwork/", page)
        self.assertIn("track.artwork_url", page)
        self.assertIn('id="cover"', page)
        self.assertIn("reader.readAsDataURL(blob)", page)
        self.assertIn("image.sizes='any'", page)
        self.assertIn("mediaArtworkData||ART512", page)
        self.assertIn("scheduleMetadataRefresh(key)", page)
        self.assertIn("artist+' · '+album", page)
        self.assertIn("audio.addEventListener('loadedmetadata'", page)
        self.assertIn("audio.addEventListener('playing',refreshMetadata)", page)
        self.assertIn("mediaSession.setActionHandler", page)
        self.assertIn("manifest.webmanifest", page)
        self.assertIn("audio.addEventListener('ended'", page)
        self.assertIn('id="output"', page)
        self.assertIn('class="cast-glyph"', page)
        self.assertIn('id="output-label"', page)
        self.assertIn('class="utility"', page)
        self.assertIn('.utility{display:grid;grid-template-columns:repeat(5,1fr)', page)
        self.assertIn('.utility-actions{grid-column:3/6', page)
        self.assertIn('>OUTPUT</span>', page)
        self.assertIn('id="mute"', page)
        self.assertIn('class="mute-glyph"', page)
        self.assertIn('>MUTE</span>', page)
        self.assertIn("audio.muted=!audio.muted", page)
        self.assertIn("localStorage.setItem('marsamp-muted'", page)
        self.assertIn("audio.addEventListener('volumechange',showMuteState)", page)
        self.assertIn('id="shuffle"', page)
        self.assertIn('class="shuffle-glyph"', page)
        self.assertIn('/api/mode/', page)
        self.assertIn("shuffle.setAttribute('aria-pressed'", page)
        self.assertIn('>SHUFFLE OFF</span>', page)
        self.assertIn("active?'SHUFFLE ON':'SHUFFLE OFF'", page)
        self.assertIn("navigate('/api/mode/'+next,'',true)", page)
        self.assertIn("audio.captureStream||audio.mozCaptureStream", page)
        self.assertIn("context.createAnalyser()", page)
        self.assertIn("getByteFrequencyData(spectrumData)", page)
        self.assertIn("requestAnimationFrame(drawSpectrum)", page)
        self.assertIn("/api/spectrum/", page)
        self.assertIn("X-MarsAmp-Interval-Ms", page)
        self.assertIn("X-MarsAmp-Complete", page)
        self.assertIn("audio.currentTime*1000/cachedSpectrum.interval", page)
        self.assertIn("requestCachedSpectrum(currentTrack,true)", page)
        self.assertNotIn("Date.now()/145", page)
        self.assertNotIn('Shuffle is on. The current cycle will not repeat songs.', page)
        self.assertNotIn('Sequential play is on.', page)
        self.assertNotIn('id="mode"', page)
        self.assertNotIn('PLAY MODE:', page)
        self.assertNotIn('class="output-copy"', page)
        self.assertNotIn('PLAY ON ANOTHER SPEAKER', page)
        self.assertNotIn('Keep this page open while controlling MarsAmp', page)
        self.assertIn('● SECURE</div>', page)
        self.assertNotIn('SECURE AUDIO LINK', page)
        self.assertIn('x-webkit-airplay="allow"', page)
        self.assertIn("webkitShowPlaybackTargetPicker", page)
        self.assertIn("audio.remote.prompt()", page)
        self.assertNotIn("Sonos appears here when it supports AirPlay", page)
        self.assertNotIn("Choose a Cast or remote playback device", page)
        self.assertIn("state.output_stream", page)

    def test_spectrum_is_bounded_cached_and_current_track_only(self) -> None:
        tracks = self.music._search("", 0, 10)["tracks"]
        track_id = int(tracks[0]["id"])
        other_id = int(tracks[1]["id"])
        self.music.command(IDENTITY["account_id"], "play", track_id)
        bands, source_bands, height, frames = 18, 24, 100, 240
        raw = bytearray(source_bands * height * frames)
        for frame in range(frames):
            for band in range(source_bands):
                active = min(height, 2 + frame * 4 + band)
                for row in range(height - active, height):
                    raw[frame * source_bands * height + row * source_bands + band] = 255
        completed = SimpleNamespace(returncode=0, stdout=bytes(raw), stderr=b"")
        with patch("marsv2.musicd.subprocess.run", return_value=completed) as run, \
                patch.object(self.music, "_store_spectrum",
                             wraps=self.music._store_spectrum) as store:
            self.music._build_spectrum(track_id)
        self.assertEqual(run.call_count, 2)
        preview_command = run.call_args_list[0].args[0]
        full_command = run.call_args_list[1].args[0]
        self.assertEqual(preview_command[0], "ffmpeg")
        self.assertIn("-protocol_whitelist", preview_command)
        self.assertIn("file,pipe", preview_command)
        self.assertIn("30", preview_command)
        self.assertIn("1800", full_command)
        self.assertTrue(any("showfreqs=s=24x100" in value
                            for value in preview_command))
        self.assertTrue(any("ascale=cbrt" in value for value in preview_command))
        self.assertNotIn("shell", run.call_args_list[0].kwargs)
        self.assertFalse(store.call_args_list[0].args[-1])
        self.assertTrue(store.call_args_list[1].args[-1])
        result = self.music.spectrum(IDENTITY["account_id"], track_id)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["bands"], bands)
        self.assertEqual(result["frames"], frames)
        self.assertEqual(result["interval_ms"], 125)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["data"]), bands * frames)
        self.assertGreater(max(result["data"]), min(result["data"]))
        self.assertNotIn(self.temporary.name, repr(result))
        with self.assertRaisesRegex(RPCError, "current song"):
            self.music.spectrum(IDENTITY["account_id"], other_id)

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

    def test_shipped_playlist_manifest_has_nineteen_curated_lists_and_dynamic_all(self) -> None:
        filename = Path(__file__).resolve().parents[1] / "assets" / "music-playlists.json"
        playlists = Music._load_playlists(str(filename))
        self.assertEqual(len(playlists), 20)
        self.assertEqual(playlists[0]["slug"], "eighties-essentials")
        sizes = {playlist["slug"]: len(playlist["tracks"]) for playlist in playlists}
        self.assertEqual(sizes["eighties-essentials"], 148)
        new_slugs = {
            "new-wave-neon", "arena-rock-anthems", "alternative-mixtape",
            "country-roadhouse", "women-of-mars", "southern-roots",
            "unplugged-sunday", "funk-disco-dancefloor", "after-midnight",
        }
        self.assertTrue(all(35 <= sizes[slug] <= 50 for slug in new_slugs))
        self.assertEqual(sizes["shuffle-everything"], 0)
        dynamic = next(playlist for playlist in playlists
                       if playlist["slug"] == "shuffle-everything")
        self.assertEqual(dynamic["dynamic"], "all")
        self.assertEqual(len({playlist["slug"] for playlist in playlists}), 20)
        eighties = next(playlist for playlist in playlists
                         if playlist["slug"] == "eighties-essentials")
        self.assertEqual(len({(track["artists"][0].casefold(), track["title"].casefold())
                              for track in eighties["tracks"]}), 148)

    def test_dynamic_all_playlist_uses_catalog_without_static_track_list(self) -> None:
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            playlists = self.music.handle(
                self.request("playlists"), (1, 29901, 29901))["playlists"]
            dynamic = next(row for row in playlists
                           if row["slug"] == "shuffle-everything")
            opened = self.music.handle(self.request("playlist_tracks", {
                "slug": "shuffle-everything", "offset": 0, "limit": 10,
            }), (1, 29901, 29901))
            first = opened["tracks"][0]
            state = self.music.handle(self.request("play", {
                "id": first["id"], "queue": [first["id"]],
                "mode": "sequential", "scope": "shuffle-everything",
            }), (1, 29901, 29901))
        self.assertEqual(dynamic["tracks"], 3)
        self.assertEqual(dynamic["dynamic"], "all")
        self.assertEqual(opened["total"], 3)
        self.assertEqual(opened["playlist"]["dynamic"], "all")
        self.assertEqual(state["mode"], "shuffle")
        advanced = self.music.advance(IDENTITY["account_id"])
        self.assertNotEqual(advanced["track_id"], first["id"])
        queue = self.music.queue_view(IDENTITY["account_id"])
        self.assertEqual(queue["dynamic"], "all")
        self.assertEqual(queue["total"], 3)
        self.assertNotIn("relative_path", repr(queue))

    def test_queue_view_names_an_exact_curated_playlist(self) -> None:
        opened = self.music.playlist_tracks("test-favorites", 0, 10)
        queue = [int(track["id"]) for track in opened["tracks"]]
        self.music.command(IDENTITY["account_id"], "play", queue[0],
                           queue, "sequential")
        view = self.music.queue_view(IDENTITY["account_id"])
        self.assertEqual(view["label"], "Test Favorites")

        self.music.command(IDENTITY["account_id"], "play", queue[0],
                           list(reversed(queue)), "sequential")
        view = self.music.queue_view(IDENTITY["account_id"])
        self.assertEqual(view["label"], "Current queue")

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

    def test_admin_confirms_and_removes_reviewed_album_artwork(self) -> None:
        manager = dict(IDENTITY, capabilities=["music.play", "music.manage"])
        track_id = int(self.music._search("Track One", 0, 10)["tracks"][0]["id"])
        mbid = "123e4567-e89b-42d3-a456-426614174000"
        imported = b"\x89PNG\r\n\x1a\nconfirmed-cover"
        candidates = [{
            "id": mbid, "title": "Album One", "artist": "Artist One",
            "date": "1984", "type": "Album", "score": 100,
        }]
        with patch("marsv2.musicd.validate", return_value=manager), \
                patch("marsv2.musicd.search_release_groups",
                      return_value=candidates) as search, \
                patch("marsv2.musicd.download_release_group_cover",
                      return_value=("image/png", imported)) as download:
            status = self.music.handle(self.request("admin_artwork_status", {
                "track_id": track_id}), (1, 29901, 29901))
            original_version = self.music.artwork_version(track_id)
            found = self.music.handle(self.request("admin_artwork_search", {
                "track_id": track_id, "artist": "Artist One", "album": "Album One",
            }), (1, 29901, 29901))
            saved = self.music.handle(self.request("admin_artwork_import", {
                "track_id": track_id, "mbid": mbid, "confirm": "IMPORT",
            }), (1, 29901, 29901))
        self.assertEqual(status["source"], "folder")
        self.assertEqual(status["bytes"], len(self.cover))
        self.assertEqual(found["candidates"], candidates)
        self.assertTrue(saved["saved"])
        self.assertEqual(self.music.artwork(track_id), ("image/png", imported, "confirmed"))
        self.assertNotEqual(self.music.artwork_version(track_id), original_version)
        self.assertEqual(self.music.artwork_version(track_id),
                         saved["sha256"][:16])
        search.assert_called_once_with(
            "Artist One", "Album One", self.music.artwork_user_agent)
        download.assert_called_once_with(mbid, self.music.artwork_user_agent)

        with patch("marsv2.musicd.validate", return_value=manager):
            stats = self.music.handle(self.request("stats"), (1, 29901, 29901))
            removed = self.music.handle(self.request("admin_artwork_remove", {
                "track_id": track_id, "confirm": "REMOVE",
            }), (1, 29901, 29901))
        self.assertEqual(stats["artwork_overrides"], 1)
        self.assertTrue(removed["removed"])
        self.assertEqual(self.music.artwork(track_id),
                         ("image/jpeg", self.cover, "folder"))

        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            with self.assertRaisesRegex(RPCError, "music access denied"):
                self.music.handle(self.request("admin_artwork_status", {
                    "track_id": track_id}), (1, 29901, 29901))

    def test_canonical_names_retain_legacy_confirmed_artwork_identity(self) -> None:
        row = self.music.db.execute(
            "SELECT id,modified_ns FROM tracks WHERE title='Track One'"
        ).fetchone()
        legacy_key = Music._artwork_key({"artist": "Artist One", "album": "Album One"})
        image = b"\x89PNG\r\n\x1a\nlegacy-cover"
        with self.music.db:
            self.music.db.execute(
                "INSERT INTO track_metadata(track_id,title,artist,album,album_artist,"
                "track_number,disc_number,year,source_type,confidence,file_modified_ns,"
                "updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], "Canonical Track", "Canonical Artist", "Canonical Album", "",
                 "", "", "", "embedded", 90, row["modified_ns"], 1, "test"),
            )
            self.music.db.execute(
                "INSERT INTO album_artwork(artwork_key,artist,album,provider,external_id,"
                "mime_type,image,image_sha256,created_at,created_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (legacy_key, "Artist One", "Album One", "test", "legacy", "image/png",
                 image, "a" * 64, 1, "test"),
            )
        self.assertEqual(self.music.artwork(int(row["id"])),
                         ("image/png", image, "confirmed"))
        removed = self.music.admin_artwork_remove(int(row["id"]))
        self.assertTrue(removed["removed"])

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

    def test_remote_output_stream_is_short_lived_track_capability(self) -> None:
        track_id = int(self.music._search("Track One", 0, 10)["tracks"][0]["id"])
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            self.music.handle(self.request("play", {"id": track_id}),
                              (1, 29901, 29901))
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
                connection.request("GET", "/marsamp/api/state", headers={
                    "Cookie": "marsamp_session=" + browser,
                })
                response = connection.getresponse()
                state = json.loads(response.read())
                connection.close()
            signed_url = str(state["output_stream"])
            self.assertNotIn(browser, signed_url)
            self.assertNotIn(self.temporary.name, signed_url)
            self.assertRegex(
                signed_url,
                r"^/marsamp/stream/[0-9]+/[0-9a-f]{16}/"
                r"[0-9]{9,12}/[0-9a-f]{64}$",
            )

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", signed_url, headers={"Range": "bytes=3-6"})
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.getheader("Cache-Control"),
                             "private, max-age=900")
            self.assertEqual(payload, self.payload[3:7])

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", f"/marsamp/audio/{track_id}")
            denied = connection.getresponse()
            denied.read()
            connection.close()
            self.assertEqual(denied.status, 401)

            tampered = signed_url[:-1] + ("0" if signed_url[-1] != "0" else "1")
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", tampered)
            rejected = connection.getresponse()
            rejected.read()
            connection.close()
            self.assertEqual(rejected.status, 403)

            wrong_track = signed_url.replace(
                f"/stream/{track_id}/", f"/stream/{track_id + 1}/")
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", wrong_track)
            rejected = connection.getresponse()
            rejected.read()
            connection.close()
            self.assertEqual(rejected.status, 403)

            expires = int(signed_url.rsplit("/", 2)[1])
            with patch("marsv2.musicd.time.time", return_value=expires + 1):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("HEAD", signed_url)
                rejected = connection.getresponse()
                rejected.read()
                connection.close()
            self.assertEqual(rejected.status, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_track_artwork_endpoint_is_authenticated_and_path_free(self) -> None:
        track_id = int(self.music._search("Track One", 0, 10)["tracks"][0]["id"])
        with patch("marsv2.musicd.validate", return_value=IDENTITY):
            self.music.handle(self.request("play", {"id": track_id}),
                              (1, 29901, 29901))
            pair = self.music.handle(self.request("pair"), (1, 29901, 29901))
        browser = self.redeem(str(pair["url"]))
        MusicHTTP.music = self.music
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MusicHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", f"/marsamp/api/artwork/{track_id}")
            denied = connection.getresponse()
            denied.read()
            connection.close()
            self.assertEqual(denied.status, 401)

            with patch("marsv2.musicd.request",
                       return_value={"ok": True, "data": IDENTITY}):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", "/marsamp/api/state", headers={
                    "Cookie": "marsamp_session=" + browser,
                })
                state_response = connection.getresponse()
                state = json.loads(state_response.read())
                connection.close()
                signed_url = str(state["track"]["artwork_url"])
                self.assertNotIn(browser, signed_url)
                self.assertRegex(signed_url,
                                 r"^/marsamp/artwork/[0-9]+/[0-9a-f]{16}/"
                                 r"[0-9]{9,12}/[0-9a-f]{64}$")

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", f"/marsamp/api/artwork/{track_id}", headers={
                    "Cookie": "marsamp_session=" + browser,
                })
                response = connection.getresponse()
                payload = response.read()
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
            self.assertEqual(payload, self.cover)
            self.assertNotIn(self.temporary.name.encode(), payload)

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", signed_url)
            signed_response = connection.getresponse()
            signed_payload = signed_response.read()
            connection.close()
            self.assertEqual(signed_response.status, 200)
            self.assertEqual(signed_response.getheader("Content-Type"), "image/jpeg")
            self.assertEqual(signed_response.getheader("Cache-Control"),
                             "private, max-age=3600, immutable")
            self.assertIn("img-src 'self' data:",
                          signed_response.getheader("Content-Security-Policy"))
            self.assertEqual(signed_payload, self.cover)

            tampered = signed_url[:-1] + ("0" if signed_url[-1] != "0" else "1")
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", tampered)
            rejected = connection.getresponse()
            rejected.read()
            connection.close()
            self.assertEqual(rejected.status, 403)

            wrong_track = signed_url.replace(
                f"/artwork/{track_id}/", f"/artwork/{track_id + 1}/")
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", wrong_track)
            rejected = connection.getresponse()
            rejected.read()
            connection.close()
            self.assertEqual(rejected.status, 403)

            expires = int(signed_url.rsplit("/", 2)[1])
            with patch("marsv2.musicd.time.time", return_value=expires + 1):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", signed_url)
                rejected = connection.getresponse()
                rejected.read()
                connection.close()
            self.assertEqual(rejected.status, 403)
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
                connection.request("GET", "/marsamp/api/queue", headers={
                    "Cookie": "marsamp_session=" + browser,
                })
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                queue_view = json.loads(payload)
                self.assertEqual(response.status, 200)
                self.assertEqual(queue_view["current"]["id"], queue[0])
                self.assertEqual(queue_view["up_next"][0]["id"], queue[1])
                self.assertNotIn("relative_path", repr(queue_view))

                for endpoint, expected in (("mode/shuffle", "shuffle"),
                                           ("mode/sequential", "sequential")):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=3)
                    connection.request("POST", f"/marsamp/api/{endpoint}", headers={
                        "Cookie": "marsamp_session=" + browser,
                        "Origin": "https://bbs.example.test",
                    })
                    response = connection.getresponse()
                    payload = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(payload)["mode"], expected)

                for endpoint, action in (("pause", "pause"), ("resume", "play")):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=3)
                    connection.request("POST", f"/marsamp/api/{endpoint}", headers={
                        "Cookie": "marsamp_session=" + browser,
                        "Origin": "https://bbs.example.test",
                    })
                    response = connection.getresponse()
                    payload = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(payload)["action"], action)

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

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/marsamp/api/stop", headers={
                    "Cookie": "marsamp_session=" + browser,
                    "Origin": "https://bbs.example.test",
                })
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(payload)["action"], "stop")
        finally:
            server.shutdown()
            server.server_close()

    def test_player_artwork_and_manifest_are_public_but_contain_no_account_data(self) -> None:
        MusicHTTP.music = self.music
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MusicHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, content_type in (("/marsamp/artwork.svg", "image/svg+xml"),
                                       ("/marsamp/artwork-192.png", "image/png"),
                                       ("/marsamp/artwork-512.png", "image/png"),
                                       ("/marsamp/manifest.webmanifest",
                                        "application/manifest+json")):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), content_type)
                self.assertNotIn(IDENTITY["userid"].encode(), payload)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
