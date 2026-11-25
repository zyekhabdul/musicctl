#!/usr/bin/env python3
import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

TUI_PATH = Path(__file__).resolve().parent.parent / "lib" / "tui.py"
COMMON_PATH = Path(__file__).resolve().parent.parent / "lib" / "common.py"
SPEC = importlib.util.spec_from_file_location("musicctl_tui", TUI_PATH)
tui = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = tui
SPEC.loader.exec_module(tui)


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


if __name__ == "__main__":
    unittest.main()
