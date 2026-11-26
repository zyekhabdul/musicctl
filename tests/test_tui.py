#!/usr/bin/env python3
import gc
import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
import warnings
import wave
from pathlib import Path

TUI_PATH = Path(__file__).resolve().parent.parent / "lib" / "tui.py"
COMMON_PATH = Path(__file__).resolve().parent.parent / "lib" / "common.py"
CATALOG_PATH = Path(__file__).resolve().parent.parent / "lib" / "catalog.py"
SPEC = importlib.util.spec_from_file_location("musicctl_tui", TUI_PATH)
tui = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = tui
SPEC.loader.exec_module(tui)

CAT_SPEC = importlib.util.spec_from_file_location("musicctl_catalog", CATALOG_PATH)
catalog = importlib.util.module_from_spec(CAT_SPEC)
assert CAT_SPEC and CAT_SPEC.loader
sys.modules[CAT_SPEC.name] = catalog
CAT_SPEC.loader.exec_module(catalog)

PROVIDERS_PATH = Path(__file__).resolve().parent.parent / "lib" / "providers.py"
DOWNLOADER_PATH = Path(__file__).resolve().parent.parent / "lib" / "downloader.py"
LIBRARY_PATH = Path(__file__).resolve().parent.parent / "lib" / "library.py"

PROV_SPEC = importlib.util.spec_from_file_location("musicctl_providers", PROVIDERS_PATH)
providers_mod = importlib.util.module_from_spec(PROV_SPEC)
assert PROV_SPEC and PROV_SPEC.loader
sys.modules[PROV_SPEC.name] = providers_mod
PROV_SPEC.loader.exec_module(providers_mod)

DL_SPEC = importlib.util.spec_from_file_location("musicctl_downloader", DOWNLOADER_PATH)
downloader_mod = importlib.util.module_from_spec(DL_SPEC)
assert DL_SPEC and DL_SPEC.loader
sys.modules[DL_SPEC.name] = downloader_mod
DL_SPEC.loader.exec_module(downloader_mod)

COMMON_SPEC = importlib.util.spec_from_file_location("musicctl_common", COMMON_PATH)
common_mod = importlib.util.module_from_spec(COMMON_SPEC)
assert COMMON_SPEC and COMMON_SPEC.loader
sys.modules[COMMON_SPEC.name] = common_mod
COMMON_SPEC.loader.exec_module(common_mod)

LIB_SPEC = importlib.util.spec_from_file_location("musicctl_library", LIBRARY_PATH)
library_mod = importlib.util.module_from_spec(LIB_SPEC)
assert LIB_SPEC and LIB_SPEC.loader
sys.modules[LIB_SPEC.name] = library_mod
LIB_SPEC.loader.exec_module(library_mod)


class TestTuiHelpers(unittest.TestCase):
    def test_format_speed(self):
        self.assertEqual(tui.format_speed(1.0), "")
        self.assertEqual(tui.format_speed("1.000000"), "")
        self.assertEqual(tui.format_speed(0.9999), "")
        self.assertEqual(tui.format_speed(1.5), "1.5x")
        self.assertEqual(tui.format_speed("1.250000"), "1.25x")
        self.assertEqual(tui.format_speed(2.0), "2x")
        self.assertEqual(tui.format_speed(None), "")

    def test_format_audio_badge(self):
        self.assertEqual(tui.format_audio_badge("flac", 96000, "s24p"), "FLAC 24-bit 96kHz")
        self.assertEqual(tui.format_audio_badge("flac", 44100, "s16"), "FLAC 16-bit 44.1kHz")
        self.assertEqual(tui.format_audio_badge("opus", 48000, "fltp"), "OPUS 48kHz")
        self.assertEqual(tui.format_audio_badge("aac", 44100, "fltp"), "AAC 44.1kHz")
        self.assertEqual(tui.format_audio_badge("", 0, ""), "")

    def test_parse_fallback_lyrics_json(self):
        raw = json.dumps({"lyrics": "Paroles de la chanson Demo par Band\r\nFirst line\nSecond line"})
        res = tui.parse_fallback_lyrics_json(raw, "Demo", "Band")
        self.assertIsNotNone(res)
        assert res is not None
        hdr, lines = res
        self.assertEqual(hdr, "Demo - Band [Fallback]")
        self.assertEqual([t for _, t in lines], ["First line", "Second line"])
        self.assertIsNone(tui.parse_fallback_lyrics_json('{"error":"No lyrics found"}', "Demo", "Band"))
        self.assertIsNone(tui.parse_fallback_lyrics_json("not json", "Demo", "Band"))

    def test_calc_seek_target(self):
        self.assertEqual(tui.calc_seek_target(0, 20, 120.0), 0.0)
        self.assertEqual(tui.calc_seek_target(19, 20, 120.0), 120.0)
        self.assertAlmostEqual(tui.calc_seek_target(9, 19, 100.0), 50.0, places=2)
        self.assertEqual(tui.calc_seek_target(-5, 20, 100.0), 0.0)
        self.assertEqual(tui.calc_seek_target(5, 20, 0.0), 0.0)

    def test_calc_vol_target(self):
        self.assertEqual(tui.calc_vol_target(0, 10), 0)
        self.assertEqual(tui.calc_vol_target(9, 10), 100)
        self.assertEqual(tui.calc_vol_target(9, 19), 50)
        self.assertEqual(tui.calc_vol_target(50, 10), 100)

    def test_unicode_width_and_truncate(self):
        self.assertEqual(tui.display_width("abc"), 3)
        self.assertEqual(tui.display_width("фруктовый"), 9)
        self.assertEqual(tui.truncate_width("фруктовый 2.0", 8), "фруктов…")
        self.assertEqual(tui.truncate_width("short", 10), "short")

    def test_hitbox(self):
        hb = tui.Hitbox(10, 2, 20, 4, "play", 1)
        self.assertTrue(hb.contains(10, 2))
        self.assertTrue(hb.contains(19, 3))
        self.assertFalse(hb.contains(20, 2))
        self.assertFalse(hb.contains(10, 4))

    def test_parse_ansi_art(self):
        sample = "\x1b[?25l\x1b[38;5;117;48;5;235m▀\x1b[0m▄\n"
        rows = tui.parse_ansi_art(sample)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], ("▀", 117, 235))
        self.assertEqual(rows[0][1], ("▄", -1, -1))

    def test_extract_primary_artist(self):
        self.assertEqual(
            tui.extract_primary_artist({"artist": "grentperez, Grant Perez, Eli Teplin", "uploader": "grentperez"}),
            "grentperez",
        )
        self.assertEqual(
            tui.extract_primary_artist({"uploader": "Drake - Topic"}),
            "Drake",
        )
        self.assertEqual(tui.extract_primary_artist(None), "")

    def test_pick_best_lyrics_candidate(self):
        candidates = [
            {
                "trackName": "Sailing",
                "artistName": "Rod Stewart",
                "duration": 278.0,
                "syncedLyrics": "[00:10.00] I am flying",
            },
            {
                "trackName": "sailing",
                "artistName": "grentperez",
                "duration": 244.0,
                "syncedLyrics": "[00:27.29] I can count all the times",
            },
        ]
        best = tui.pick_best_lyrics_candidate(candidates, "sailing", "grentperez", 244.2)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best["artistName"], "grentperez")

        # Unrelated artist with mismatched duration must be rejected instead of returning wrong lyrics
        wrong_only = [candidates[0]]
        self.assertIsNone(tui.pick_best_lyrics_candidate(wrong_only, "sailing", "grentperez", 244.2))

    def test_enrich_tsv_entry(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-hist-") as tmp:
            hp = Path(tmp) / "history.tsv"
            hp.write_text(
                "8ET3Sj5ctcw\t8ET3Sj5ctcw\tVideo ID\n"
                "IR7tYHdYN2Q\tTo the Bone\tPamungkas • 5:45\n",
                encoding="utf-8",
            )
            self.assertTrue(tui.enrich_tsv_entry(hp, "8ET3Sj5ctcw", "When We Were Younger", "grentperez • 4:01"))
            lines = hp.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "8ET3Sj5ctcw\tWhen We Were Younger\tgrentperez • 4:01")
            self.assertEqual(lines[1], "IR7tYHdYN2Q\tTo the Bone\tPamungkas • 5:45")
            # Already healed -> second call is a no-op
            self.assertFalse(tui.enrich_tsv_entry(hp, "8ET3Sj5ctcw", "Other", "Other"))

    def test_mpv_client_batch_get_live_isolated(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-py-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    "--force-window=no",
                    "--ao=null",
                    f"--input-ipc-server={sock}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if os.path.exists(sock):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(sock)
                            s.close()
                            break
                        except OSError:
                            s.close()
                    time.sleep(0.1)

                client = tui.MpvClient(sock)
                self.assertTrue(client.send(["set_property", "volume", 77]))
                res = client.batch_get(["idle-active", "volume", "speed"])
                self.assertIsNotNone(res)
                assert res is not None
                self.assertIs(res["idle-active"], True)
                self.assertEqual(int(res["volume"]), 77)
                self.assertEqual(tui.format_speed(res["speed"]), "")
                snap = client.batch_status_snapshot()
                self.assertIsNotNone(snap)
                assert snap is not None
                self.assertIs(snap["idle"], True)
                self.assertEqual(snap["volume"], 77)
                self.assertEqual(snap["speed_label"], "")
                client.send(["quit"])
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_common_cli_subcommands(self):
        proc_artist = subprocess.run(
            [sys.executable, str(COMMON_PATH), "primary-artist", "grentperez, Grant Perez", "grentperez"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertEqual(proc_artist.stdout.strip(), "grentperez")

        candidates = json.dumps([
            {"trackName": "sailing", "artistName": "grentperez", "duration": 244.0, "syncedLyrics": "[00:27.29] Hi"},
        ])
        proc_pick = subprocess.run(
            [sys.executable, str(COMMON_PATH), "pick-lyrics", "sailing", "grentperez", "244.0"],
            input=candidates,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        picked = json.loads(proc_pick.stdout)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["artistName"], "grentperez")

        proc_sync = subprocess.run(
            [sys.executable, str(COMMON_PATH), "parse-synced-lyrics"],
            input=candidates,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        synced = json.loads(proc_sync.stdout)
        self.assertEqual(synced["lines"], [{"t": 27.29, "text": "Hi"}])

        fb_in = json.dumps({"lyrics": "Paroles de la chanson Demo\nHello"})
        proc_fb = subprocess.run(
            [sys.executable, str(COMMON_PATH), "parse-fallback-lyrics", "Demo", "Artist"],
            input=fb_in,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("=== Demo - Artist [Fallback] ===", proc_fb.stdout)
        self.assertIn("Hello", proc_fb.stdout)

    @staticmethod
    def _make_headless_dashboard():
        dash = tui.SovereignDashboard.__new__(tui.SovereignDashboard)
        dash.ui_events = queue.SimpleQueue()
        dash.active_tab = "home"
        dash.search_loading = False
        dash.search_seq = 0
        dash.search_results = []
        dash.tracks = []
        dash.selected_idx = 0
        dash.scroll_top = 0
        dash.lyrics_track_key = ""
        dash.lyrics_header = ""
        dash.lyrics_lines = []
        dash.lyrics_loading = False
        dash.heal_inflight = set()
        dash.toast_msg = ""
        dash.toast_until = 0.0
        dash._poll_calls = 0
        dash._load_calls = 0
        dash.poll_mpv = lambda: setattr(dash, "_poll_calls", dash._poll_calls + 1)
        dash.load_tab_items = lambda: setattr(dash, "_load_calls", dash._load_calls + 1)
        return dash

    def test_drain_ui_events_lyrics_stale_guard(self):
        dash = self._make_headless_dashboard()
        dash.lyrics_track_key = "trackB"
        dash.lyrics_header = "Loading lyrics: Track B..."
        dash.lyrics_lines = []
        dash.lyrics_loading = True

        # Stale worker for trackA arrives late: must NOT overwrite trackB state or clear loading
        dash.ui_events.put(("lyrics_done", ("trackA", "Track A [LRCLIB]", [(1.0, "old")])))
        dash.drain_ui_events()
        self.assertTrue(dash.lyrics_loading)
        self.assertEqual(dash.lyrics_header, "Loading lyrics: Track B...")
        self.assertEqual(dash.lyrics_lines, [])

        # Current worker for trackB arrives: updates state and clears loading
        dash.ui_events.put(("lyrics_done", ("trackB", "Track B [LRCLIB]", [(2.0, "new")])))
        dash.drain_ui_events()
        self.assertFalse(dash.lyrics_loading)
        self.assertEqual(dash.lyrics_header, "Track B [LRCLIB]")
        self.assertEqual(dash.lyrics_lines, [(2.0, "new")])

    def test_drain_ui_events_search_seq_and_tab_isolation(self):
        dash = self._make_headless_dashboard()
        dash.active_tab = "search"
        dash.search_seq = 2
        dash.search_loading = True
        dash.selected_idx = 4
        dash.scroll_top = 2

        # Stale seq 1 ignored
        dash.ui_events.put(("search_done", (1, [("id1", "Old", "", "[YTM]")], True)))
        dash.drain_ui_events()
        self.assertTrue(dash.search_loading)
        self.assertEqual(dash.search_results, [])
        self.assertEqual(dash._load_calls, 0)

        # Current seq 2 on search tab resets selection and reloads
        rows = [("id2", "New", "", "[YTM]")]
        dash.ui_events.put(("search_done", (2, rows, True)))
        dash.drain_ui_events()
        self.assertFalse(dash.search_loading)
        self.assertEqual(dash.search_results, rows)
        self.assertEqual(dash.selected_idx, 0)
        self.assertEqual(dash.scroll_top, 0)
        self.assertEqual(dash._load_calls, 1)
        self.assertEqual(dash.toast_msg, "Found 1 tracks")

        # When user switched away from search tab, search_done stores results without resetting cursor/reloading
        dash.active_tab = "fav"
        dash.search_seq = 3
        dash.search_loading = True
        dash.selected_idx = 3
        dash.scroll_top = 1
        rows3 = [("id3", "Another", "", "[YTM]")]
        dash.ui_events.put(("search_done", (3, rows3, True)))
        dash.drain_ui_events()
        self.assertFalse(dash.search_loading)
        self.assertEqual(dash.search_results, rows3)
        self.assertEqual(dash.selected_idx, 3)
        self.assertEqual(dash.scroll_top, 1)
        self.assertEqual(dash._load_calls, 1)

    def test_drain_ui_events_cli_and_heal(self):
        dash = self._make_headless_dashboard()
        dash.ui_events.put(("cli_done", ("Queued: Song", True)))
        dash.drain_ui_events()
        self.assertEqual(dash.toast_msg, "Queued: Song")
        self.assertEqual(dash._poll_calls, 1)
        self.assertEqual(dash._load_calls, 1)

        dash.active_tab = "queue"
        dash.heal_inflight = {"vid1", "vid2"}
        dash.ui_events.put(("heal_done", (["vid1", "vid2"], True)))
        dash.drain_ui_events()
        self.assertEqual(dash.heal_inflight, set())
        self.assertEqual(dash._load_calls, 1)

        dash.active_tab = "hist"
        dash.heal_inflight = {"vid3"}
        dash.ui_events.put(("heal_done", (["vid3"], True)))
        dash.drain_ui_events()
        self.assertEqual(dash.heal_inflight, set())
        self.assertEqual(dash._load_calls, 2)

    def test_apply_search_key_utf8_and_controls(self):
        q = ""
        for ch in "東京 Café тест":
            q, act = tui.apply_search_key(q, ch)
            self.assertEqual(act, "stay")
        self.assertEqual(q, "東京 Café тест")

        q, act = tui.apply_search_key(q, 127)
        self.assertEqual((q, act), ("東京 Café тес", "stay"))
        q, act = tui.apply_search_key(q, "\x7f")
        self.assertEqual((q, act), ("東京 Café те", "stay"))
        q2, act2 = tui.apply_search_key(q, "\n")
        self.assertEqual((q2, act2), ("東京 Café те", "submit"))
        q3, act3 = tui.apply_search_key(q, 27)
        self.assertEqual((q3, act3), ("東京 Café те", "blur"))
        q4, act4 = tui.apply_search_key(q, "\x01")
        self.assertEqual((q4, act4), ("東京 Café те", "stay"))

    def test_reset_art_pairs_on_track_change(self):
        dash = self._make_headless_dashboard()
        dash.art_pairs = {(117, -1): 25, (235, 117): 26}
        dash.last_art_vid = "vidAAAAAAAA"
        dash.next_art_pair = 27

        self.assertFalse(dash.reset_art_pairs_for_vid("vidAAAAAAAA"))
        self.assertEqual(dash.next_art_pair, 27)
        self.assertEqual(len(dash.art_pairs), 2)

        self.assertTrue(dash.reset_art_pairs_for_vid("vidBBBBBBBB"))
        self.assertEqual(dash.last_art_vid, "vidBBBBBBBB")
        self.assertEqual(dash.art_pairs, {})
        self.assertEqual(dash.next_art_pair, tui.ART_PAIR_START)

    def test_mpv_client_batch_session_snapshot_live_isolated(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-sess-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            wav_path = os.path.join(tmp, "tone.wav")
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 16000)

            proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    "--force-window=no",
                    "--ao=null",
                    f"--input-ipc-server={sock}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if os.path.exists(sock):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(sock)
                            s.close()
                            break
                        except OSError:
                            s.close()
                    time.sleep(0.1)

                client = tui.MpvClient(sock, timeout=1.0)
                self.assertIsNone(client.batch_session_snapshot())

                self.assertTrue(client.send(["loadfile", wav_path, "replace"]))
                self.assertTrue(client.send(["set_property", "volume", 64]))
                self.assertTrue(client.send(["set_property", "loop-playlist", "inf"]))
                self.assertTrue(client.send(["set_property", "speed", 1.25]))

                snap = None
                for _ in range(30):
                    snap = client.batch_session_snapshot()
                    if snap is not None:
                        break
                    time.sleep(0.1)

                self.assertIsNotNone(snap)
                assert snap is not None
                self.assertEqual(snap["playlist"], [wav_path])
                self.assertEqual(snap["volume"], 64)
                self.assertEqual(snap["loop"], "all")
                self.assertEqual(snap["speed"], 1.25)
                self.assertIsInstance(snap["saved_at"], int)
                client.send(["quit"])
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_refresh_local_files_meta_stat_cache(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-meta-") as tmp:
            data_dir = Path(tmp)
            (data_dir / "favorites.tsv").write_text("id111111111\tSong One\tArtist\n", encoding="utf-8")
            (data_dir / "audio.state").write_text("bass:1\n", encoding="utf-8")
            (data_dir / "quality.state").write_text("hires\n", encoding="utf-8")

            dash = self._make_headless_dashboard()
            dash.data_dir = data_dir
            dash.fav_ids = set()
            dash.eq_mode = "flat"
            dash.norm_on = False
            dash.quality_mode = "cd"
            dash._file_sigs = {}

            calls = 0
            orig_read_tsv = dash.read_tsv

            def counted_read_tsv(path, tag):
                nonlocal calls
                calls += 1
                return orig_read_tsv(path, tag)

            dash.read_tsv = counted_read_tsv

            dash.refresh_local_files_meta()
            self.assertEqual(calls, 1)
            self.assertEqual(dash.fav_ids, {"id111111111"})
            self.assertEqual(dash.eq_mode, "bass")
            self.assertTrue(dash.norm_on)
            self.assertEqual(dash.quality_mode, "hires")

            # Unchanged files must not re-read favorites.tsv
            dash.refresh_local_files_meta()
            self.assertEqual(calls, 1)

            # Mutate favorites.tsv with distinct size/mtime -> reloads
            (data_dir / "favorites.tsv").write_text(
                "id111111111\tSong One\tArtist\nid222222222\tSong Two\tArtist\n",
                encoding="utf-8",
            )
            dash.refresh_local_files_meta()
            self.assertEqual(calls, 2)
            self.assertEqual(dash.fav_ids, {"id111111111", "id222222222"})


class TestCatalogEngine(unittest.TestCase):
    def test_parse_ref_and_format_ref(self):
        self.assertEqual(catalog.format_ref("deezer", "track", "12345"), "deezer:song:12345")
        self.assertEqual(catalog.parse_ref("ytm:song:IR7tYHdYN2Q"), ("ytm", "song", "IR7tYHdYN2Q"))
        self.assertEqual(catalog.parse_ref("deezer:track:12345"), ("deezer", "song", "12345"))
        self.assertEqual(catalog.parse_ref("itunes:album:999888"), ("itunes", "album", "999888"))
        self.assertEqual(catalog.parse_ref("MPREb_test123"), ("ytm", "album", "MPREb_test123"))
        self.assertEqual(catalog.parse_ref("OLAK5uy_abc12345"), ("ytm", "album", "OLAK5uy_abc12345"))
        self.assertEqual(catalog.parse_ref("UCtestartist123456"), ("ytm", "artist", "UCtestartist123456"))
        self.assertEqual(catalog.parse_ref("VLPLtest12345"), ("ytm", "playlist", "VLPLtest12345"))
        self.assertEqual(catalog.parse_ref("PLtest12345"), ("ytm", "playlist", "VLPLtest12345"))
        self.assertEqual(catalog.parse_ref("https://www.deezer.com/en/album/302127"), ("deezer", "album", "302127"))
        self.assertEqual(
            catalog.parse_ref("https://music.youtube.com/browse/MPREb_test123"),
            ("ytm", "album", "MPREb_test123"),
        )
        self.assertEqual(
            catalog.parse_ref("https://music.youtube.com/playlist?list=PLtest12345"),
            ("ytm", "playlist", "VLPLtest12345"),
        )
        self.assertEqual(
            catalog.parse_ref("https://music.youtube.com/channel/UCtestartist123456"),
            ("ytm", "artist", "UCtestartist123456"),
        )
        self.assertIsNone(catalog.parse_ref("just a search query"))

    def test_parse_ytm_search_all_types(self):
        songs_raw = {
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "IR7tYHdYN2Q"},
                                "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://img/s.jpg", "width": 120, "height": 120}]}}},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "To the Bone"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Pamungkas"}, {"text": " • "}, {"text": "Flying Solo"}, {"text": " • "}, {"text": "5:45"}]}}},
                                ],
                            }
                        },
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "IR7tYHdYN2Q"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Duplicate"}]}}},
                                ],
                            }
                        },
                    ]
                }
            }
        }
        songs = catalog.parse_ytm_search(songs_raw, "songs")
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["source"], "ytm")
        self.assertEqual(songs[0]["id"], "IR7tYHdYN2Q")
        self.assertEqual(songs[0]["type"], "song")
        self.assertEqual(songs[0]["artist"], "Pamungkas")
        self.assertEqual(songs[0]["album"], "Flying Solo")
        self.assertEqual(songs[0]["duration"], 345)
        self.assertEqual(songs[0]["cover_url"], "https://img/s.jpg")
        self.assertEqual(catalog.entity_to_tsv_row(songs[0]), "IR7tYHdYN2Q\tTo the Bone\tPamungkas • Flying Solo • 5:45")

        albums_raw = {
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "navigationEndpoint": {"browseEndpoint": {"browseId": "MPREb_test123"}},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Walk the Talk"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Album"}, {"text": " • "}, {"text": "Pamungkas"}, {"text": " • "}, {"text": "2018"}]}}},
                                ],
                            }
                        }
                    ]
                }
            }
        }
        albums = catalog.parse_ytm_search(albums_raw, "albums")
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["type"], "album")
        self.assertEqual(albums[0]["id"], "MPREb_test123")
        self.assertEqual(albums[0]["artist"], "Pamungkas")
        self.assertEqual(albums[0]["year"], 2018)
        self.assertTrue(catalog.entity_to_tsv_row(albums[0]).startswith("ytm:album:MPREb_test123\tWalk the Talk\t"))

        artists_raw = {
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "navigationEndpoint": {"browseEndpoint": {"browseId": "UCtestartist123456"}},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Pamungkas"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist"}, {"text": " • "}, {"text": "2.1M monthly listeners"}]}}},
                                ],
                            }
                        }
                    ]
                }
            }
        }
        artists = catalog.parse_ytm_search(artists_raw, "artists")
        self.assertEqual(artists[0]["type"], "artist")
        self.assertEqual(artists[0]["id"], "UCtestartist123456")

        playlists_raw = {
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "navigationEndpoint": {"browseEndpoint": {"browseId": "VLPLtest12345"}},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Indie Chill"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Playlist"}, {"text": " • "}, {"text": "Curator"}, {"text": " • "}, {"text": "40 songs"}]}}},
                                ],
                            }
                        }
                    ]
                }
            }
        }
        pls = catalog.parse_ytm_search(playlists_raw, "playlists")
        self.assertEqual(pls[0]["type"], "playlist")
        self.assertEqual(pls[0]["id"], "VLPLtest12345")
        self.assertEqual(pls[0]["artist"], "Curator")

    def test_parse_ytm_browse_album_artist_playlist(self):
        album_fixture = {
            "header": {
                "musicDetailHeaderRenderer": {
                    "title": {"runs": [{"text": "Walk the Talk"}]},
                    "subtitle": {"runs": [{"text": "Album"}, {"text": " • "}, {"text": "Pamungkas"}, {"text": " • "}, {"text": "2018"}]},
                    "thumbnail": {"croppedSquareThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://img/alb.jpg", "width": 544, "height": 544}]}}},
                }
            },
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "index": {"runs": [{"text": "1"}]},
                                "playlistItemData": {"videoId": "w_0RyTy-GlA"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Monolog"}]}}},
                                ],
                                "fixedColumns": [
                                    {"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:28"}]}}},
                                ],
                            }
                        }
                    ]
                }
            },
        }
        alb = catalog.parse_ytm_browse(album_fixture, "MPREb_test123")
        self.assertEqual(alb["entity"]["type"], "album")
        self.assertEqual(alb["entity"]["artist"], "Pamungkas")
        self.assertEqual(alb["entity"]["year"], 2018)
        self.assertEqual(len(alb["items"]), 1)
        trk = alb["items"][0]
        self.assertEqual(trk["album"], "Walk the Talk")
        self.assertEqual(trk["artist"], "Pamungkas")
        self.assertEqual(trk["track_num"], 1)
        self.assertEqual(trk["year"], 2018)
        self.assertEqual(trk["duration"], 208)
        self.assertEqual(trk["cover_url"], "https://img/alb.jpg")

        artist_fixture = {
            "header": {
                "musicImmersiveHeaderRenderer": {
                    "title": {"runs": [{"text": "Pamungkas"}]},
                    "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://img/art.jpg", "width": 720, "height": 720}]}}},
                }
            },
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "IR7tYHdYN2Q"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "To the Bone"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Pamungkas"}, {"text": " • "}, {"text": "5:45"}]}}},
                                ],
                            }
                        }
                    ]
                },
                "musicCarouselShelfRenderer": {
                    "contents": [
                        {
                            "musicTwoRowItemRenderer": {
                                "title": {"runs": [{"text": "Solipsism"}]},
                                "subtitle": {"runs": [{"text": "Album"}, {"text": " • "}, {"text": "2020"}]},
                                "navigationEndpoint": {"browseEndpoint": {"browseId": "MPREb_solipsism"}},
                                "thumbnailRenderer": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://img/sol.jpg", "width": 544, "height": 544}]}}},
                            }
                        }
                    ]
                },
            },
        }
        art = catalog.parse_ytm_browse(artist_fixture, "UCtestartist123456")
        self.assertEqual(art["entity"]["type"], "artist")
        self.assertEqual([x["type"] for x in art["items"]], ["song", "album"])
        self.assertEqual(art["items"][1]["id"], "MPREb_solipsism")
        self.assertEqual(art["items"][1]["year"], 2020)

        pl_fixture = {
            "header": {
                "musicResponsiveHeaderRenderer": {
                    "title": {"runs": [{"text": "Late Night"}]},
                    "subtitle": {"runs": [{"text": "Playlist"}, {"text": " • "}, {"text": "Aomi"}]},
                }
            },
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "abc12345678"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Track A"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Artist A"}, {"text": " • "}, {"text": "3:00"}]}}},
                                ],
                            }
                        }
                    ],
                    "continuations": [{"nextContinuationData": {"continuation": "CONT_TOK_1"}}],
                }
            },
        }
        pl = catalog.parse_ytm_browse(pl_fixture, "VLPLtest12345")
        self.assertEqual(pl["entity"]["type"], "playlist")
        self.assertEqual(pl["continuation"], "CONT_TOK_1")
        self.assertEqual(pl["items"][0]["id"], "abc12345678")

    def test_deezer_parsers_isrc_and_square_art(self):
        dz_search = {
            "data": [
                {
                    "id": 3135556,
                    "title": "Harder, Better, Faster, Stronger",
                    "isrc": "GBDUW0000059",
                    "duration": 224,
                    "track_position": 4,
                    "artist": {"id": 27, "name": "Daft Punk"},
                    "album": {
                        "id": 302127,
                        "title": "Discovery",
                        "release_date": "2001-03-12",
                        "cover_xl": "https://cdn-images.dzcdn.net/images/cover/2e018122cb56986277102d2041a592c8/1000x1000-000000-80-0-0.jpg",
                    },
                }
            ]
        }
        rows = catalog.parse_deezer_search(dz_search, "songs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "deezer")
        self.assertEqual(rows[0]["isrc"], "GBDUW0000059")
        self.assertEqual(rows[0]["album"], "Discovery")
        self.assertEqual(rows[0]["track_num"], 4)
        self.assertEqual(rows[0]["year"], 2001)
        self.assertIn("1000x1000", rows[0]["cover_url"])

        dz_artist = catalog.parse_deezer_artist(
            {"id": 27, "name": "Daft Punk", "picture_big": "https://cdn-images.dzcdn.net/images/artist/abc/500x500-000000-80-0-0.jpg"},
            top_data={"data": [{"id": 3135556, "title": "One More Time", "duration": 320, "artist": {"name": "Daft Punk"}, "album": {"title": "Discovery"}}]},
            albums_data={"data": [{"id": 302127, "title": "Discovery", "release_date": "2001-03-12", "cover_medium": "https://cdn-images.dzcdn.net/images/cover/xyz/250x250-000000-80-0-0.jpg"}]},
        )
        self.assertEqual(dz_artist["entity"]["type"], "artist")
        self.assertIn("1000x1000", dz_artist["entity"]["cover_url"])
        self.assertEqual([x["type"] for x in dz_artist["items"]], ["song", "album"])
        self.assertIn("1000x1000", dz_artist["items"][1]["cover_url"])

    def test_itunes_parsers_search_and_lookup(self):
        it_search = {
            "results": [
                {
                    "wrapperType": "track",
                    "trackId": 1440857781,
                    "trackName": "Get Lucky",
                    "artistName": "Daft Punk",
                    "collectionName": "Random Access Memories",
                    "trackNumber": 8,
                    "trackTimeMillis": 369626,
                    "releaseDate": "2013-05-17T07:00:00Z",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/100x100bb.jpg",
                }
            ]
        }
        rows = catalog.parse_itunes_search(it_search, "songs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "itunes")
        self.assertEqual(rows[0]["track_num"], 8)
        self.assertEqual(rows[0]["year"], 2013)
        self.assertEqual(rows[0]["duration"], 370)
        self.assertTrue(rows[0]["cover_url"].endswith("1000x1000bb.jpg"))
        self.assertEqual(catalog.parse_itunes_search(it_search, "playlists"), [])

        it_lookup = {
            "results": [
                {
                    "wrapperType": "collection",
                    "collectionId": 617154241,
                    "collectionName": "Random Access Memories",
                    "artistName": "Daft Punk",
                    "releaseDate": "2013-05-17T07:00:00Z",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/100x100bb.jpg",
                },
                {
                    "wrapperType": "track",
                    "trackId": 617154366,
                    "trackName": "Give Life Back to Music",
                    "artistName": "Daft Punk",
                    "collectionName": "Random Access Memories",
                    "trackNumber": 1,
                    "trackTimeMillis": 274000,
                    "releaseDate": "2013-05-17T07:00:00Z",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/100x100bb.jpg",
                },
            ]
        }
        b = catalog.parse_itunes_lookup(it_lookup, "album")
        self.assertEqual(b["entity"]["type"], "album")
        self.assertEqual(b["entity"]["id"], "617154241")
        self.assertEqual(len(b["items"]), 1)
        self.assertEqual(b["items"][0]["track_num"], 1)


class TestMetadataPropagation(unittest.TestCase):
    def test_normalize_track_title(self):
        self.assertEqual(tui.normalize_track_title("To the Bone (Official Video) [HD]"), "To the Bone")
        self.assertEqual(tui.normalize_track_title("Monolog (Official Lyric Video)"), "Monolog")
        self.assertEqual(tui.normalize_track_title("One Only | Official Music Video"), "One Only")
        self.assertEqual(tui.normalize_track_title("Sailing (Acoustic)"), "Sailing (Acoustic)")
        self.assertEqual(tui.normalize_track_title("Track (Live) [Official Audio]"), "Track (Live)")
        self.assertEqual(tui.normalize_track_title("Song (Remix) (feat. Guest)"), "Song (Remix) (feat. Guest)")
        self.assertEqual(tui.normalize_track_title("(Official Video)"), "(Official Video)")

    def test_extract_metadata_fields_and_tui_album_helpers(self):
        meta = {
            "ARTIST": "Pamungkas, Guest",
            "ALBUM": "Walk the Talk",
            "DATE": "2018-07-15",
            "TRACK": "4/12",
            "ISRC": "idabc1234567",
        }
        fields = tui.extract_metadata_fields(meta)
        self.assertEqual(fields["artist"], "Pamungkas")
        self.assertEqual(fields["album"], "Walk the Talk")
        self.assertEqual(fields["year"], 2018)
        self.assertEqual(fields["track_num"], 4)
        self.assertEqual(fields["isrc"], "IDABC1234567")

        self.assertEqual(tui.format_album_line("Walk the Talk", 2018), "Walk the Talk (2018)")
        self.assertEqual(tui.format_album_line("Walk the Talk", 0), "Walk the Talk")
        self.assertEqual(tui.format_album_line("", 2018), "")
        self.assertEqual(tui.extract_album_from_meta("Pamungkas • Flying Solo • 5:45"), "Flying Solo")
        self.assertEqual(tui.extract_album_from_meta("Pamungkas • 5:45"), "")
        self.assertEqual(tui.extract_album_from_meta("Album • Pamungkas • 2018"), "")

    def test_mpv_batch_status_snapshot_metadata_live_isolated(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-meta-mpv-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            flac_path = os.path.join(tmp, "tagged.flac")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=stereo",
                    "-t",
                    "5",
                    "-c:a",
                    "flac",
                    "-metadata",
                    "title=To the Bone",
                    "-metadata",
                    "artist=Pamungkas",
                    "-metadata",
                    "album=Flying Solo",
                    "-metadata",
                    "date=2019",
                    "-metadata",
                    "track=3",
                    "-metadata",
                    "isrc=ID1234567890",
                    flac_path,
                ],
                check=True,
            )

            proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    "--force-window=no",
                    "--ao=null",
                    f"--input-ipc-server={sock}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if os.path.exists(sock):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(sock)
                            s.close()
                            break
                        except OSError:
                            s.close()
                    time.sleep(0.1)

                client = tui.MpvClient(sock, timeout=1.0)
                self.assertTrue(client.send(["loadfile", flac_path, "replace"]))
                self.assertTrue(client.send(["set_property", "pause", True]))

                snap = None
                for _ in range(30):
                    cur = client.batch_status_snapshot()
                    if cur and not cur["idle"] and cur.get("album"):
                        snap = cur
                        break
                    time.sleep(0.1)

                self.assertIsNotNone(snap)
                assert snap is not None
                self.assertEqual(snap["artist"], "Pamungkas")
                self.assertEqual(snap["album"], "Flying Solo")
                self.assertEqual(snap["year"], 2019)
                self.assertEqual(snap["track_num"], 3)
                self.assertEqual(snap["isrc"], "ID1234567890")
                client.send(["quit"])
            finally:
                proc.terminate()
                proc.wait(timeout=3)


class TestAudiophileAndHooks(unittest.TestCase):
    def test_parse_sponsorblock_segments(self):
        raw = json.dumps([
            {"segment": [12.0, 25.0], "category": "music_offtopic"},
            {"segment": [25.05, 30.0], "category": "music_offtopic"},
            {"segment": [40.0, 40.3], "category": "music_offtopic"},
            {"segment": [50.0, 65.0], "category": "sponsor"},
            {"segment": [5.0, 8.0], "category": "music_offtopic"},
        ])
        segs = tui.MpvClient  # ensure common loaded
        import common

        parsed = common.parse_sponsorblock_segments(raw)
        self.assertEqual(
            parsed,
            [
                {"start": 5.0, "end": 8.0, "category": "music_offtopic"},
                {"start": 12.0, "end": 30.0, "category": "music_offtopic"},
            ],
        )
        self.assertEqual(common.parse_sponsorblock_segments("not-json"), [])
        self.assertEqual(common.parse_sponsorblock_segments("{}"), [])
        _ = segs

    def test_should_trigger_autoplay(self):
        import common

        self.assertFalse(common.should_trigger_autoplay(False, "off", 1, 1, "IR7tYHdYN2Q"))
        self.assertFalse(common.should_trigger_autoplay(True, "one", 1, 1, "IR7tYHdYN2Q"))
        self.assertFalse(common.should_trigger_autoplay(True, "all", 1, 1, "IR7tYHdYN2Q"))
        self.assertFalse(common.should_trigger_autoplay(True, "off", 1, 3, "IR7tYHdYN2Q"))
        self.assertFalse(common.should_trigger_autoplay(True, "off", 3, 3, "IR7tYHdYN2Q", "IR7tYHdYN2Q"))
        self.assertFalse(common.should_trigger_autoplay(True, "off", 3, 3, "localfile.flac"))
        self.assertTrue(common.should_trigger_autoplay(True, "off", 3, 3, "IR7tYHdYN2Q", "w_0RyTy-GlA"))

    def test_mpv_hooks_lua_loads_cleanly_in_isolated_mpv(self):
        lua_path = Path(__file__).resolve().parent.parent / "lib" / "mpv_hooks.lua"
        self.assertTrue(lua_path.is_file())
        with tempfile.TemporaryDirectory(prefix="musicctl-lua-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--idle=yes",
                    "--force-window=no",
                    "--ao=null",
                    "--audio-swresample-o=resampler=soxr,precision=28",
                    f"--input-ipc-server={sock}",
                    f"--script={lua_path}",
                    f"--script-opts-append=musicctl_hooks-data_dir={tmp}",
                    "--script-opts-append=musicctl_hooks-sponsorblock=0",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                connected = False
                for _ in range(50):
                    if os.path.exists(sock):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(sock)
                            s.close()
                            connected = True
                            break
                        except OSError:
                            s.close()
                    time.sleep(0.1)
                self.assertTrue(connected)
                client = tui.MpvClient(sock, timeout=1.0)
                self.assertTrue(client.send(["quit"]))
                _, stderr_out = proc.communicate(timeout=5)
                self.assertNotIn("Lua error", stderr_out)
                self.assertNotIn("error loading", stderr_out.lower())
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)


class TestProvidersAndDownloader(unittest.TestCase):
    def test_lazy_provider_short_circuit_stream_and_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prov_dir = root / "providers"
            prov_dir.mkdir()
            tmp_dir = root / "tmp"
            tmp_dir.mkdir()
            sentinel = root / "sentinel.called"

            sample_flac = root / "sample.flac"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                    str(sample_flac),
                ],
                check=True,
            )

            first = prov_dir / "10-first.sh"
            first.write_text(
                f"#!/usr/bin/env bash\ncat >/dev/null\n"
                f"printf '{{\"provider\":\"10-first.sh\",\"url\":\"file://{sample_flac}\",\"codec\":\"flac\",\"sample_rate\":44100,\"bit_depth\":16}}\\n'\n",
                encoding="utf-8",
            )
            first.chmod(0o755)

            second = prov_dir / "20-sentinel.sh"
            second.write_text(
                f"#!/usr/bin/env bash\ntouch '{sentinel}'\ncat >/dev/null\nexit 1\n",
                encoding="utf-8",
            )
            second.chmod(0o755)

            req_stream = providers_mod.build_provider_request("stream", "cd", "trk1", "Song One")
            chosen = providers_mod.resolve_stream_candidate(req_stream, prov_dir)
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen["provider"], "10-first.sh")
            self.assertFalse(sentinel.exists(), "20-sentinel.sh must not execute when 10-first.sh succeeds")

            req_dl = providers_mod.build_provider_request("download", "cd", "trk1", "Song One")
            res = providers_mod.fetch_validated_download(req_dl, prov_dir, tmp_dir)
            self.assertIsNotNone(res)
            self.assertFalse(sentinel.exists(), "20-sentinel.sh must not execute for validated cd download")
            res[0].unlink(missing_ok=True)

    def test_render_dest_path_and_traversal_clamp(self):
        with tempfile.TemporaryDirectory() as td:
            dl_dir = Path(td)
            p1 = downloader_mod.render_dest_path(
                dl_dir,
                downloader_mod.DEFAULT_TEMPLATE,
                {"artist": "Pamungkas", "album": "Walk the Talk", "track_num": 4, "title": "To the Bone", "id": "abc"},
                "flac",
            )
            self.assertEqual(p1, (dl_dir / "Pamungkas" / "Walk the Talk" / "04 - To the Bone.flac").resolve())

            p2 = downloader_mod.render_dest_path(
                dl_dir,
                "{artist}/{album}/{track} - {title}",
                {"artist": "", "album": "", "track_num": 0, "title": "Solo Track", "id": "xyz"},
                "opus",
            )
            self.assertEqual(p2, (dl_dir / "Solo Track.opus").resolve())

            p3 = downloader_mod.render_dest_path(
                dl_dir,
                "../../etc/{title}",
                {"artist": "A", "album": "B", "track_num": 1, "title": "../passwd", "id": "bad"},
                "mp3",
            )
            self.assertTrue(p3.is_relative_to(dl_dir.resolve()))

    def test_prepare_square_cover_crops_to_1_to_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            art_dir = root / "art"
            art_dir.mkdir()
            tmp_dir = root / "tmp"
            tmp_dir.mkdir()
            wide_img = root / "wide.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:size=128x72", "-frames:v", "1", str(wide_img)],
                check=True,
            )
            out = downloader_mod.prepare_square_cover("cov1", f"file://{wide_img}", art_dir, tmp_dir)
            self.assertIsNotNone(out)
            probe = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(out)],
                text=True,
            ))
            st = probe["streams"][0]
            self.assertEqual(st["width"], 72)
            self.assertEqual(st["height"], 72)

    def test_fetch_and_write_lrc_offline(self):
        with tempfile.TemporaryDirectory() as td:
            audio_dest = Path(td) / "Track.flac"
            audio_dest.write_bytes(b"stub")
            lrc = downloader_mod.fetch_and_write_lrc(
                audio_dest,
                "To the Bone",
                "Pamungkas",
                210,
                fetcher=lambda t, a, d: "[00:01.00] Line one\n",
            )
            self.assertEqual(lrc, audio_dest.with_suffix(".lrc"))
            self.assertEqual(lrc.read_text(encoding="utf-8"), "[00:01.00] Line one\n")

    def test_finalize_audio_formats_and_zero_gen_loss_remux(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_flac = root / "src.flac"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                    str(src_flac),
                ],
                check=True,
            )
            cover = root / "cover.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", str(cover)],
                check=True,
            )
            meta = {"id": "t1", "title": "Title", "artist": "Artist", "album": "Album", "track_num": 2, "year": 2024, "isrc": "US1234567890"}

            for fmt, exp_ext, exp_codec in [
                ("auto", "flac", "flac"),
                ("opus", "opus", "opus"),
                ("m4a", "m4a", "aac"),
                ("mp3", "mp3", "mp3"),
            ]:
                out_dest = root / f"out_{fmt}.placeholder"
                final_path, desc = downloader_mod.finalize_audio(
                    src_flac, out_dest, quality=("small" if fmt != "auto" else "cd"),
                    fmt=fmt, track=meta, cover_file=cover, replaygain=(fmt == "auto"), src_codec="flac",
                )
                self.assertEqual(final_path.suffix.lstrip("."), exp_ext)
                probe = json.loads(subprocess.check_output(
                    ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name:stream_tags:format_tags", "-of", "json", str(final_path)],
                    text=True,
                ))
                st = probe["streams"][0]
                self.assertEqual(st["codec_name"], exp_codec)
                tags = {}
                for k, v in (st.get("tags") or {}).items():
                    tags[k.lower()] = v
                for k, v in (probe.get("format", {}).get("tags") or {}).items():
                    tags[k.lower()] = v
                self.assertEqual(tags.get("title"), "Title")
                self.assertEqual(tags.get("album"), "Album")
                if fmt == "auto":
                    self.assertIn("replaygain_track_gain", tags)
                self.assertTrue(desc)


class TestLibraryIndexer(unittest.TestCase):
    def test_incremental_scan_cache_hit_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dl_dir = root / "dl"
            dl_dir.mkdir()
            cache_file = root / "cache" / "library.json"

            f1 = dl_dir / "track1.flac"
            f2 = dl_dir / "track2.m4a"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "flac",
                    "-metadata", "artist=Pamungkas",
                    "-metadata", "album=Walk the Talk",
                    "-metadata", "title=To the Bone",
                    "-metadata", "track=4",
                    "-metadata", "date=2018",
                    str(f1),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=523:duration=1",
                    "-c:a", "aac",
                    "-metadata", "artist=NIKI",
                    "-metadata", "album=Nicole",
                    "-metadata", "title=High School in Jakarta",
                    "-metadata", "track=2",
                    "-metadata", "date=2022",
                    str(f2),
                ],
                check=True,
            )

            calls = []

            def counting_prober(path: Path, lib_root: Path):
                calls.append(path.name)
                return library_mod.probe_audio_file(path, lib_root)

            entries1 = library_mod.scan_library(dl_dir, cache_file, prober=counting_prober)
            self.assertEqual(len(calls), 2)
            self.assertTrue(cache_file.is_file())
            by_name = {e["filename"]: e for e in entries1}
            self.assertEqual(by_name["track1.flac"]["artist"], "Pamungkas")
            self.assertEqual(by_name["track1.flac"]["album"], "Walk the Talk")
            self.assertEqual(by_name["track1.flac"]["title"], "To the Bone")
            self.assertEqual(by_name["track1.flac"]["track"], 4)
            self.assertEqual(by_name["track1.flac"]["codec"], "flac")
            self.assertEqual(by_name["track2.m4a"]["artist"], "NIKI")
            self.assertEqual(by_name["track2.m4a"]["album"], "Nicole")
            self.assertEqual(by_name["track2.m4a"]["codec"], "aac")

            # Second scan on unchanged files must hit cache with 0 prober calls.
            entries2 = library_mod.scan_library(dl_dir, cache_file, prober=counting_prober)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(entries2), 2)

            # Mutating mtime invalidates only the touched file.
            st2 = f2.stat()
            os.utime(f2, ns=(st2.st_atime_ns, st2.st_mtime_ns + 10_000_000_000))
            library_mod.scan_library(dl_dir, cache_file, prober=counting_prober)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1], "track2.m4a")

            # Forced rescan re-probes all files.
            library_mod.scan_library(dl_dir, cache_file, rescan=True, prober=counting_prober)
            self.assertEqual(len(calls), 5)

            # Deleted file is pruned from index.
            f2.unlink()
            entries5 = library_mod.scan_library(dl_dir, cache_file, prober=counting_prober)
            self.assertEqual(len(entries5), 1)
            loaded = library_mod.load_index(cache_file, dl_dir)
            self.assertEqual(len(loaded), 1)

    def test_filename_fallback_grouping_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dl_dir = root / "dl"
            cache_file = root / "library.json"
            nested = dl_dir / "Artist X" / "Album Y" / "03 - Untitled Gem.opus"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"")
            other = dl_dir / "Artist X" / "Album Z" / "01 - First Cut.opus"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"")
            va_track = dl_dir / "Guest" / "Album Z" / "02 - Collab Cut.opus"
            va_track.parent.mkdir(parents=True)
            va_track.write_bytes(b"")

            entries = library_mod.scan_library(dl_dir, cache_file)
            self.assertEqual(len(entries), 3)
            gem = [e for e in entries if e["filename"] == "03 - Untitled Gem.opus"][0]
            self.assertEqual(gem["artist"], "Artist X")
            self.assertEqual(gem["album"], "Album Y")
            self.assertEqual(gem["track"], 3)
            self.assertEqual(gem["title"], "Untitled Gem")

            by_art = library_mod.group_by_artist(entries)
            self.assertEqual([g["artist"] for g in by_art], ["Artist X", "Guest"])
            self.assertEqual(len(by_art[0]["tracks"]), 2)

            by_alb = library_mod.group_by_album(entries)
            alb_map = {g["album"]: g for g in by_alb}
            self.assertEqual(alb_map["Album Y"]["artist"], "Artist X")
            self.assertEqual(alb_map["Album Z"]["artist"], "Various Artists")
            self.assertEqual([t["track"] for t in alb_map["Album Z"]["tracks"]], [1, 2])

            filtered = library_mod.filter_entries(entries, "artist untitled")
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["title"], "Untitled Gem")


class TestProvidersV2AndHooks(unittest.TestCase):
    def test_dash_manifest_remux_and_req_v2_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dash_dir = root / "dash"
            dash_dir.mkdir()
            prov_dir = root / "providers"
            prov_dir.mkdir()
            tmp_dir = root / "tmp"
            tmp_dir.mkdir()

            mpd = dash_dir / "manifest.mpd"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                    "-f", "dash", str(mpd),
                ],
                check=True,
            )

            req_dump = root / "req.json"
            prov = prov_dir / "10-dash.sh"
            prov.write_text(
                f"#!/usr/bin/env bash\ncat >'{req_dump}'\n"
                f"printf '{{\"provider\":\"10-dash.sh\",\"manifest\":\"file://{mpd}\",\"codec\":\"flac\",\"sample_rate\":44100,\"bit_depth\":16}}\\n'\n",
                encoding="utf-8",
            )
            prov.chmod(0o755)

            req = providers_mod.build_provider_request(
                "download", "cd", "dz-1", "Track One", "Artist One", "Album One",
                duration=1, isrc="USABC1234567", track_num=3, year=2024, source="deezer", fmt="flac",
            )
            res = providers_mod.fetch_validated_download(req, prov_dir, tmp_dir)
            self.assertIsNotNone(res)
            raw_path, info, cand = res
            try:
                self.assertEqual(info["codec"], "flac")
                self.assertGreater(info["duration"], 0.0)
                self.assertEqual(cand["manifest"], f"file://{mpd}")
                captured = json.loads(req_dump.read_text(encoding="utf-8"))
                self.assertEqual(captured["source"], "deezer")
                self.assertEqual(captured["format"], "flac")
                self.assertEqual(captured["isrc"], "USABC1234567")
            finally:
                raw_path.unlink(missing_ok=True)

    def test_trigger_hooks_delivery_and_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = Path(td)
            ev_dir = hdir / "on-download-complete.d"
            ev_dir.mkdir(parents=True)
            out_json = hdir / "captured.json"

            h1 = ev_dir / "10-ok.sh"
            h1.write_text(f"#!/usr/bin/env bash\ncat >'{out_json}'\n", encoding="utf-8")
            h1.chmod(0o755)

            h2 = ev_dir / "20-fail.sh"
            h2.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 1\n", encoding="utf-8")
            h2.chmod(0o755)

            h3 = ev_dir / "30-slow.sh"
            h3.write_text("#!/usr/bin/env bash\nsleep 2\n", encoding="utf-8")
            h3.chmod(0o755)

            count = common_mod.trigger_hooks(
                "on-download-complete",
                {"id": "x1", "title": "Song", "codec": "flac"},
                hooks_dir=hdir,
                timeout=0.2,
            )
            self.assertEqual(count, 3)
            data = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(data["event"], "on-download-complete")
            self.assertEqual(data["id"], "x1")
            self.assertIn("timestamp", data)


class TestTuiWP2GH(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        d = tui.SovereignDashboard.__new__(tui.SovereignDashboard)
        d.data_dir = td / "data"
        d.cache_dir = td / "cache"
        d.download_dir = td / "dl"
        d.data_dir.mkdir(parents=True, exist_ok=True)
        d.cache_dir.mkdir(parents=True, exist_ok=True)
        d.download_dir.mkdir(parents=True, exist_ok=True)
        d.ui_events = queue.SimpleQueue()
        d.lock = threading.Lock()
        d.active_tab = "home"
        d.right_tab = "upnext"
        d.catalog_source = "ytm"
        d.search_type = "songs"
        d.local_group_mode = "flat"
        d.nav_stack = []
        d.drill_title = ""
        d.drill_ref = ""
        d.drill_kind = ""
        d._local_groups = {}
        d.tracks = []
        d.search_results = []
        d.selected_idx = 0
        d.scroll_top = 0
        d.mpv_state = {}
        d.eq_mode = "flat"
        d.norm_on = False
        d.quality_mode = "cd"
        d.autoplay_on = False
        d.sleep_rem = ""
        d.fav_ids = set()
        d.toast_msg = ""
        d.toast_until = 0.0
        d.dl_jobs = []
        return d

    def test_dl_jsonl_parser_and_drain_ui_events(self):
        ev = tui.parse_download_jsonl_line('{"job_id":"dl-1","title":"Song","stage":"fetching","pct":25,"status":"running","dest":"","error":""}\n')
        self.assertIsNotNone(ev)
        self.assertEqual(ev["pct"], 25)
        self.assertIsNone(tui.parse_download_jsonl_line("[OK] plain line"))

        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.ui_events.put(("dl_event", ev))
            d.ui_events.put(("dl_event", {"job_id": "dl-1", "title": "Song", "stage": "done", "pct": 100, "status": "done", "dest": "/tmp/Song.flac", "error": ""}))
            d.drain_ui_events()
            self.assertEqual(len(d.dl_jobs), 1)
            self.assertEqual(d.dl_jobs[0]["status"], "done")
            self.assertEqual(d.dl_jobs[0]["pct"], 100)
            self.assertIn("DL Done", d.toast_msg)

    def test_nav_stack_catalog_cycle_local_grouping_and_playlists_tab(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._make_dash(root)

            # Catalog & search type cycling
            self.assertEqual(d.cycle_catalog(), "deezer")
            self.assertEqual(d.cycle_catalog(), "itunes")
            self.assertEqual(d.cycle_catalog(), "ytm")
            self.assertEqual(d.cycle_search_type(), "albums")
            self.assertEqual(d.cycle_search_type(), "artists")
            self.assertEqual(d.cycle_search_type(), "playlists")
            self.assertEqual(d.cycle_search_type(), "songs")

            # Push/pop nav_stack preserves cursor & scroll
            d.tracks = [("a1", "Alb 1", "Art", "[ALB]"), ("a2", "Alb 2", "Art", "[ALB]")]
            d.selected_idx = 1
            d.scroll_top = 1
            d.push_nav("Alb 2", [("s1", "Track 1", "Art", "[YTM]")], context_ref="ytm:album:a2", kind="browse")
            self.assertEqual(len(d.nav_stack), 1)
            self.assertEqual(d.selected_idx, 0)
            self.assertEqual(d.tracks[0][1], "Track 1")
            self.assertTrue(d.pop_nav())
            self.assertEqual(d.selected_idx, 1)
            self.assertEqual(d.scroll_top, 1)
            self.assertEqual(d.tracks[1][1], "Alb 2")

            # 7 Pl TSV loading
            pl_dir = d.data_dir / "playlists"
            pl_dir.mkdir()
            (pl_dir / "chill.tsv").write_text("vid1\tSong 1\tArt\n", encoding="utf-8")
            d.active_tab = "playlists"
            d.load_tab_items()
            self.assertEqual(d.tracks, [("pl:chill", "chill", "1 tracks", "[PL]")])

            # 6 Local grouping toggle
            (d.download_dir / "Art X" / "Alb Y").mkdir(parents=True)
            (d.download_dir / "Art X" / "Alb Y" / "01 - Track.flac").write_bytes(b"")
            d.active_tab = "local"
            self.assertEqual(d.cycle_local_group(), "artist")
            self.assertEqual(d.tracks[0][3], "[L-ART]")
            self.assertEqual(d.cycle_local_group(), "album")
            self.assertEqual(d.tracks[0][3], "[L-ALB]")
            self.assertEqual(d.cycle_local_group(), "flat")
            self.assertEqual(d.tracks[0][3], "[LOC]")

    def test_gap_closures_spotify_client_id_dedup_and_sync_pl(self):
        # Gap #1: Spotify URL regex and HTML parsing in downloader.py
        self.assertIsNotNone(downloader_mod.SPOTIFY_URL_RE.match("https://open.spotify.com/album/12345abc"))
        html_doc = '<script id="__NEXT_DATA__" type="application/json">{"props":{"trackList":[{"title":"Track A","subtitle":"Artist A"}]}}</script>'
        self.assertEqual(downloader_mod.parse_spotify_html(html_doc), [("Track A", "Artist A")])

        # Gap #3: _client_id strips placeholder dlm-* row so no ghost entry remains
        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.ui_events.put(("dl_event", {"job_id": "dlm-1-1", "title": "Song", "stage": "queued", "pct": 0, "status": "running", "dest": "", "error": ""}))
            d.ui_events.put(("dl_event", {"_client_id": "dlm-1-1", "job_id": "dl-100-99", "title": "Song", "stage": "done", "pct": 100, "status": "done", "dest": "/tmp/Song.flac", "error": ""}))
            d.drain_ui_events()
            self.assertEqual(len(d.dl_jobs), 1)
            self.assertEqual(d.dl_jobs[0]["job_id"], "dl-100-99")
            self.assertEqual(d.dl_jobs[0]["status"], "done")

            # Gap #5: sync_pl starts download manager sync and focuses downloads tab
            started = []
            class StubDL:
                def start(self, args, label=""):
                    started.append((args, label))
                    return "dlm-stub"
            d.dl_manager = StubDL()
            d.active_tab = "playlists"
            d.tracks = [("pl:chill", "chill", "2 tracks", "[PL]")]
            d.selected_idx = 0
            d.handle_action("sync_pl", None)
            self.assertEqual(started, [(["sync", "pl:chill"], "Sync chill")])
            self.assertEqual(d.right_tab, "downloads")


class TestP0BugFixes(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        return TestTuiWP2GH()._make_dash(td)

    def test_bug1_catalog_ref_and_container_routing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._make_dash(root)
            cli_calls: list[list[str]] = []
            batch_calls: list[list[list[str]]] = []
            browse_calls: list[tuple[str, str]] = []

            d.run_cli_async = lambda args, toast="", reload_after=False, timeout=30.0: cli_calls.append(list(args))  # type: ignore[method-assign]
            d.run_cli_batch_async = lambda cmds, toast="", reload_after=False, timeout=30.0: batch_calls.append([list(c) for c in cmds])  # type: ignore[method-assign]
            d.trigger_browse = lambda ref, title: browse_calls.append((ref, title))  # type: ignore[method-assign]

            # 1. Play non-YTM catalog songs directly via ["play", ref]
            d.tracks = [
                ("deezer:song:3135556", "HBFS", "Daft Punk", "[DEEZER]"),
                ("itunes:song:1440857781", "Get Lucky", "Daft Punk", "[ITUNES]"),
            ]
            d.selected_idx = 0
            d.play_selected()
            d.selected_idx = 1
            d.play_selected()
            self.assertEqual(
                cli_calls[:2],
                [["play", "deezer:song:3135556"], ["play", "itunes:song:1440857781"]],
            )

            # 2. Local playlist drill-down tags child tracks as [TRK], not [PL]
            pl_dir = d.data_dir / "playlists"
            pl_dir.mkdir(parents=True, exist_ok=True)
            (pl_dir / "chill.tsv").write_text("vid1\tSong One\tArtist One\n", encoding="utf-8")
            d.tracks = [("pl:chill", "chill", "1 tracks", "[PL]")]
            d.selected_idx = 0
            d.play_selected()
            self.assertEqual(d.tracks, [("vid1", "Song One", "Artist One", "[TRK]")])
            self.assertEqual(d._collect_track_refs(), ["vid1"])
            d.selected_idx = 0
            d.play_selected()
            self.assertEqual(browse_calls, [])
            self.assertEqual(cli_calls[2][0], "_play_line")

            # 3. Enqueue catalog refs, containers, and local groups
            cli_calls.clear()
            d.tracks = [
                ("deezer:song:3135556", "HBFS", "Daft Punk", "[DEEZER]"),
                ("deezer:album:302127", "Discovery", "Daft Punk", "[ALB]"),
                ("pl:chill", "chill", "1 tracks", "[PL]"),
                ("lart:Pamungkas", "Pamungkas", "2 tracks", "[L-ART]"),
            ]
            d._local_groups = {
                "lart:Pamungkas": [{"path": "/tmp/a.flac"}, {"path": "/tmp/b.flac"}],
            }
            for idx in range(4):
                d.selected_idx = idx
                d.enqueue_selected()
            self.assertEqual(
                cli_calls,
                [
                    ["add", "deezer:song:3135556"],
                    ["add", "deezer:album:302127"],
                    ["add", "pl:chill"],
                ],
            )
            self.assertEqual(
                batch_calls,
                [[["add", "/tmp/a.flac"], ["add", "/tmp/b.flac"]]],
            )

            # 4. dl_sel guards already-local tracks and local groups
            started: list[tuple[list[str], str]] = []

            class StubDL:
                def start(self, args, label=""):
                    started.append((args, label))
                    return "dlm-1"

            d.dl_manager = StubDL()
            d.tracks = [
                ("/tmp/a.flac", "Track A", "Local Audio", "[LOC]"),
                ("lart:Pamungkas", "Pamungkas", "2 tracks", "[L-ART]"),
            ]
            for idx in (0, 1):
                d.selected_idx = idx
                d.toast_msg = ""
                d.handle_action("dl_sel", None)
                self.assertEqual(d.toast_msg, "Already local")
            self.assertEqual(started, [])

    def test_bug2_sequential_batch_play_all_and_add_all(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._make_dash(root)
            log_file = root / "calls.log"
            mock_bin = root / "mock-musicctl"
            mock_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                f"log_path = {str(log_file)!r}\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'play':\n"
                "    time.sleep(0.05)\n"
                "with open(log_path, 'a', encoding='utf-8') as fh:\n"
                "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            mock_bin.chmod(0o755)
            d.bin_path = str(mock_bin)
            d.drill_ref = "lart:Pamungkas"
            d.drill_title = "Pamungkas"
            d.tracks = [
                ("/m/1.flac", "One", "Pamungkas", "[LOC]"),
                ("/m/2.flac", "Two", "Pamungkas", "[LOC]"),
                ("/m/3.flac", "Three", "Pamungkas", "[LOC]"),
            ]

            d.handle_action("play_all", None)
            kind, payload = d.ui_events.get(timeout=5.0)
            self.assertEqual(kind, "cli_done")
            self.assertEqual(payload, ("Playing 3 tracks", True))
            self.assertEqual(
                log_file.read_text(encoding="utf-8").splitlines(),
                ["play /m/1.flac", "add /m/2.flac", "add /m/3.flac"],
            )

            log_file.unlink()
            d.handle_action("add_all", None)
            kind, payload = d.ui_events.get(timeout=5.0)
            self.assertEqual(kind, "cli_done")
            self.assertEqual(payload, ("Queued 3 tracks", True))
            self.assertEqual(
                log_file.read_text(encoding="utf-8").splitlines(),
                ["add /m/1.flac", "add /m/2.flac", "add /m/3.flac"],
            )

    def test_bug3_dl_sync_prune_manifest_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            cache_dir = root / "cache"
            dl_dir = root / "dl"
            prov_dir = root / "providers"
            hooks_dir = root / "hooks"
            pl_dir = data_dir / "playlists"
            for p in (data_dir, cache_dir, dl_dir, prov_dir, hooks_dir, pl_dir):
                p.mkdir(parents=True, exist_ok=True)

            src_flac = root / "src.flac"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                    str(src_flac),
                ],
                check=True,
            )
            prov = prov_dir / "10-local.sh"
            prov.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "req = json.load(sys.stdin)\n"
                "if str(req.get('id', '')).startswith('trkFail'):\n"
                "    sys.exit(0)\n"
                f"print(json.dumps({{'provider': '10-local.sh', 'url': 'file://{src_flac}', 'codec': 'flac', 'sample_rate': 44100, 'bit_depth': 16}}))\n",
                encoding="utf-8",
            )
            prov.chmod(0o755)

            unrelated = dl_dir / "unrelated_album" / "Keep Me.flac"
            unrelated.parent.mkdir(parents=True, exist_ok=True)
            unrelated.write_bytes(b"keep")

            (pl_dir / "a.tsv").write_text(
                "trk11111111\tSong One\tArtist A • Album A • 0:01\n"
                "trk22222222\tSong Two\tArtist B • Album B • 0:01\n",
                encoding="utf-8",
            )
            (pl_dir / "b.tsv").write_text(
                "trk22222222\tSong Two\tArtist B • Album B • 0:01\n",
                encoding="utf-8",
            )

            env_patch = {
                "MUSICCTL_DATA_DIR": str(data_dir),
                "MUSICCTL_CACHE_DIR": str(cache_dir),
                "MUSICCTL_DOWNLOAD_DIR": str(dl_dir),
                "MUSICCTL_PROVIDERS_DIR": str(prov_dir),
                "MUSICCTL_HOOKS_DIR": str(hooks_dir),
                "MUSICCTL_DL_TEMPLATE": "{artist}/{album}/{title}",
            }
            p_one = dl_dir / "Artist A" / "Album A" / "Song One.flac"
            p_two = dl_dir / "Artist B" / "Album B" / "Song Two.flac"

            with mock.patch.dict(os.environ, env_patch):
                # 1. Sync pl:a -> downloads both, keeps unrelated
                self.assertEqual(downloader_mod.run_download(["--quality", "cd", "sync", "pl:a", "--prune"]), 0)
                self.assertTrue(p_one.is_file())
                self.assertTrue(p_two.is_file())
                self.assertTrue(unrelated.is_file())

                # 2. Sync pl:b sharing Song Two
                self.assertEqual(downloader_mod.run_download(["--quality", "cd", "sync", "pl:b"]), 0)

                # 3. Remove Song Two from pl:a & prune -> Song Two stays because pl:b owns it
                (pl_dir / "a.tsv").write_text("trk11111111\tSong One\tArtist A • Album A • 0:01\n", encoding="utf-8")
                self.assertEqual(downloader_mod.run_download(["--quality", "cd", "sync", "pl:a", "--prune"]), 0)
                self.assertTrue(p_one.is_file())
                self.assertTrue(p_two.is_file())
                self.assertTrue(unrelated.is_file())

                # 4. Remove Song One from pl:a & prune -> exclusive Song One + .lrc pruned, unrelated kept
                lrc_one = p_one.with_suffix(".lrc")
                lrc_one.write_text("[00:00.00] hi\n", encoding="utf-8")
                (pl_dir / "a.tsv").write_text("", encoding="utf-8")
                self.assertEqual(downloader_mod.run_download(["--quality", "cd", "sync", "pl:a", "--prune"]), 0)
                self.assertFalse(p_one.exists())
                self.assertFalse(lrc_one.exists())
                self.assertTrue(p_two.is_file())
                self.assertTrue(unrelated.is_file())

                # 5. Partial failure on pl:b --prune skips pruning Song Two
                (pl_dir / "b.tsv").write_text("trkFail9999\tBroken\tArtist B • Album B • 0:01\n", encoding="utf-8")
                with mock.patch.object(downloader_mod, "_ytdlp_fallback_download", return_value=None):
                    rc = downloader_mod.run_download(["--quality", "cd", "sync", "pl:b", "--prune"])
                self.assertEqual(rc, 1)
                self.assertTrue(p_two.is_file())
                self.assertTrue(unrelated.is_file())


class TestP1Consolidation(unittest.TestCase):
    def test_common_parse_spotify_html_ssot(self):
        self.assertIs(downloader_mod.parse_spotify_html, downloader_mod.common.parse_spotify_html)
        self.assertEqual(Path(downloader_mod.parse_spotify_html.__code__.co_filename).resolve(), COMMON_PATH.resolve())
        nd_html = (
            '<html><head><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"trackList":[{"title":"Track A","subtitle":"Artist A"}],'
            '"tracks":{"items":[{"track":{"name":"Track B","artists":[{"name":"Art B1"},{"name":"Art B2"}]}}]},'
            '"single":{"type":"track","name":"Track C","artists":[{"name":"Art C"}]}}}'
            '</script></head></html>'
        )
        self.assertEqual(
            common_mod.parse_spotify_html(nd_html),
            [
                ("Track A", "Artist A"),
                ("Track B", "Art B1, Art B2"),
                ("Track C", "Art C"),
            ],
        )

        oembed_json = json.dumps({"title": "OEmbed Song", "author_name": "OEmbed Artist"})
        self.assertEqual(common_mod.parse_spotify_html(oembed_json), [("OEmbed Song", "OEmbed Artist")])

        og1 = '<html><head><meta property="og:title" content="Sailing | Spotify" /><meta property="og:description" content="grentperez · Song · 2024" /></head></html>'
        self.assertEqual(common_mod.parse_spotify_html(og1), [("Sailing", "grentperez")])

        og2 = '<html><head><meta content="To the Bone - song and lyrics by Pamungkas | Spotify" property="og:title" /><meta content="Listen to To the Bone on Spotify. Pamungkas · Song · 2018" property="og:description" /></head></html>'
        self.assertEqual(common_mod.parse_spotify_html(og2), [("To the Bone", "Pamungkas")])

    def test_common_fetch_lyrics_ssot_and_ua(self):
        self.assertNotIn("musicctl/2.1", TUI_PATH.read_text(encoding="utf-8"))
        seen_ua: list[str | None] = []

        class FakeResp:
            def __init__(self, body: str) -> None:
                self._body = body.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return self._body

        # 1. Direct LRCLIB /api/get hit
        def fake_open_get(req, timeout=5.0):
            seen_ua.append(req.get_header("User-agent"))
            if "lrclib.net/api/get" in req.full_url:
                return FakeResp(json.dumps({
                    "trackName": "Monolog",
                    "artistName": "Pamungkas",
                    "syncedLyrics": "[00:05.00] First line\n[00:10.00] Second line",
                }))
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_open_get):
            res = common_mod.fetch_lyrics("Monolog (Official Video)", artist="Pamungkas", duration=200)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["source"], "lrclib")
        self.assertEqual(res["synced_lines"], [(5.0, "First line"), (10.0, "Second line")])
        self.assertIn("=== Monolog - Pamungkas ===", common_mod.format_lyrics_plain(res))
        self.assertEqual(seen_ua, ["musicctl/2.3"])

        # 2. /api/get 404 -> /api/search fallback
        seen_ua.clear()

        def fake_open_search(req, timeout=5.0):
            seen_ua.append(req.get_header("User-agent"))
            if "lrclib.net/api/get" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
            if "lrclib.net/api/search" in req.full_url:
                return FakeResp(json.dumps([{
                    "trackName": "Demo",
                    "artistName": "Artist",
                    "duration": 120,
                    "plainLyrics": "Plain body",
                }]))
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with mock.patch.object(urllib.request, "urlopen", side_effect=fake_open_search):
                res2 = common_mod.fetch_lyrics("Demo", artist="Artist", duration=120)
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertIsNotNone(res2)
        assert res2 is not None
        self.assertEqual(res2["plain_text"], "Plain body")
        self.assertTrue(all(ua == "musicctl/2.3" for ua in seen_ua))

        # 3. LRCLIB empty -> lyrics.ovh fallback
        seen_ua.clear()

        def fake_open_fb(req, timeout=5.0):
            seen_ua.append(req.get_header("User-agent"))
            if "lrclib.net" in req.full_url:
                return FakeResp("[]")
            if "api.lyrics.ovh/v1/" in req.full_url:
                return FakeResp(json.dumps({"lyrics": "Paroles de la chanson X\nFallback line"}))
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_open_fb):
            res3 = common_mod.fetch_lyrics("Song", artist="Singer")
        self.assertIsNotNone(res3)
        assert res3 is not None
        self.assertEqual(res3["source"], "fallback")
        self.assertEqual(res3["plain_text"], "Fallback line")
        self.assertIn("[Fallback]", common_mod.format_lyrics_plain(res3))
        self.assertTrue(all(ua == "musicctl/2.3" for ua in seen_ua))

    def test_common_split_shell_args_and_cli(self):
        self.assertEqual(common_mod.split_shell_args(""), [])
        self.assertEqual(common_mod.split_shell_args("   "), [])
        self.assertEqual(
            common_mod.split_shell_args('--audio-device="pipewire/My Speaker" --title=\'Hi There\''),
            ["--audio-device=pipewire/My Speaker", "--title=Hi There"],
        )
        # Shell substitution syntax stays literal (no shell eval)
        self.assertEqual(
            common_mod.split_shell_args('--title="$(touch /tmp/nope)" --ao=null'),
            ["--title=$(touch /tmp/nope)", "--ao=null"],
        )
        # Unbalanced quotes fall back cleanly without crashing
        self.assertEqual(
            common_mod.split_shell_args("--ao=null --title='unclosed"),
            ["--ao=null", "--title='unclosed"],
        )
        proc = subprocess.run(
            [sys.executable, str(COMMON_PATH), "split-args", '--ao=null --title="Space Value"'],
            capture_output=True,
            check=True,
        )
        self.assertEqual(proc.stdout, b"--ao=null\x00--title=Space Value\x00")

    def test_mpv_client_batch_loadfile_and_resume_live_isolated(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-p1-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            wav1 = os.path.join(tmp, "t1.wav")
            wav2 = os.path.join(tmp, "t2.wav")
            for wp in (wav1, wav2):
                with wave.open(wp, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(8000)
                    w.writeframes(b"\x00\x00" * 16000)

            def start_mpv():
                proc = subprocess.Popen(
                    [
                        "mpv",
                        "--no-video",
                        "--idle=yes",
                        "--force-window=no",
                        "--ao=null",
                        f"--input-ipc-server={sock}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(50):
                    if os.path.exists(sock):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        try:
                            s.connect(sock)
                            s.close()
                            break
                        except OSError:
                            s.close()
                    time.sleep(0.1)
                return proc

            proc1 = start_mpv()
            try:
                client = common_mod.MpvClient(sock, timeout=1.0)
                ok_cnt, total = client.batch_loadfile(
                    [
                        {"url": wav1, "title": "Track One", "headers": {"X-Test": "a,b"}},
                        {"url": wav2, "title": "Track Two"},
                    ],
                    action="play",
                    timeout=3.0,
                )
                self.assertEqual((ok_cnt, total), (3, 3))
                snap = None
                for _ in range(30):
                    s_get = client.batch_get(["playlist-count", "media-title", "idle-active"])
                    if s_get and s_get.get("idle-active") is False and s_get.get("playlist-count") == 2 and s_get.get("media-title"):
                        snap = s_get
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(snap)
                assert snap is not None
                self.assertEqual(snap["playlist-count"], 2)
                self.assertEqual(snap["media-title"], "Track One")
                client.send(["quit"])
            finally:
                proc1.terminate()
                proc1.wait(timeout=3)

            if os.path.exists(sock):
                os.unlink(sock)

            sess_file = Path(tmp) / "session.json"
            sess_file.write_text(
                json.dumps(
                    {
                        "playlist": [wav1, wav2],
                        "playlist_pos": 1,
                        "time_pos": 0.0,
                        "volume": 72,
                        "muted": True,
                        "loop": "all",
                        "speed": 1.25,
                    }
                ),
                encoding="utf-8",
            )

            proc2 = start_mpv()
            try:
                res_proc = subprocess.run(
                    [sys.executable, str(COMMON_PATH), "mpv-resume", sock, str(sess_file)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                )
                self.assertEqual(res_proc.stdout.strip(), "2")
                client2 = common_mod.MpvClient(sock, timeout=1.0)
                state = None
                for _ in range(30):
                    s_get = client2.batch_get(
                        ["idle-active", "playlist-count", "playlist-pos", "volume", "mute", "loop-playlist", "speed"]
                    )
                    if s_get and s_get.get("idle-active") is False and s_get.get("playlist-pos") == 1:
                        state = s_get
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state["playlist-count"], 2)
                self.assertEqual(state["playlist-pos"], 1)
                self.assertEqual(int(round(float(state["volume"]))), 72)
                self.assertTrue(state["mute"])
                self.assertEqual(state["loop-playlist"], "inf")
                self.assertEqual(float(state["speed"]), 1.25)
                client2.send(["quit"])
            finally:
                proc2.terminate()
                proc2.wait(timeout=3)


class TestWP3AdaptiveHybridTui(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        d = TestTuiWP2GH()._make_dash(td)
        d.layout_mode = "auto"
        d.show_drawer = True
        d.focus_pane = "list"
        d.drawer_scroll = 0
        d.drawer_sel = 0
        d.last_list_body_h = 6
        d.last_drawer_body_h = 5
        d.last_layout = {}
        d.last_active_path = ""
        d.lyrics_track_key = ""
        d.lyrics_header = ""
        d.lyrics_lines = []
        d.lyrics_loading = False
        d.search_focus = False
        d.search_query = ""
        d.show_help = False
        d.art_enabled = False
        d.art_cache = {}
        d.art_inflight = set()
        d.healed_vids = set()
        d.heal_inflight = set()
        d.tick = 0
        return d

    def test_compute_layout_adaptive_split_stacked_focus(self):
        # Tall/portrait terminal (e.g. 100x55 from screenshot) uses stacked full-width layout
        lay_tall = tui.compute_layout(55, 100, "auto", True)
        self.assertEqual(lay_tall["effective"], "stacked")
        self.assertEqual(lay_tall["now_playing"], (2, 9, 0, 100))
        self.assertEqual(lay_tall["list"], (11, 27, 0, 100))
        self.assertEqual(lay_tall["drawer"], (38, 13, 0, 100))
        self.assertEqual(
            lay_tall["now_playing"][1] + lay_tall["list"][1] + lay_tall["drawer"][1],
            55 - 6,
        )

        # Wide landscape terminal uses 58/42 split
        lay_wide = tui.compute_layout(40, 140, "auto", True)
        self.assertEqual(lay_wide["effective"], "split")
        self.assertEqual(lay_wide["list"], (2, 34, 0, 81))
        self.assertEqual(lay_wide["now_playing"][2:], (81, 59))
        self.assertEqual(lay_wide["drawer"][2:], (81, 59))
        self.assertEqual(lay_wide["now_playing"][1] + lay_wide["drawer"][1], 34)

        # Focus mode or hidden drawer hides drawer and maximizes list width
        lay_focus = tui.compute_layout(55, 100, "focus", True)
        self.assertEqual(lay_focus["effective"], "focus")
        self.assertIsNone(lay_focus["drawer"])
        self.assertEqual(lay_focus["now_playing"], (2, 5, 0, 100))
        self.assertEqual(lay_focus["list"], (7, 44, 0, 100))

        lay_hidden = tui.compute_layout(40, 140, "auto", False)
        self.assertEqual(lay_hidden["effective"], "focus")
        self.assertIsNone(lay_hidden["drawer"])

    def test_lightweight_wave_art_size_and_placeholder_lyrics_deferral(self):
        wave_play = tui.build_lightweight_wave(52, 4, False)
        self.assertEqual(len(wave_play), 52)
        self.assertGreater(len(set(wave_play)), 1)
        self.assertEqual(tui.build_lightweight_wave(52, 4, True), "▁" * 52)
        self.assertEqual(tui.build_lightweight_wave(0, 4, False), "")

        self.assertEqual(tui.calc_art_size(9, 100), (16, 7))
        self.assertIsNone(tui.calc_art_size(5, 100))
        self.assertIsNone(tui.calc_art_size(10, 35))

        self.assertTrue(tui.is_placeholder_title("watch?v=TXxBzIJVdCI", "TXxBzIJVdCI"))
        self.assertTrue(tui.is_placeholder_title("TXxBzIJVdCI", "TXxBzIJVdCI"))
        self.assertTrue(tui.is_placeholder_title("", "TXxBzIJVdCI"))
        self.assertFalse(tui.is_placeholder_title("Adek Sarah", "TXxBzIJVdCI"))

        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.right_tab = "lyrics"
            fetched: list[tuple[str, str, str, float]] = []
            d.fetch_lyrics_async = lambda title, tk, artist="", duration=0.0: fetched.append((title, tk, artist, duration))  # type: ignore[method-assign]

            state = {
                "idle-active": False,
                "path": "https://www.youtube.com/watch?v=TXxBzIJVdCI",
                "media-title": "watch?v=TXxBzIJVdCI",
                "metadata": {},
                "duration": 249.0,
            }

            class StubMpv:
                def batch_get(self, _props):
                    return dict(state)

            d.mpv = StubMpv()  # type: ignore[assignment]
            d.poll_mpv()
            self.assertEqual(fetched, [])
            self.assertEqual(d.lyrics_header, "Waiting for track metadata...")

            # Once mpv resolves real metadata, poll_mpv triggers lyrics lookup automatically
            state["media-title"] = "Adek Sarah"
            state["metadata"] = {"artist": "Kapten Cantik"}
            d.poll_mpv()
            self.assertEqual(
                fetched,
                [("Adek Sarah", "https://www.youtube.com/watch?v=TXxBzIJVdCI", "Kapten Cantik", 249.0)],
            )

    def test_hybrid_keyboard_focus_scroll_layout_and_batch_shortcuts(self):
        self.assertEqual(tui.apply_search_key("dj clbk", 21), ("", "stay"))
        self.assertEqual(tui.apply_search_key("dj clbk", "\x0b"), ("", "stay"))

        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.tracks = [(f"id{i:09d}", f"Track {i}", "", "[YTM]") for i in range(10)]
            d.mpv_state = {
                "idle-active": False,
                "playlist": [{"title": f"Q{i}", "current": (i == 0)} for i in range(6)],
            }
            d.lyrics_lines = [(float(i), f"Line {i}") for i in range(20)]

            sent_cmds: list[list[object]] = []

            class StubMpv:
                def send(self, cmd):
                    sent_cmds.append(list(cmd))
                    return True

            d.mpv = StubMpv()  # type: ignore[assignment]

            actions: list[tuple[str, object]] = []
            orig_handle_action = d.handle_action

            def spy_action(action, payload=None, bstate=0):
                actions.append((action, payload))
                if action in ("play_all", "add_all", "dl_all", "dl_cancel"):
                    return
                orig_handle_action(action, payload, bstate)

            d.handle_action = spy_action  # type: ignore[method-assign]

            # Layout cycling (v) and drawer toggle (z)
            d.handle_key("v")
            self.assertEqual(d.layout_mode, "split")
            d.handle_key("v")
            self.assertEqual(d.layout_mode, "stacked")
            d.handle_key("v")
            self.assertEqual(d.layout_mode, "focus")
            self.assertFalse(d.show_drawer)
            d.handle_key("z")
            self.assertTrue(d.show_drawer)
            self.assertEqual(d.layout_mode, "auto")

            # Pane focus (l -> drawer, j/PgDn/Enter in Up Next, h -> list)
            d.handle_key("l")
            self.assertEqual(d.focus_pane, "drawer")
            d.handle_key("j")
            self.assertEqual(d.drawer_sel, 1)
            d.handle_key(tui.curses.KEY_NPAGE)
            self.assertEqual(d.drawer_sel, 4)
            d.handle_key(10)
            self.assertEqual(
                sent_cmds,
                [["set_property", "playlist-pos", 4], ["set_property", "pause", False]],
            )

            # Drawer tab keys (Y, Shift-Tab, I, D, U) and lyrics scrolling
            d.handle_key("Y")
            self.assertEqual(d.right_tab, "lyrics")
            d.handle_key(tui.curses.KEY_NPAGE)
            self.assertEqual(d.drawer_scroll, 3)
            d.handle_key(getattr(tui.curses, "KEY_BTAB", 353))
            self.assertEqual(d.right_tab, "upnext")
            d.handle_key("I")
            self.assertEqual(d.right_tab, "info")
            d.handle_key("D")
            self.assertEqual(d.right_tab, "downloads")
            d.handle_key("U")
            self.assertEqual(d.right_tab, "upnext")

            # Return to list pane and verify paging + batch shortcuts + Ctrl-L clear
            d.handle_key("h")
            self.assertEqual(d.focus_pane, "list")
            d.handle_key(tui.curses.KEY_NPAGE)
            self.assertEqual(d.selected_idx, 4)
            d.handle_key("G")
            self.assertEqual(d.selected_idx, 9)
            d.handle_key("g")
            self.assertEqual(d.selected_idx, 0)

            d.search_query = "pamungkas"
            d.handle_key(12)  # Ctrl-L
            self.assertEqual(d.search_query, "")

            for key_ch in ("P", "E", "W", "X"):
                d.handle_key(key_ch)
            self.assertIn(("play_all", None), actions)
            self.assertIn(("add_all", None), actions)
            self.assertIn(("dl_all", None), actions)
            self.assertIn(("dl_cancel", None), actions)
            self.assertFalse(d.handle_key("q"))

    def test_compute_layout_bounds_and_non_overlap_matrix(self):
        sizes = [
            (12, 44),
            (13, 64),
            (15, 50),
            (15, 70),
            (17, 80),
            (18, 100),
            (22, 100),
            (24, 80),
            (40, 140),
            (55, 100),
        ]
        for h, w in sizes:
            for mode in tui.LAYOUT_MODES:
                for show_drawer in (True, False):
                    lay = tui.compute_layout(h, w, mode, show_drawer)
                    rects = [
                        (name, lay[name])
                        for name in ("list", "now_playing", "drawer")
                        if lay.get(name) is not None
                    ]
                    self.assertGreaterEqual(len(rects), 1)
                    for name, (y0, rh, x0, rw) in rects:
                        ctx = f"{h}x{w} mode={mode} sd={show_drawer} pane={name}"
                        self.assertGreaterEqual(y0, 2, ctx)
                        self.assertLessEqual(y0 + rh, h - 4, ctx)
                        self.assertGreaterEqual(x0, 0, ctx)
                        self.assertLessEqual(x0 + rw, w, ctx)
                        self.assertGreaterEqual(rh, 4, ctx)
                        self.assertGreaterEqual(rw, 20, ctx)
                    for i in range(len(rects)):
                        n1, (y1, h1, x1, w1) = rects[i]
                        for j in range(i + 1, len(rects)):
                            n2, (y2, h2, x2, w2) = rects[j]
                            x_ov = x1 < (x2 + w2) and x2 < (x1 + w1)
                            y_ov = y1 < (y2 + h2) and y2 < (y1 + h1)
                            self.assertFalse(
                                x_ov and y_ov,
                                f"overlap {n1} vs {n2} at {h}x{w} mode={mode} sd={show_drawer}",
                            )

        lay_short = tui.compute_layout(12, 44, "auto", True)
        self.assertIsNone(lay_short["now_playing"])
        self.assertIsNone(lay_short["drawer"])
        self.assertLessEqual(lay_short["list"][0] + lay_short["list"][1], 8)

        lay_split_short = tui.compute_layout(15, 70, "split", True)
        self.assertIsNotNone(lay_split_short["drawer"])
        self.assertLessEqual(lay_split_short["drawer"][0] + lay_split_short["drawer"][1], 11)
        self.assertEqual(lay_split_short["now_playing"][1] + lay_split_short["drawer"][1], 9)

    def test_format_track_row_columns_wide_and_narrow(self):
        row_a = tui.format_track_row_columns("Short", "Artist A • Album X • 3:20", 72)
        row_b = tui.format_track_row_columns("東京の夜 Long Title Extended Edition", "Artist B • 4:05", 72)
        self.assertLessEqual(tui.display_width(row_a), 72)
        self.assertLessEqual(tui.display_width(row_b), 72)
        self.assertIn(" │ ", row_a)
        self.assertIn(" │ ", row_b)
        col_a = tui.display_width(row_a.split(" │ ", 1)[0])
        col_b = tui.display_width(row_b.split(" │ ", 1)[0])
        self.assertEqual(col_a, col_b)

        narrow = tui.format_track_row_columns("Title", "Artist • 3:20", 40)
        self.assertNotIn(" │ ", narrow)
        self.assertEqual(narrow, "Title — Artist • 3:20")
        self.assertLessEqual(tui.display_width(narrow), 40)

        no_meta = tui.format_track_row_columns("Only Title Here", "", 72)
        self.assertEqual(no_meta, "Only Title Here")
        self.assertNotIn(" │ ", no_meta)

    def test_render_drawer_card_child_buttons_precede_focus_hitbox(self):
        class StubStdscr:
            def getmaxyx(self):
                return (40, 140)

            def addstr(self, *_args, **_kwargs):
                return None

        def hit_action(dash, mx, my):
            hb = next((h for h in reversed(dash.hitboxes) if h.contains(mx, my)), None)
            return hb.action if hb else None

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            d.stdscr = StubStdscr()

            # downloads tab: Cancel All hitbox must win over background focus_pane
            d.right_tab = "downloads"
            d.dl_jobs = [{"id": "j1", "title": "Song 1", "status": "running", "pct": 42, "stage": "audio"}]
            d.hitboxes = []
            d.render_drawer_card(14, 16, 80, 60)
            cancel_hb = next(h for h in d.hitboxes if h.action == "dl_cancel")
            self.assertEqual(hit_action(d, cancel_hb.x0, cancel_hb.y0), "dl_cancel")
            self.assertEqual(hit_action(d, 130, 28), "focus_pane")

            # info tab: all action buttons must win over background focus_pane
            d.right_tab = "info"
            d.tracks = [("id123456789", "Song A", "Artist A • Album A • 3:10", "[Q]")]
            d.selected_idx = 0
            d.hitboxes = []
            d.render_drawer_card(14, 16, 80, 60)
            for act in ("play_sel", "add_sel", "radio_sel", "fav_sel", "dl_sel", "q_up", "q_down"):
                hb = next(h for h in d.hitboxes if h.action == act)
                self.assertEqual(hit_action(d, hb.x0, hb.y0), act)
            self.assertEqual(hit_action(d, 130, 28), "focus_pane")

            # upnext tab: queue row wins with jump, empty rows resolve to focus_pane
            d.right_tab = "upnext"
            d.mpv_state = {"idle-active": False, "playlist": [{"title": "Track 1", "current": True}]}
            d.hitboxes = []
            d.render_drawer_card(14, 16, 80, 60)
            jump_hb = next(h for h in d.hitboxes if h.action == "jump")
            self.assertEqual(hit_action(d, jump_hb.x0, jump_hb.y0), "jump")
            self.assertEqual(hit_action(d, 130, 28), "focus_pane")


if __name__ == "__main__":
    unittest.main()
