#!/usr/bin/env python3
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TUI_PATH = Path(__file__).resolve().parent.parent / "lib" / "tui.py"
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
                client.send(["quit"])
            finally:
                proc.terminate()
                proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
