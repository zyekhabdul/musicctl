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
        dash = tui.SovereignDashboard(
            stdscr=None,
            art_enabled=False,
            mpv=unittest.mock.Mock(),
            dl_manager=unittest.mock.Mock(),
        )
        dash.active_tab = "home"
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

            p4 = downloader_mod.render_dest_path(
                dl_dir,
                "{title.__class__}/{artist[0]} - {title!r}",
                {"artist": "Pamungkas", "album": "Walk the Talk", "track_num": 1, "title": "To the Bone", "id": "safe1"},
                "flac",
            )
            self.assertEqual(p4, (dl_dir / "To the Bone.flac").resolve())
            self.assertNotIn("class", str(p4))

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

            bad_cover = root / "bad-cover.jpg"
            bad_cover.write_text("not-an-image", encoding="utf-8")
            spaced_meta = {"id": "t2", "title": "To the Bone", "artist": "Pamungkas & Friends", "album": "Walk the Talk"}
            fb_path, _ = downloader_mod.finalize_audio(
                src_flac, root / "fb_spaced.placeholder", quality="cd", fmt="flac", track=spaced_meta, cover_file=bad_cover, src_codec="flac",
            )
            fb_probe = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format_tags=title,artist,album", "-of", "json", str(fb_path)],
                text=True,
            ))
            fb_tags = {k.lower(): v for k, v in (fb_probe.get("format", {}).get("tags") or {}).items()}
            self.assertEqual(fb_tags.get("title"), "To the Bone")
            self.assertEqual(fb_tags.get("artist"), "Pamungkas & Friends")
            self.assertEqual(fb_tags.get("album"), "Walk the Talk")


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
        data_dir = td / "data"
        cache_dir = td / "cache"
        download_dir = td / "dl"
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        d = tui.SovereignDashboard(
            stdscr=None,
            data_dir=data_dir,
            cache_dir=cache_dir,
            download_dir=download_dir,
            art_enabled=False,
            mpv=unittest.mock.Mock(),
            dl_manager=unittest.mock.Mock(),
        )
        d.active_tab = "home"
        d.catalog_source = "ytm"
        d.eq_mode = "flat"
        d.norm_on = False
        d.quality_mode = "cd"
        d.autoplay_on = False
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
        self.assertEqual(seen_ua, ["musicctl/2.4"])

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
        self.assertTrue(all(ua == "musicctl/2.4" for ua in seen_ua))

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
        self.assertTrue(all(ua == "musicctl/2.4" for ua in seen_ua))

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
        d.last_list_body_h = 6
        d.last_drawer_body_h = 5
        return d

    def test_placeholder_lyrics_deferral(self):
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


class TestHardening(unittest.TestCase):
    def test_downloader_cross_process_flock_archive_and_jobs(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-flock-") as tmp:
            tdir = Path(tmp)
            archive_path = tdir / "download-archive.tsv"
            data_dir = tdir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            worker_code = (
                "import importlib.util, sys, threading\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('dl', sys.argv[1])\n"
                "dl = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(dl)\n"
                "wid = int(sys.argv[2])\n"
                "arch = Path(sys.argv[3])\n"
                "ddir = Path(sys.argv[4])\n"
                "lock = threading.Lock()\n"
                "for i in range(5):\n"
                "    tkey = f'w{wid}-t{i}'\n"
                "    dl.record_archive(arch, lock, tkey, 'cd', 'auto', ddir / f'{tkey}.flac')\n"
                "    dl._upsert_job(ddir, lock, {'job_id': f'job-{wid}-{i}', 'status': 'done', 'done': 1, 'total': 1})\n"
            )
            procs = [
                subprocess.Popen([sys.executable, "-c", worker_code, str(DOWNLOADER_PATH), str(w), str(archive_path), str(data_dir)])
                for w in range(3)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=15), 0)

            archive_map = downloader_mod.load_archive(archive_path)
            self.assertEqual(len(archive_map), 15)
            for w in range(3):
                for i in range(5):
                    self.assertIn((f"w{w}-t{i}", "cd", "auto"), archive_map)

            jobs = downloader_mod._load_jobs(data_dir)
            self.assertEqual(len(jobs), 15)
            self.assertEqual({j["job_id"] for j in jobs}, {f"job-{w}-{i}" for w in range(3) for i in range(5)})

    def test_mpv_resume_polls_until_ready_and_avoids_sticky_start(self):
        client = common_mod.MpvClient("/tmp/unused.sock", timeout=1.0)
        states = iter([
            {"idle-active": False, "playlist-pos": 1, "duration": None, "seekable": False},
            {"idle-active": False, "playlist-pos": 1, "duration": None, "seekable": False},
            {"idle-active": False, "playlist-pos": 1, "duration": 180.0, "seekable": True},
        ])
        with (
            mock.patch.object(client, "batch_send", return_value=(10, 10)),
            mock.patch.object(client, "batch_get", side_effect=lambda _props: next(states)),
            mock.patch.object(client, "send", return_value=True) as mock_send,
        ):
            cnt = client.resume_session(
                {"playlist": ["a.flac", "b.flac"], "playlist_pos": 1, "time_pos": 42.5},
                timeout=2.0,
            )
        self.assertEqual(cnt, 2)
        mock_send.assert_called_once_with(["seek", 42.5, "absolute"])

        # Race recovery: playlist-pos reset to 0 after async loadfile -> re-sends target_pos before seek
        race_states = iter([
            {"idle-active": False, "playlist-pos": 0, "playlist-count": 2, "duration": 10.0, "seekable": True},
            {"idle-active": False, "playlist-pos": 1, "playlist-count": 2, "duration": 180.0, "seekable": True},
        ])
        with (
            mock.patch.object(client, "batch_send", return_value=(10, 10)),
            mock.patch.object(client, "batch_get", side_effect=lambda _props: next(race_states)),
            mock.patch.object(client, "send", return_value=True) as mock_race_send,
        ):
            cnt_race = client.resume_session(
                {"playlist": ["a.flac", "b.flac"], "playlist_pos": 1, "time_pos": 42.5},
                timeout=2.0,
            )
        self.assertEqual(cnt_race, 2)
        self.assertEqual(
            mock_race_send.call_args_list,
            [
                mock.call(["set_property", "playlist-pos", 1]),
                mock.call(["seek", 42.5, "absolute"]),
            ],
        )

        with tempfile.TemporaryDirectory(prefix="musicctl-resume-") as tmp:
            sock = os.path.join(tmp, "mpv.sock")
            wav1 = os.path.join(tmp, "s1.wav")
            wav2 = os.path.join(tmp, "s2.wav")
            for wp in (wav1, wav2):
                with wave.open(wp, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(8000)
                    wf.writeframes(b"\x00\x00" * 8000 * 5)

            proc = subprocess.Popen(
                ["mpv", "--no-video", "--idle=yes", "--force-window=no", "--ao=null", f"--input-ipc-server={sock}"],
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
                    time.sleep(0.05)

                live_client = common_mod.MpvClient(sock, timeout=1.0)
                cnt2 = live_client.resume_session(
                    {"playlist": [wav1, wav2], "playlist_pos": 1, "time_pos": 2.2},
                    timeout=4.0,
                )
                self.assertEqual(cnt2, 2)
                st = live_client.batch_get(["playlist-pos", "time-pos"])
                self.assertIsNotNone(st)
                assert st is not None
                self.assertEqual(st["playlist-pos"], 1)
                self.assertGreaterEqual(float(st["time-pos"] or 0.0), 1.8)

                # Switching away and back must start at 0:00 (no sticky loadfile start= option).
                self.assertTrue(live_client.send(["set_property", "playlist-pos", 0]))
                time.sleep(0.1)
                self.assertTrue(live_client.send(["set_property", "playlist-pos", 1]))
                for _ in range(20):
                    st_back = live_client.batch_get(["playlist-pos", "time-pos", "idle-active"])
                    if st_back and st_back.get("idle-active") is False and st_back.get("playlist-pos") == 1 and st_back.get("time-pos") is not None:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(st_back)
                assert st_back is not None
                self.assertLess(float(st_back["time-pos"] or 0.0), 1.2)
                live_client.send(["quit"])
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_providers_resolve_batch_cache_provider_and_ytm_fallback(self):
        with tempfile.TemporaryDirectory(prefix="musicctl-batch-") as tmp:
            tdir = Path(tmp)
            pdir = tdir / "providers"
            cdir = tdir / "cache"
            pdir.mkdir()
            cdir.mkdir()

            cached_id = "dQw4w9WgXcQ"
            cached_file = cdir / f"{cached_id}.opus"
            cached_file.write_bytes(b"cached-opus-data")

            dz_cached_id = "abcDEF12345"
            dz_cached_file = cdir / f"{dz_cached_id}.flac"
            dz_cached_file.write_bytes(b"cached-flac-data")

            prov_target = tdir / "stream.flac"
            prov_target.write_bytes(b"mock-flac")
            prov_script = pdir / "10-mock.sh"
            prov_script.write_text(
                "#!/usr/bin/env bash\n"
                "req=$(cat)\n"
                "id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get(\"id\",\"\"))' \"$req\")\n"
                "if [ \"$id\" = \"ytmProv1234\" ]; then\n"
                f"  printf '{{\"provider\":\"mock\",\"url\":\"{prov_target}\",\"headers\":{{\"X-Auth\":\"tok\"}}}}\\n'\n"
                "fi\n",
                encoding="utf-8",
            )
            prov_script.chmod(0o755)

            items = [
                {"source": "ytm", "type": "album", "id": "MPREb_skip", "title": "Album Row"},
                {"source": "ytm", "type": "song", "id": cached_id, "title": "Cached Song", "artist": "Art A", "album": "Alb A", "duration": 185},
                {"source": "ytm", "type": "song", "id": "ytmProv1234", "title": "Prov Song", "artist": "Art B", "duration": 200},
                {"source": "ytm", "type": "song", "id": "ytmWatch123", "title": "Watch Song", "artist": "Art C", "duration": 90},
                {"source": "deezer", "type": "song", "id": "999", "title": "Deezer Song", "artist": "Art D", "album": "Alb D", "duration": 210},
            ]

            res = providers_mod.resolve_catalog_batch(
                items,
                "cd",
                pdir,
                cache_audio_dir=cdir,
                ytm_fallback_fn=lambda q: dz_cached_id if "Deezer Song" in q else "",
            )
            self.assertEqual(len(res), 4)
            self.assertEqual(res[0], {"url": str(cached_file), "title": "Cached Song", "headers": {}, "hid": cached_id, "meta": "Art A • Alb A • 03:05"})
            self.assertEqual(res[1], {"url": str(prov_target), "title": "Prov Song", "headers": {"X-Auth": "tok"}, "hid": "ytmProv1234", "meta": "Art B • 03:20"})
            self.assertEqual(res[2], {"url": "https://www.youtube.com/watch?v=ytmWatch123", "title": "Watch Song", "headers": {}, "hid": "ytmWatch123", "meta": "Art C • 01:30"})
            self.assertEqual(res[3], {"url": str(dz_cached_file), "title": "Deezer Song", "headers": {}, "hid": dz_cached_id, "meta": "Art D • Alb D • 03:30"})

            cli_proc = subprocess.run(
                [sys.executable, str(PROVIDERS_PATH), "resolve-batch", "cd", str(cdir)],
                input=json.dumps({"items": items[:4]}),
                text=True,
                capture_output=True,
                env={**os.environ, "MUSICCTL_PROVIDERS_DIR": str(pdir)},
                check=True,
            )
            lines = [json.loads(ln) for ln in cli_proc.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0]["url"], str(cached_file))
            self.assertEqual(lines[1]["headers"], {"X-Auth": "tok"})
            self.assertEqual(lines[2]["url"], "https://www.youtube.com/watch?v=ytmWatch123")


class TestWP4ARmpcFrameAndWidgets(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        return TestWP3AdaptiveHybridTui()._make_dash(td)

    def test_tui_widgets_exports_and_helpers(self):
        widgets_path = Path(__file__).resolve().parent.parent / "lib" / "tui_widgets.py"
        mod = sys.modules["tui_widgets"]
        self.assertIsNotNone(mod.__file__)
        assert mod.__file__ is not None
        self.assertEqual(Path(mod.__file__).resolve(), widgets_path.resolve())

        self.assertIs(tui.compute_rmpc_layout, mod.compute_rmpc_layout)
        self.assertIs(tui.Hitbox, mod.Hitbox)
        self.assertEqual(len(mod.RMPC_TABS), 8)

        self.assertEqual(mod.format_queue_summary([]), "0 songs")
        self.assertEqual(mod.format_queue_summary([{"duration": 185}]), "1 song / 03:05 total time")
        self.assertEqual(mod.format_queue_summary([{"duration": 60}, {"duration": 90}]), "2 songs / 02:30 total time")

        s_left, s_thumb, s_right = mod.build_volume_slider(50, 11)
        self.assertEqual(len(s_left) + len(s_thumb) + len(s_right), 11)
        self.assertEqual(s_thumb, "●")

        p_fill, p_empty = mod.build_progress_bar(30, 120, 20)
        self.assertEqual(len(p_fill) + len(p_empty), 20)
        self.assertEqual(p_fill, "█" * 5)
        self.assertEqual(mod.build_progress_bar(0, 0, 10), ("", "─" * 10))

    def test_rmpc_layout_matrix_bounds_and_non_overlap(self):
        sizes = [
            (12, 44),
            (15, 60),
            (17, 79),
            (18, 80),
            (18, 88),
            (24, 80),
            (24, 100),
            (40, 140),
            (60, 200),
        ]
        for h, w in sizes:
            for mode in tui.LAYOUT_MODES:
                for show_drawer in (True, False):
                    for search_bar in (True, False):
                        lay = tui.compute_rmpc_layout(h, w, mode, show_drawer, search_bar=search_bar)
                        self.assertEqual(lay["compact"], h < 18)
                        hboxes = lay["header_boxes"]
                        if not lay["compact"]:
                            self.assertIsNotNone(hboxes["left"])
                            self.assertIsNotNone(hboxes["right"])
                            if w >= 80:
                                self.assertIsNotNone(hboxes["center"])
                            else:
                                self.assertIsNone(hboxes["center"])

                        sibling_groups = [
                            [b for b in (hboxes["left"], hboxes["center"], hboxes["right"]) if b is not None],
                            [lay["footer_mode"], lay["footer_progress"]],
                            [r for r in (lay["header"], lay["tabs"], lay["search_bar"], lay["art"], lay["lyrics"], lay["list"], lay["drawer"], lay["hint_bar"], lay["footer"]) if r is not None],
                        ]
                        for rects in sibling_groups:
                            for y0, rh, x0, rw in rects:
                                self.assertGreaterEqual(y0, 0)
                                self.assertLessEqual(y0 + rh, h)
                                self.assertGreaterEqual(x0, 0)
                                self.assertLessEqual(x0 + rw, w)
                                self.assertGreater(rh, 0)
                                self.assertGreater(rw, 0)
                            for i in range(len(rects)):
                                y1, h1, x1, w1 = rects[i]
                                for j in range(i + 1, len(rects)):
                                    y2, h2, x2, w2 = rects[j]
                                    x_ov = x1 < (x2 + w2) and x2 < (x1 + w1)
                                    y_ov = y1 < (y2 + h2) and y2 < (y1 + h1)
                                    self.assertFalse(x_ov and y_ov, f"overlap at {h}x{w} mode={mode}")
                        self.assertGreaterEqual(lay["list"][1], 4)

    def test_rmpc_chrome_render_hitboxes_and_tab_navigation(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h = h
                self.w = w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

            def row_text(self, y: int) -> str:
                buf = [" "] * self.w
                for wy, wx, txt in self.writes:
                    if wy == y:
                        for i, ch in enumerate(txt):
                            if 0 <= wx + i < self.w:
                                buf[wx + i] = ch
                return "".join(buf)

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            d.mpv_state = {
                "idle-active": False,
                "pause": False,
                "media-title": "To the Bone",
                "metadata": {"artist": "Pamungkas", "album": "Walk the Talk", "date": "2018"},
                "time-pos": 65.0,
                "duration": 225.0,
                "volume": 80,
                "mute": False,
                "audio-codec-name": "opus",
                "audio-params/samplerate": 48000,
                "playlist": [{"title": "To the Bone", "duration": 225.0, "current": True}],
            }
            d.tracks = [("id1", "To the Bone", "Pamungkas • 3:45", "[Q]")]

            for h, w in ((40, 140), (24, 72), (12, 44)):
                scr = RecordingStdscr(h, w)
                d.stdscr = scr
                d.active_tab = "queue"
                d.render()
                for wy, wx, wtxt in scr.writes:
                    self.assertGreaterEqual(wy, 0)
                    self.assertLess(wy, h)
                    self.assertGreaterEqual(wx, 0)
                    self.assertLessEqual(wx + tui.display_width(wtxt), w, f"overflow at {h}x{w}: y={wy} x={wx} txt={wtxt!r}")

                actions = {hb.action for hb in d.hitboxes}
                self.assertIn("toggle", actions)
                self.assertIn("seek_bar", actions)
                self.assertIn("help", actions)
                tab_payloads = {hb.payload for hb in d.hitboxes if hb.action == "tab"}
                self.assertEqual(tab_payloads, {k for k, _ in tui.RMPC_TABS})
                if h >= 18:
                    self.assertIn("vol_bar", actions)
                    for flag_act in ("loop", "shuffle", "eq", "norm", "autoplay", "mute"):
                        self.assertIn(flag_act, actions)

            # Keyboard 1-8 + Tab/Shift-Tab on list focus, and / switches to search
            d.focus_pane = "list"
            d.handle_key("8")
            self.assertEqual(d.active_tab, "lyrics")
            d.handle_key(9)
            self.assertEqual(d.active_tab, "queue")
            d.handle_key(getattr(tui.curses, "KEY_BTAB", 353))
            self.assertEqual(d.active_tab, "lyrics")
            d.handle_key("/")
            self.assertEqual(d.active_tab, "search")
            self.assertTrue(d.search_focus)


class TestWP4BTableScrollbarAndQueueSplit(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        return TestWP3AdaptiveHybridTui()._make_dash(td)

    def test_4col_table_widths_unicode_cjk_and_narrow_fallback(self):
        self.assertIsNone(tui.compute_table_col_widths(53))
        cols = tui.compute_table_col_widths(90)
        self.assertIsNotNone(cols)
        assert cols is not None
        self.assertEqual(sum(cols) + 6, 90)

        hdr = tui.format_table_header(90)
        self.assertLessEqual(tui.display_width(hdr), 90)
        for col_name in ("Artist", "Title", "Album", "Duration"):
            self.assertIn(col_name, hdr)

        row_cjk = tui.format_table_row_4col("米津玄師 Kenshi Yonezu", "Lemon (東京ライブ)", "STRAY SHEEP", "04:15", 90)
        self.assertLessEqual(tui.display_width(row_cjk), 90)
        self.assertTrue(row_cjk.endswith("  04:15"))

        narrow = tui.format_table_row_4col("Pamungkas", "To the Bone", "Walk the Talk", "05:45", 42)
        self.assertLessEqual(tui.display_width(narrow), 42)
        self.assertIn("To the Bone", narrow)

        art, ttl, alb, dur = tui.parse_track_columns("Pamungkas - Monolog", "Walk the Talk • 03:30 • Active", "[LOC]")
        self.assertEqual((art, ttl, alb, dur), ("Walk the Talk", "Pamungkas - Monolog", "-", "03:30"))
        art2, ttl2, alb2, dur2 = tui.parse_track_columns("Pamungkas - Monolog", "Local Audio", "[LOC]")
        self.assertEqual((art2, ttl2, alb2, dur2), ("Pamungkas", "Monolog", "-", "-"))

    def test_vertical_scrollbar_geometry(self):
        self.assertEqual(tui.build_vertical_scrollbar(10, 10, 0, 5), ["│"] * 5)
        self.assertEqual(tui.build_vertical_scrollbar(0, 5, 0, 4), ["│"] * 4)
        self.assertEqual(tui.build_vertical_scrollbar(20, 5, 0, 1), ["█"])
        self.assertEqual(tui.build_vertical_scrollbar(20, 5, 0, 2), ["▲", "▼"])

        sb_top = tui.build_vertical_scrollbar(40, 10, 0, 10)
        sb_bot = tui.build_vertical_scrollbar(40, 10, 30, 10)
        self.assertEqual(len(sb_top), 10)
        self.assertEqual(sb_top[0], "▲")
        self.assertEqual(sb_top[-1], "▼")
        self.assertEqual(sb_top[1], "█")
        self.assertEqual(sb_bot[-2], "█")

    def test_queue_35_65_split_and_entity_full_width_render(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h = h
                self.w = w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

        lay_q = tui.compute_rmpc_layout(40, 140, "auto", True, active_tab="queue")
        self.assertEqual(lay_q["effective"], "split")
        self.assertIsNotNone(lay_q["art"])
        self.assertIsNotNone(lay_q["lyrics"])
        self.assertIsNone(lay_q["drawer"])
        self.assertEqual(lay_q["art"][3], 49)
        self.assertEqual(lay_q["list"][2:], (49, 91))

        lay_ent = tui.compute_rmpc_layout(40, 140, "auto", True, active_tab="local")
        self.assertEqual(lay_ent["effective"], "focus")
        self.assertIsNone(lay_ent["art"])
        self.assertIsNone(lay_ent["lyrics"])
        self.assertEqual(lay_ent["list"][2:], (0, 140))

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            d.mpv_state = {
                "idle-active": False,
                "pause": False,
                "media-title": "To the Bone",
                "metadata": {"artist": "Pamungkas", "album": "Walk the Talk", "date": "2018"},
                "time-pos": 30.0,
                "duration": 225.0,
                "volume": 80,
                "mute": False,
                "playlist": [{"title": f"Track {i}", "duration": 200.0, "current": (i == 0)} for i in range(40)],
            }
            d.tracks = [(f"id{i:09d}", f"Track {i}", f"Artist {i} • Album {i} • 03:20", "[Q]") for i in range(40)]
            d.lyrics_header = "To the Bone - Pamungkas"
            d.lyrics_lines = [(float(i * 3), f"Lyric line {i}") for i in range(30)]

            for tab, (h, w) in (("queue", (40, 140)), ("queue", (24, 88)), ("local", (40, 140)), ("lyrics", (24, 72))):
                scr = RecordingStdscr(h, w)
                d.stdscr = scr
                d.active_tab = tab
                d.render()
                joined = "\n".join(txt for _y, _x, txt in scr.writes)
                for wy, wx, wtxt in scr.writes:
                    self.assertGreaterEqual(wy, 0)
                    self.assertLess(wy, h)
                    self.assertGreaterEqual(wx, 0)
                    self.assertLessEqual(wx + tui.display_width(wtxt), w, f"overflow {tab} {h}x{w}: {wy},{wx} {wtxt!r}")
                if tab == "queue":
                    self.assertIn("Album Art", joined)
                    self.assertIn("Lyrics", joined)
                    self.assertIn("█", joined)
                    if w >= 100:
                        self.assertIn("Duration", joined)
                    else:
                        self.assertIn("Track 0", joined)
                elif tab == "local":
                    self.assertIn("Artist", joined)
                    self.assertIn("Duration", joined)
                elif tab == "lyrics":
                    self.assertIn("Lyric line", joined)


class TestWP4CEntityTabsAutoRequeryAndModals(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        return TestWP3AdaptiveHybridTui()._make_dash(td)

    def test_entity_aggregation_artists_albums_and_merged_history(self):
        favs = [("vid1", "Song Fav", "Artist A • Album X • 03:00", "[FAV]")]
        hist = [
            ("vid1", "Song Fav", "Artist A • Album X • 03:00", "[HIS]"),
            ("vid2", "Song Hist", "Artist B • Album X • 02:30", "[HIS]"),
        ]
        merged = tui.merge_fav_and_history(favs, hist)
        self.assertEqual([r[0] for r in merged], ["vid1", "vid2"])
        self.assertEqual(merged[0][3], "[FAV]")

        local_entries = [
            {"path": "/music/a.flac", "title": "Song Loc", "artist": "Artist A", "album": "Album Y", "duration": 200, "codec": "flac"},
        ]
        art_rows, art_groups = tui.aggregate_artists(local_entries, favs, hist)
        self.assertEqual([(r[1], r[2], r[3]) for r in art_rows], [("Artist A", "2 tracks", "[E-ART]"), ("Artist B", "1 tracks", "[E-ART]")])
        self.assertEqual(len(art_groups["eart:Artist A"]), 2)

        alb_rows, alb_groups = tui.aggregate_albums(local_entries, favs, hist)
        self.assertEqual(
            [(r[1], r[2], r[3]) for r in alb_rows],
            [("Album X", "Various Artists • 2 tracks", "[E-ALB]"), ("Album Y", "Artist A • 1 tracks", "[E-ALB]")],
        )
        self.assertEqual(len(alb_groups["ealb:Album X"]), 2)

        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.data_dir.mkdir(parents=True, exist_ok=True)
            loc_f = d.download_dir / "Artist A" / "Album Y" / "01 - Song Loc.flac"
            loc_f.parent.mkdir(parents=True, exist_ok=True)
            loc_f.write_bytes(b"fake-flac")
            (d.data_dir / "favorites.tsv").write_text("vid1\tSong Fav\tArtist A • Album X • 03:00\n", encoding="utf-8")
            (d.data_dir / "history.tsv").write_text("vid1\tSong Fav\tArtist A • Album X • 03:00\nvid2\tSong Hist\tArtist B • Album X • 02:30\n", encoding="utf-8")
            pl_dir = d.data_dir / "playlists"
            pl_dir.mkdir(parents=True, exist_ok=True)
            (pl_dir / "chill.tsv").write_text("vid1\tSong Fav\tArtist A • Album X • 03:00\n", encoding="utf-8")

            d.handle_action("tab", "artists")
            self.assertEqual([r[1] for r in d.tracks], ["Artist A", "Artist B"])
            d.selected_idx = 0
            d.play_selected()  # drill into Artist A
            self.assertEqual(len(d.nav_stack), 1)
            self.assertEqual(d.drill_ref, "eart:Artist A")
            self.assertEqual(len(d.tracks), 2)
            self.assertTrue(d.pop_nav())
            self.assertEqual([r[1] for r in d.tracks], ["Artist A", "Artist B"])

            d.handle_action("tab", "albums")
            self.assertEqual([r[1] for r in d.tracks], ["Album X", "Album Y"])
            d.handle_action("tab", "playlists")
            self.assertEqual([r[0] for r in d.tracks], ["pl:chill"])
            d.handle_action("tab", "hist")
            self.assertEqual([(r[0], r[3]) for r in d.tracks], [("vid1", "[FAV]"), ("vid2", "[HIS]")])

    def test_search_scope_and_catalog_auto_requery_and_isolation(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

            def row_text(self, y: int) -> str:
                buf = [" "] * self.w
                for wy, wx, txt in self.writes:
                    if wy == y:
                        for i, ch in enumerate(txt):
                            if 0 <= wx + i < self.w:
                                buf[wx + i] = ch
                return "".join(buf)

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            searched: list[tuple[str, str, str]] = []
            d.trigger_search = lambda q: searched.append((q, d.catalog_source, d.search_type))  # type: ignore[method-assign]

            # Search bar must NOT render outside tab 6 Search
            scr = RecordingStdscr(40, 140)
            d.stdscr = scr
            d.active_tab = "queue"
            d.search_focus = False
            d.render()
            self.assertNotIn("C:ytm", "\n".join(t for _, _, t in scr.writes))

            # focus_search switches to search tab and focuses input
            d.handle_action("focus_search", None)
            self.assertEqual(d.active_tab, "search")
            self.assertTrue(d.search_focus)
            d.render()
            self.assertIn("C:ytm", "\n".join(t for _, _, t in scr.writes))

            # Empty query does not auto-requery
            d.search_query = ""
            d.handle_action("set_search_type", "albums")
            self.assertEqual(searched, [])

            # Non-empty query immediately triggers search on scope/catalog change
            d.search_query = "pamungkas"
            d.handle_action("set_search_type", "artists")
            d.handle_action("cycle_search_type", None)
            d.handle_action("cycle_catalog", None)
            self.assertEqual(
                searched,
                [
                    ("pamungkas", "ytm", "artists"),
                    ("pamungkas", "ytm", "playlists"),
                    ("pamungkas", "deezer", "playlists"),
                ],
            )

    def test_dl_and_info_overlay_modals_and_hitbox_priority(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

        def hit_action(dash, mx: int, my: int) -> str | None:
            hb = next((h for h in reversed(dash.hitboxes) if h.contains(mx, my)), None)
            return hb.action if hb else None

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            scr = RecordingStdscr(32, 120)
            d.stdscr = scr
            d.active_tab = "queue"
            d.tracks = [("id123456789", "To the Bone", "Pamungkas • Walk the Talk • 05:45", "[Q]")]
            d.dl_jobs = [{"job_id": "j1", "title": "To the Bone", "stage": "fetching", "pct": 55, "status": "running"}]
            d.last_layout = tui.compute_rmpc_layout(32, 120, "auto", True, active_tab="queue")
            self.assertIsNone(d.last_layout["drawer"])

            # D opens downloads modal; child buttons beat noop/close_modal; Esc closes
            d.handle_key("D")
            self.assertEqual(d.active_modal, "downloads")
            d.render()
            cancel_hb = next(h for h in d.hitboxes if h.action == "dl_cancel")
            close_hb = next(h for h in d.hitboxes if h.action == "close_modal" and h.y0 > 0)
            self.assertEqual(hit_action(d, cancel_hb.x0, cancel_hb.y0), "dl_cancel")
            self.assertEqual(hit_action(d, close_hb.x0, close_hb.y0), "close_modal")
            self.assertEqual(hit_action(d, 0, 0), "close_modal")
            d.handle_key(27)
            self.assertEqual(d.active_modal, "")

            # I opens info modal; action buttons resolve cleanly; Esc closes
            d.handle_key("I")
            self.assertEqual(d.active_modal, "info")
            d.render()
            for act in ("play_sel", "add_sel", "radio_sel", "fav_sel", "dl_sel", "q_up", "q_down"):
                hb = next(h for h in d.hitboxes if h.action == act)
                self.assertEqual(hit_action(d, hb.x0, hb.y0), act)
            d.handle_key(27)
            self.assertEqual(d.active_modal, "")


class TestWP4DHybridKeymapAndHitboxAudit(unittest.TestCase):
    def _make_dash(self, td: Path) -> tui.SovereignDashboard:
        return TestWP3AdaptiveHybridTui()._make_dash(td)

    def test_rmpc_hybrid_keyboard_aliases_and_modal_paging(self):
        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            actions: list[tuple[str, object]] = []
            mpv_cmds: list[list[object]] = []
            moves: list[int] = []
            d.handle_action = lambda act, payload=None, bstate=0: actions.append((act, payload))  # type: ignore[method-assign]
            d.mpv = unittest.mock.Mock()
            d.mpv.send.side_effect = lambda cmd: mpv_cmds.append(list(cmd)) or True
            d.move_queue_selected = lambda delta: moves.append(delta)  # type: ignore[method-assign]

            for key, expected in (
                ("p", ("toggle", None)),
                (" ", ("toggle", None)),
                (">", ("next", None)),
                ("n", ("next", None)),
                ("<", ("prev", None)),
                ("b", ("prev", None)),
                (".", ("vol_delta", 5)),
                ("+", ("vol_delta", 5)),
                (",", ("vol_delta", -5)),
                ("-", ("vol_delta", -5)),
                ("s", ("stop", None)),
                ("x", ("stop", None)),
                ("o", ("loop", None)),
                ("u", ("shuffle", None)),
                ("v", ("cycle_layout", None)),
                ("z", ("toggle_drawer", None)),
            ):
                actions.clear()
                d.handle_key(key)
                self.assertEqual(actions, [expected], f"key={key!r}")

            d.active_tab = "queue"
            d.handle_key("J")
            d.handle_key("K")
            self.assertEqual(moves, [1, -1])

            d.handle_key("[")
            d.handle_key("]")
            self.assertEqual(mpv_cmds, [["seek", -10, "relative"], ["seek", 10, "relative"]])

        # Modal paging via real handle_action for downloads modal
        with tempfile.TemporaryDirectory() as td:
            d = self._make_dash(Path(td))
            d.last_layout = {"drawer": None}
            d.handle_key("D")
            self.assertEqual(d.active_modal, "downloads")
            d.drawer_scroll = 0
            d.handle_key(getattr(tui.curses, "KEY_NPAGE", 338))
            self.assertEqual(d.drawer_scroll, 6)
            d.handle_key(4)  # Ctrl-D
            self.assertEqual(d.drawer_scroll, 12)
            d.handle_key(getattr(tui.curses, "KEY_PPAGE", 339))
            self.assertEqual(d.drawer_scroll, 6)
            d.handle_key(21)  # Ctrl-U
            self.assertEqual(d.drawer_scroll, 0)
            d.handle_key("G")
            self.assertGreater(d.drawer_scroll, 0)
            d.handle_key("g")
            self.assertEqual(d.drawer_scroll, 0)
            d.handle_key(27)
            self.assertEqual(d.active_modal, "")

    def test_hitbox_priority_across_header_tabs_table_search_footer_and_wheel(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

        def resolve_hitbox(hitboxes: list[tui.Hitbox], mx: int, my: int) -> tui.Hitbox | None:
            for hb in reversed(hitboxes):
                if hb.contains(mx, my):
                    return hb
            return None

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            d = self._make_dash(Path(td))
            scr = RecordingStdscr(40, 140)
            d.stdscr = scr
            d.active_tab = "queue"
            d.mpv_state = {
                "idle-active": False,
                "pause": False,
                "media-title": "To the Bone",
                "metadata": {"artist": "Pamungkas", "album": "Walk the Talk", "date": "2018"},
                "time-pos": 30.0,
                "duration": 225.0,
                "volume": 80,
                "mute": False,
                "audio-codec-name": "opus",
                "audio-params/samplerate": 48000,
                "playlist": [{"title": f"Track {i}", "duration": 200.0, "current": (i == 0)} for i in range(6)],
            }
            d.tracks = [(f"id{i:09d}", f"Track {i}", f"Artist {i} • Album {i} • 03:20", "[Q]") for i in range(6)]
            d.lyrics_header = "To the Bone - Pamungkas"
            d.lyrics_lines = [(float(i * 3), f"Lyric line {i}") for i in range(40)]
            d.render()

            for act in ("toggle", "vol_bar", "mute", "loop", "shuffle", "eq", "norm", "autoplay", "help", "seek_bar"):
                hb = next(h for h in d.hitboxes if h.action == act)
                resolved = resolve_hitbox(d.hitboxes, (hb.x0 + hb.x1) // 2, hb.y0)
                self.assertIsNotNone(resolved, f"missing reverse hit for {act}")
                assert resolved is not None
                self.assertEqual(resolved.action, act)

            for tab_key, _ in tui.RMPC_TABS:
                hb = next(h for h in d.hitboxes if h.action == "tab" and h.payload == tab_key)
                resolved = resolve_hitbox(d.hitboxes, (hb.x0 + hb.x1) // 2, hb.y0)
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual((resolved.action, resolved.payload), ("tab", tab_key))

            row0 = next(h for h in d.hitboxes if h.action == "row" and h.payload == 0)
            res_row0 = resolve_hitbox(d.hitboxes, row0.x0 + 2, row0.y0)
            self.assertIsNotNone(res_row0)
            assert res_row0 is not None
            self.assertEqual((res_row0.action, res_row0.payload), ("row", 0))

            ly, lh, lx, lw = d.last_layout["list"]
            res_empty = resolve_hitbox(d.hitboxes, lx + 2, ly + lh - 2)
            self.assertIsNotNone(res_empty)
            assert res_empty is not None
            self.assertEqual((res_empty.action, res_empty.payload), ("focus_pane", "list"))

            lyr_y, lyr_h, lyr_x, lyr_w = d.last_layout["lyrics"]
            res_lyr = resolve_hitbox(d.hitboxes, lyr_x + 2, lyr_y + lyr_h - 2)
            self.assertIsNotNone(res_lyr)
            assert res_lyr is not None
            self.assertEqual((res_lyr.action, res_lyr.payload), ("focus_pane", "drawer"))

            # Wheel routing: over Queue lyrics pane scrolls drawer_scroll without changing selected_idx
            d.drawer_scroll = 0
            d.selected_idx = 0
            d.handle_mouse_wheel(lyr_x + 2, lyr_y + 2, 3)
            self.assertEqual(d.drawer_scroll, 3)
            self.assertEqual(d.selected_idx, 0)
            # Over list pane scrolls selected_idx
            d.handle_mouse_wheel(lx + 2, row0.y0, 3)
            self.assertEqual(d.selected_idx, 3)
            # While modal open, wheel scrolls drawer_scroll regardless of cursor position
            d.active_modal = "downloads"
            d.drawer_scroll = 1
            d.handle_mouse_wheel(lx + 2, row0.y0, 3)
            self.assertEqual(d.drawer_scroll, 4)
            d.active_modal = ""

            # Search root hitbox priority audit
            d.active_tab = "search"
            d.nav_stack = []
            d.render()
            for act in ("cycle_catalog", "set_search_type", "focus_search", "do_search", "clear_search", "row"):
                hb = next(h for h in d.hitboxes if h.action == act)
                resolved = resolve_hitbox(d.hitboxes, (hb.x0 + hb.x1) // 2, hb.y0)
                self.assertIsNotNone(resolved, f"missing reverse hit for {act}")
                assert resolved is not None
                self.assertEqual(resolved.action, act)

            # Drill-down context bar replaces search bar and keeps table border clean
            d.nav_stack = [{"active_tab": "search", "tracks": []}]
            d.drill_title = "Album X"
            d.render()
            for act in ("nav_back", "play_all", "add_all", "dl_all", "row"):
                hb = next(h for h in d.hitboxes if h.action == act)
                resolved = resolve_hitbox(d.hitboxes, (hb.x0 + hb.x1) // 2, hb.y0)
                self.assertIsNotNone(resolved, f"missing reverse hit for {act}")
                assert resolved is not None
                self.assertEqual(resolved.action, act)

            # Help modal renders updated rmpc shortcuts cleanly without overflow
            d.nav_stack = []
            d.show_help = True
            d.render()
            help_text = "\n".join(txt for _y, _x, txt in scr.writes)
            self.assertIn("HYBRID RMPC", help_text)
            self.assertIn("Space / p", help_text)
            self.assertIn("< / >", help_text)
            self.assertIn(", / .", help_text)
            help_close = resolve_hitbox(d.hitboxes, 0, 0)
            self.assertIsNotNone(help_close)
            assert help_close is not None
            self.assertEqual(help_close.action, "close_help")


class TestAntiMonkeypatchRegressions(unittest.TestCase):
    def test_constructor_di_initializes_full_dashboard_without_new(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub_mpv = unittest.mock.Mock()
            stub_dl = unittest.mock.Mock()
            d = tui.SovereignDashboard(
                stdscr=None,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=stub_mpv,
                dl_manager=stub_dl,
            )
            self.assertEqual(d.data_dir, root / "data")
            self.assertEqual(d.cache_dir, root / "cache")
            self.assertEqual(d.download_dir, root / "dl")
            self.assertFalse(d.art_enabled)
            self.assertIs(d.mpv, stub_mpv)
            self.assertIs(d.dl_manager, stub_dl)
            self.assertEqual(d.active_tab, "queue")
            self.assertEqual(d.active_modal, "")
            self.assertEqual(d._entity_groups, {})
            self.assertEqual(d.last_layout, {})

    def test_queue_split_autoloads_lyrics_and_art_size_parity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub_mpv = unittest.mock.Mock()
            stub_mpv.batch_get.return_value = {
                "idle-active": False,
                "path": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "media-title": "To the Bone",
                "metadata": {"artist": "Pamungkas", "album": "Walk the Talk"},
                "duration": 225.0,
            }
            d = tui.SovereignDashboard(
                stdscr=None,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=True,
                mpv=stub_mpv,
                dl_manager=unittest.mock.Mock(),
            )
            d.data_dir.mkdir(parents=True, exist_ok=True)
            d.active_tab = "queue"
            d.right_tab = "upnext"
            d.last_layout = tui.compute_rmpc_layout(40, 140, "auto", True, active_tab="queue")
            expected_art = tui.calc_queue_art_size(d.last_layout["art"])
            self.assertIsNotNone(expected_art)
            self.assertNotEqual(expected_art, (22, 10))
            self.assertTrue(d.is_lyrics_visible())

            fetched_lyrics: list[tuple[str, str, str, float]] = []
            fetched_art: list[tuple[str, int, int]] = []
            d.fetch_lyrics_async = lambda t, p, artist="", duration=0.0: fetched_lyrics.append((t, p, artist, duration))  # type: ignore[method-assign]
            d.ensure_art_async = lambda vid, tw, th: fetched_art.append((vid, tw, th))  # type: ignore[method-assign]

            d.poll_mpv()
            self.assertEqual(
                fetched_lyrics,
                [("To the Bone", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Pamungkas", 225.0)],
            )
            self.assertEqual(fetched_art, [("dQw4w9WgXcQ", expected_art[0], expected_art[1])])

            d.last_layout = tui.compute_rmpc_layout(40, 140, "auto", True, active_tab="local")
            d.active_tab = "local"
            self.assertFalse(d.is_lyrics_visible())
            self.assertIsNone(tui.calc_queue_art_size(d.last_layout["art"]))

    def test_dl_sel_dl_all_entity_groups_and_multi_ref_resolver(self):
        self.assertEqual(tui.classify_row("eart:Pamungkas", "[E-ART]"), "entity_group")
        self.assertEqual(tui.classify_row("lart:Pamungkas", "[L-ART]"), "local_group")
        self.assertEqual(tui.classify_row("pl:chill", "[PL]"), "playlist")
        self.assertEqual(tui.classify_row("deezer:album:12", "[ALB]"), "catalog_container")
        self.assertEqual(tui.classify_row("deezer:song:34", "[DEEZER]"), "catalog_track")
        self.assertEqual(tui.classify_row("dQw4w9WgXcQ", "[Q]"), "queue_item")
        self.assertEqual(tui.classify_row("/tmp/song.flac", "[LOC]"), "local_file")
        self.assertEqual(tui.classify_row("dQw4w9WgXcQ", "[YTM]"), "track")

        self.assertFalse(tui.is_downloadable_ref("eart:Pamungkas", "[E-ART]"))
        self.assertFalse(tui.is_downloadable_ref("/tmp/song.flac", "[LOC]"))
        self.assertTrue(tui.is_downloadable_ref("dQw4w9WgXcQ", "[FAV]"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            started: list[tuple[list[str], str]] = []
            stub_dl = unittest.mock.Mock()
            stub_dl.start.side_effect = lambda args, label="": started.append((list(args), label)) or "dlm-1"

            d = tui.SovereignDashboard(
                stdscr=None,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=unittest.mock.Mock(),
                dl_manager=stub_dl,
            )
            d.last_layout = tui.compute_rmpc_layout(40, 140, "auto", True, active_tab="artists")
            d._entity_groups = {
                "eart:Pamungkas": [
                    ("/tmp/local.flac", "Local Song", "Pamungkas", "[LOC]"),
                    ("dQw4w9WgXcQ", "Song 1", "Pamungkas", "[FAV]"),
                    ("abcDEF12345", "Song 2", "Pamungkas", "[HIS]"),
                ],
                "eart:LocalOnly": [
                    ("/tmp/only.flac", "Only Local", "LocalOnly", "[LOC]"),
                ],
            }
            d.tracks = [
                ("eart:Pamungkas", "Pamungkas", "3 tracks", "[E-ART]"),
                ("eart:LocalOnly", "LocalOnly", "1 tracks", "[E-ART]"),
            ]
            d.selected_idx = 0
            d.handle_action("dl_sel", None)
            self.assertEqual(started, [(["dQw4w9WgXcQ", "abcDEF12345"], "Pamungkas")])
            self.assertIn("2 track(s)", d.toast_msg)

            started.clear()
            d.selected_idx = 1
            d.handle_action("dl_sel", None)
            self.assertEqual(started, [])
            self.assertEqual(d.toast_msg, "Already local")

            # Drill into eart:Pamungkas and press W (dl_all) when drawer is None -> opens downloads modal + toast
            d.selected_idx = 0
            d.play_selected()
            d.handle_action("dl_all", None)
            self.assertEqual(started, [(["dQw4w9WgXcQ", "abcDEF12345"], "Pamungkas")])
            self.assertEqual(d.right_tab, "downloads")
            self.assertEqual(d.active_modal, "downloads")
            self.assertIn("2 track(s)", d.toast_msg)

            resolved = downloader_mod.resolve_download_targets(["dQw4w9WgXcQ", "abcDEF12345"], d.data_dir, "/tmp/unused.sock")
            self.assertEqual([t["id"] for t in resolved], ["dQw4w9WgXcQ", "abcDEF12345"])

    def test_help_modal_fits_24x80_without_vertical_or_horizontal_clip(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

            def row_text(self, y: int) -> str:
                buf = [" "] * self.w
                for wy, wx, txt in self.writes:
                    if wy == y:
                        for i, ch in enumerate(txt):
                            if 0 <= wx + i < self.w:
                                buf[wx + i] = ch
                return "".join(buf)

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            root = Path(td)
            scr = RecordingStdscr(24, 80)
            d = tui.SovereignDashboard(
                stdscr=scr,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=unittest.mock.Mock(),
                dl_manager=unittest.mock.Mock(),
            )
            d.show_help = True
            d.render()
            for wy, wx, wtxt in scr.writes:
                self.assertGreaterEqual(wy, 0)
                self.assertLess(wy, 24)
                self.assertGreaterEqual(wx, 0)
                self.assertLessEqual(wx + tui.display_width(wtxt), 80, f"overflow: {wy},{wx} {wtxt!r}")
            help_text = "\n".join(txt for _y, _x, txt in scr.writes)
            self.assertIn("HYBRID RMPC", help_text)
            self.assertIn("J/K  o u Q e N A m ? q", help_text)


class TestUXClarityPolish(unittest.TestCase):
    def test_honest_mode_pill_drilldown_context_bar_and_footer_timeline(self):
        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

            def row_text(self, y: int) -> str:
                buf = [" "] * self.w
                for wy, wx, txt in self.writes:
                    if wy == y:
                        for i, ch in enumerate(txt):
                            if 0 <= wx + i < self.w:
                                buf[wx + i] = ch
                return "".join(buf)

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            root = Path(td)
            scr = RecordingStdscr(40, 120)
            d = tui.SovereignDashboard(
                stdscr=scr,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=unittest.mock.Mock(),
                dl_manager=unittest.mock.Mock(),
            )
            d.mpv_state = {
                "idle-active": False,
                "pause": True,
                "media-title": "Shut It Down",
                "metadata": {"artist": "Drake", "album": "Thank Me Later"},
                "time-pos": 67.0,
                "duration": 419.0,
                "volume": 100,
                "mute": False,
                "audio-codec-name": "opus",
                "audio-params/samplerate": 48000,
                "playlist": [{"title": "Shut It Down", "duration": 419.0, "current": True}],
            }
            d.tracks = [("1", "When We Were Younger", "grentperez • When We Were Younger • 03:54", "[YTM]")]

            # 1. On 6 Search with search_focus=False & active drill-down:
            #    mode pill must say NORMAL (not SEARCH), context bar replaces scope pills,
            #    header shows readable Vol + Loop/Shuf/EQ/Norm/Auto + [<]/[>], footer shows 01:07 .. ● .. 06:59.
            d.active_tab = "search"
            d.search_focus = False
            d.catalog_source = "deezer"
            d.drill_title = "When We Were Younger"
            d.drill_kind = "browse"
            d.nav_stack = [{"active_tab": "search", "tracks": []}]
            d.render()
            for wy, wx, wtxt in scr.writes:
                self.assertGreaterEqual(wy, 0)
                self.assertLess(wy, 40)
                self.assertGreaterEqual(wx, 0)
                self.assertLessEqual(wx + tui.display_width(wtxt), 120)

            text = "\n".join(scr.row_text(r) for r in range(40))
            self.assertIn(" NORMAL ", text)
            self.assertNotIn(" SEARCH ", text)
            self.assertNotIn("[Albums]", text)
            self.assertIn("[Bksp <-]", text)
            self.assertIn("[P Play All]", text)
            self.assertIn("Vol ", text)
            self.assertIn("Loop", text)
            self.assertIn("Shuf", text)
            self.assertIn("01:07", text)
            self.assertIn("06:59", text)
            self.assertIn("Enter Play", text)
            acts = {hb.action for hb in d.hitboxes}
            for req_act in ("prev", "toggle", "next", "vol_bar", "mute", "loop", "shuffle", "eq", "norm", "autoplay", "help", "nav_back", "play_all", "seek_bar"):
                self.assertIn(req_act, acts)

            # 2. Focusing search input switches mode pill to INPUT and context hints to typing mode
            d.nav_stack = []
            d.search_focus = True
            d.render()
            text_in = "\n".join(scr.row_text(r) for r in range(40))
            self.assertIn(" INPUT ", text_in)
            self.assertIn("[Albums]", text_in)
            self.assertIn("Esc Done", text_in)

    def test_context_hints_variants(self):
        self.assertEqual(tui.build_context_hints("search", search_focus=True)[0], ("Enter", "Search"))
        self.assertEqual(tui.build_context_hints("queue", active_modal="downloads")[1], ("X", "Cancel All"))
        self.assertEqual(tui.build_context_hints("artists", is_group_list=True)[0], ("Enter", "Open"))
        self.assertEqual(tui.build_context_hints("queue")[1], ("J/K", "Move"))
        self.assertEqual(tui.build_context_hints("search", has_nav=True)[-2], ("Bksp", "Back"))

    def test_ytm_multi_artist_runs_keep_album_intact(self):
        search_raw = {
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "abc12345678"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "When We Were Young (The Logical Song)"}]}}},
                                    {
                                        "musicResponsiveListItemFlexColumnRenderer": {
                                            "text": {
                                                "runs": [
                                                    {"text": "David Guetta"},
                                                    {"text": " & "},
                                                    {"text": "Sia"},
                                                    {"text": " • "},
                                                    {"text": "Nothing But the Beat"},
                                                    {"text": " • "},
                                                    {"text": "2:28"},
                                                ]
                                            }
                                        }
                                    },
                                ],
                            }
                        },
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "def12345678"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Coming Over"}]}}},
                                    {
                                        "musicResponsiveListItemFlexColumnRenderer": {
                                            "text": {
                                                "runs": [
                                                    {"text": "Dillon Francis"},
                                                    {"text": ", "},
                                                    {"text": "Kygo"},
                                                    {"text": " & "},
                                                    {"text": "James Hersey"},
                                                    {"text": " • "},
                                                    {"text": "This Mixtape Is Fire"},
                                                    {"text": " • "},
                                                    {"text": "2:58"},
                                                ]
                                            }
                                        }
                                    },
                                ],
                            }
                        },
                    ]
                }
            }
        }
        songs = catalog.parse_ytm_search(search_raw, "songs")
        self.assertEqual(len(songs), 2)
        self.assertEqual(songs[0]["artist"], "David Guetta & Sia")
        self.assertEqual(songs[0]["album"], "Nothing But the Beat")
        self.assertEqual(songs[0]["duration"], 148)
        self.assertEqual(songs[1]["artist"], "Dillon Francis, Kygo & James Hersey")
        self.assertEqual(songs[1]["album"], "This Mixtape Is Fire")
        self.assertEqual(songs[1]["duration"], 178)

        browse_raw = {
            "header": {
                "musicResponsiveHeaderRenderer": {
                    "title": {"runs": [{"text": "Collab Album"}]},
                    "subtitle": {"runs": [{"text": "Album"}, {"text": " • "}, {"text": "2018"}]},
                    "straplineTextOne": {"runs": [{"text": "David Guetta"}, {"text": " & "}, {"text": "Sia"}]},
                }
            },
            "contents": {
                "musicShelfRenderer": {
                    "contents": [
                        {
                            "musicResponsiveListItemRenderer": {
                                "playlistItemData": {"videoId": "ghi12345678"},
                                "flexColumns": [
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Track One"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "David Guetta"}, {"text": " & "}, {"text": "Sia"}]}}},
                                    {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Collab Album"}, {"text": " • "}, {"text": "3:10"}]}}},
                                ],
                            }
                        }
                    ]
                }
            },
        }
        browsed = catalog.parse_ytm_browse(browse_raw, "MPREb_collab123")
        self.assertEqual(browsed["entity"]["artist"], "David Guetta & Sia")
        self.assertEqual(browsed["entity"]["year"], 2018)
        self.assertEqual(len(browsed["items"]), 1)
        self.assertEqual(browsed["items"][0]["artist"], "David Guetta & Sia")
        self.assertEqual(browsed["items"][0]["album"], "Collab Album")
        self.assertEqual(browsed["items"][0]["duration"], 190)

    def test_header_title_normalization_and_render(self):
        import tui_widgets

        self.assertEqual(
            tui_widgets.format_header_title("grentperez - When We Were Younger (feat. Cavetown) (Official Audio)", "grentperez"),
            "When We Were Younger (feat. Cavetown)",
        )
        self.assertEqual(
            tui_widgets.format_header_title("GRENTPEREZ — When We Were Younger [Official Video]", "grentperez"),
            "When We Were Younger",
        )
        self.assertEqual(
            tui_widgets.format_header_title("To the Bone (Acoustic)", "Pamungkas"),
            "To the Bone (Acoustic)",
        )
        self.assertEqual(tui_widgets.format_header_title("", "Pamungkas"), "")
        self.assertEqual(tui_widgets.format_header_title("watch?v=dQw4w9WgXcQ", ""), "watch?v=dQw4w9WgXcQ")

        class RecordingStdscr:
            def __init__(self, h: int, w: int) -> None:
                self.h, self.w = h, w
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (self.h, self.w)

            def erase(self) -> None:
                self.writes.clear()

            def refresh(self) -> None:
                return None

            def addstr(self, y: int, x: int, text: str, *_args) -> None:
                self.writes.append((y, x, text))

            def row_text(self, y: int) -> str:
                buf = [" "] * self.w
                for wy, wx, txt in self.writes:
                    if wy == y:
                        for i, ch in enumerate(txt):
                            if 0 <= wx + i < self.w:
                                buf[wx + i] = ch
                return "".join(buf)

        with tempfile.TemporaryDirectory() as td, unittest.mock.patch.object(tui.curses, "color_pair", return_value=0):
            root = Path(td)
            scr = RecordingStdscr(30, 120)
            d = tui.SovereignDashboard(
                stdscr=scr,
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=unittest.mock.Mock(),
                dl_manager=unittest.mock.Mock(),
            )
            d.mpv_state = {
                "idle-active": False,
                "pause": False,
                "media-title": "grentperez - When We Were Younger (feat. Cavetown) (Official Audio)",
                "metadata": {"artist": "grentperez"},
                "time-pos": 10.0,
                "duration": 241.0,
                "volume": 100,
                "mute": False,
            }
            d.render()
            row1 = scr.row_text(1)
            self.assertIn("When We Were Younger (feat. Cavetown)", row1)
            self.assertNotIn("grentperez -", row1)
            self.assertNotIn("(Official", row1)

    def test_common_parse_meta_fields_ssot(self):
        self.assertEqual(
            common_mod.parse_meta_fields("David Guetta & Sia • Nothing But the Beat • 2:28"),
            {"artist": "David Guetta & Sia", "album": "Nothing But the Beat", "duration": 148, "duration_str": "2:28"},
        )
        self.assertEqual(
            common_mod.parse_meta_fields("Pamungkas • 5:45"),
            {"artist": "Pamungkas", "album": "", "duration": 345, "duration_str": "5:45"},
        )
        self.assertEqual(
            common_mod.parse_meta_fields("Artist X • 1:02:03"),
            {"artist": "Artist X", "album": "", "duration": 3723, "duration_str": "1:02:03"},
        )
        self.assertEqual(
            common_mod.parse_meta_fields("Album • Pamungkas • 2018"),
            {"artist": "", "album": "", "duration": 0, "duration_str": ""},
        )
        self.assertEqual(
            common_mod.parse_meta_fields("Pamungkas • 2018 • 1.2M plays • Active • #3 • 03:30"),
            {"artist": "Pamungkas", "album": "", "duration": 210, "duration_str": "03:30"},
        )
        self.assertEqual(
            common_mod.parse_meta_fields("Local Audio"),
            {"artist": "", "album": "", "duration": 0, "duration_str": ""},
        )

        # Parity across downloader, tui_widgets, and common CLI
        sample = "Pamungkas • Walk the Talk • 05:45 • Active"
        self.assertEqual(downloader_mod._parse_meta_fields(sample), ("Pamungkas", "Walk the Talk", 345))
        self.assertEqual(tui.extract_album_from_meta(sample), "Walk the Talk")
        self.assertEqual(tui.parse_track_columns("To the Bone", sample), ("Pamungkas", "To the Bone", "Walk the Talk", "05:45"))

        cli_out = subprocess.check_output(
            ["python3", str(Path(__file__).resolve().parent.parent / "lib" / "common.py"), "parse-meta", " • 04:01"],
            text=True,
        )
        self.assertEqual(cli_out.splitlines(), ["", "", "241"])

    def test_wp5a_queue_sig_ssot_exts_and_duration_formatters(self):
        runtime_common = sys.modules["common"]
        self.assertIs(runtime_common.AUDIO_EXTS, library_mod.AUDIO_EXTS)
        self.assertIs(library_mod.AUDIO_EXTS, downloader_mod.AUDIO_EXTS)
        self.assertEqual(common_mod.AUDIO_EXTS, library_mod.AUDIO_EXTS)
        self.assertEqual(common_mod.format_mmss(345), "05:45")
        self.assertEqual(common_mod.format_mmss(345, pad_minutes=False), "5:45")
        self.assertEqual(catalog.format_duration_mmss(345), "5:45")
        self.assertEqual(catalog.format_duration_mmss(0), "")
        self.assertEqual(library_mod.format_duration(345), "5:45")
        self.assertEqual(library_mod.format_duration(3661), "1:01:01")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = tui.SovereignDashboard(
                data_dir=root / "data",
                cache_dir=root / "cache",
                download_dir=root / "dl",
                art_enabled=False,
                mpv=unittest.mock.Mock(),
                dl_manager=unittest.mock.Mock(),
            )
            d.mpv_state = {
                "playlist": [{"filename": "a.opus", "title": "Song A", "current": True}],
                "metadata": {"artist": "Artist A", "album": "Album A"},
                "duration": 200.0,
            }
            d.active_tab = "queue"
            d.load_tab_items()
            sig1 = d._queue_state_sig()
            self.assertEqual(sig1, d._last_queue_sig)

            d.mpv_state["time-pos"] = 45.0
            self.assertEqual(d._queue_state_sig(), sig1)

            d.mpv_state["playlist"].append({"filename": "b.opus", "title": "Song B", "current": False})
            self.assertNotEqual(d._queue_state_sig(), sig1)


if __name__ == "__main__":
    unittest.main()
