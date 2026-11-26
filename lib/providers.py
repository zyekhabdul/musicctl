#!/usr/bin/env python3
"""Lazy single-source-of-truth audio provider chain and ffprobe validator."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from common import USER_AGENT, VIDEO_ID_RE, format_mmss, normalize_track_title

LOSSLESS_CODECS = ("flac", "alac", "wav")
URL_SCHEME_RE = re.compile(r"^(https?://|file://|/)")
CACHE_AUDIO_EXTS = ("flac", "opus", "m4a", "webm", "mp3")


def is_lossless_codec(codec: str) -> bool:
    """Return True when codec string represents lossless audio."""
    c = (codec or "").strip().lower()
    return c in LOSSLESS_CODECS or c.startswith("pcm_")


def ext_for_codec(codec: str) -> str:
    """Map ffprobe codec_name to canonical container file extension."""
    c = (codec or "").strip().lower()
    if c == "flac":
        return "flac"
    if c in ("alac", "aac"):
        return "m4a"
    if c == "mp3":
        return "mp3"
    if c == "vorbis":
        return "ogg"
    if c == "opus":
        return "opus"
    if c.startswith("pcm"):
        return "wav"
    return "m4a"


def discover_providers(providers_dir: Path | str) -> list[Path]:
    """Return executable provider scripts in C-locale lexical order."""
    pdir = Path(providers_dir) if providers_dir else None
    if not pdir or not pdir.is_dir():
        return []
    entries: list[Path] = []
    try:
        for item in pdir.iterdir():
            if item.is_dir():
                continue
            if (item.is_file() or item.is_symlink()) and os.access(item, os.X_OK):
                entries.append(item)
    except OSError:
        return []
    entries.sort(key=lambda p: p.name.encode("utf-8", errors="replace"))
    return entries


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def build_provider_request(
    mode: str,
    quality: str,
    track_id: str,
    title: str,
    artist: str = "",
    album: str = "",
    duration: int | float = 0,
    isrc: str = "",
    track_num: int | str = 0,
    year: int | str = 0,
    source: str = "ytm",
    fmt: str = "auto",
) -> dict[str, Any]:
    """Build normalized v2 provider stdin JSON payload."""
    clean_title = normalize_track_title(str(title or ""))
    return {
        "id": str(track_id or ""),
        "source": str(source or "ytm"),
        "title": clean_title,
        "artist": str(artist or ""),
        "album": str(album or ""),
        "isrc": str(isrc or "").strip().upper(),
        "track_num": max(0, _safe_int(track_num, 0)),
        "year": max(0, _safe_int(year, 0)),
        "duration": max(0, _safe_int(duration, 0)),
        "quality": str(quality or "cd"),
        "format": str(fmt or "auto"),
        "mode": str(mode or "download"),
    }


def _is_valid_manifest(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    if not s:
        return False
    return bool(URL_SCHEME_RE.match(s) or (s.startswith("<") and "<MPD" in s))


def _normalize_candidate(raw_obj: Any, default_name: str) -> dict[str, Any] | None:
    if not isinstance(raw_obj, dict):
        return None
    url = raw_obj.get("url")
    has_url = isinstance(url, str) and bool(URL_SCHEME_RE.match(url))
    manifest = raw_obj.get("manifest")
    has_manifest = _is_valid_manifest(manifest)
    if not has_url and not has_manifest:
        return None
    headers = raw_obj.get("headers")
    return {
        "provider": str(raw_obj.get("provider") or default_name),
        "url": str(url) if has_url else "",
        "manifest": str(manifest).strip() if has_manifest else "",
        "codec": str(raw_obj.get("codec") or ""),
        "bit_depth": max(0, _safe_int(raw_obj.get("bit_depth"), 0)),
        "sample_rate": max(0, _safe_int(raw_obj.get("sample_rate"), 0)),
        "headers": headers if isinstance(headers, dict) else {},
    }


def iter_provider_candidates(
    req: dict[str, Any],
    providers_dir: Path | str,
    timeout: float = 8.0,
) -> Iterator[dict[str, Any]]:
    """Lazily execute providers in order, yielding normalized candidates on demand."""
    providers = discover_providers(providers_dir)
    if not providers:
        return
    payload = json.dumps(req, separators=(",", ":")) + "\n"
    for prov in providers:
        try:
            proc = subprocess.run(
                [str(prov)],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except Exception:
            continue
        out = (proc.stdout or "").strip()
        if not out:
            continue
        try:
            parsed = json.loads(out)
        except Exception:
            continue
        cand = _normalize_candidate(parsed, prov.name)
        if cand is not None:
            yield cand


def probe_stream_url(
    url: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> bool:
    """Verify local file or HTTP(S) stream URL is reachable before mpv loads it."""
    if not url:
        return False
    if url.startswith("file://"):
        p = Path(url[7:])
        return p.is_file() and p.stat().st_size > 0
    if url.startswith("/"):
        p = Path(url)
        return p.is_file() and p.stat().st_size > 0

    hdrs = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        for k, v in headers.items():
            hdrs[str(k)] = str(v)

    try:
        req = urllib.request.Request(url, headers=hdrs, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code in (405, 501):
            try:
                r_hdrs = dict(hdrs)
                r_hdrs["Range"] = "bytes=0-0"
                req_r = urllib.request.Request(url, headers=r_hdrs, method="GET")
                with urllib.request.urlopen(req_r, timeout=timeout) as resp:
                    return 200 <= resp.status < 300
            except Exception:
                return False
        return False
    except Exception:
        return False


def probe_manifest(
    manifest: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> bool:
    """Verify local/HTTP/inline MPEG-DASH MPD manifest before playback or remux."""
    m = (manifest or "").strip()
    if not m:
        return False
    if m.startswith("<"):
        return "<MPD" in m
    return probe_stream_url(m, headers, timeout=timeout)


def resolve_stream_candidate(
    req: dict[str, Any],
    providers_dir: Path | str,
) -> dict[str, Any] | None:
    """Lazily find first provider candidate passing stream preflight probe."""
    for cand in iter_provider_candidates(req, providers_dir):
        if cand.get("url") and probe_stream_url(cand["url"], cand.get("headers")):
            return cand
        if cand.get("manifest") and probe_manifest(cand["manifest"], cand.get("headers")):
            return cand
    return None


def fetch_provider_file(
    url: str,
    dest: Path,
    headers: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> bool:
    """Copy local/file:// path or stream HTTP(S) target into dest."""
    try:
        if url.startswith("file://"):
            src = Path(url[7:])
            if src.is_file() and src.stat().st_size > 0:
                shutil.copyfile(src, dest)
                return dest.is_file() and dest.stat().st_size > 0
            return False
        if url.startswith("/"):
            src = Path(url)
            if src.is_file() and src.stat().st_size > 0:
                shutil.copyfile(src, dest)
                return dest.is_file() and dest.stat().st_size > 0
            return False

        hdrs = {"User-Agent": USER_AGENT}
        if isinstance(headers, dict):
            for k, v in headers.items():
                hdrs[str(k)] = str(v)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=65536)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def validate_audio_file(
    file_path: Path | str,
    expected_dur: float = 0.0,
    require_lossless: bool = False,
    require_hires: bool = False,
) -> dict[str, Any] | None:
    """Probe audio stream with ffprobe and validate codec, quality tier, and duration."""
    fpath = Path(file_path)
    if not fpath.is_file() or fpath.stat().st_size == 0:
        return None
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample:format=duration",
                "-of",
                "json",
                str(fpath),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10.0,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    try:
        data = json.loads(proc.stdout)
    except Exception:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None
    st = streams[0]
    codec = str(st.get("codec_name") or "").lower()
    if not codec:
        return None

    if (require_lossless or require_hires) and not is_lossless_codec(codec):
        return None

    srate = _safe_int(st.get("sample_rate"), 0)
    bits = _safe_int(st.get("bits_per_raw_sample") or st.get("bits_per_sample"), 0)
    if require_hires and not (bits >= 24 or srate > 48000):
        return None

    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0

    if expected_dur > 0:
        if dur <= 0 or dur < (expected_dur - 5.0) or dur > (expected_dur + 15.0):
            return None

    return {
        "codec": codec,
        "bit_depth": bits,
        "sample_rate": srate,
        "duration": round(dur, 3),
    }


def _ffmpeg_header_args(headers: dict[str, Any] | None) -> list[str]:
    if not isinstance(headers, dict) or not headers:
        return []
    joined = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k)
    return ["-headers", joined] if joined else []


def fetch_dash_manifest(
    manifest: str,
    dest: Path,
    headers: dict[str, Any] | None = None,
    timeout: float = 90.0,
) -> bool:
    """Remux MPEG-DASH (.mpd) audio stream into dest without lossy re-encoding."""
    if not shutil.which("ffmpeg"):
        return False
    m = (manifest or "").strip()
    if not m:
        return False

    tmp_mpd: Path | None = None
    tmp_remux: Path | None = None
    try:
        if m.startswith("<"):
            fd, mpd_name = tempfile.mkstemp(prefix=".dash.", suffix=".mpd", dir=str(dest.parent))
            os.close(fd)
            tmp_mpd = Path(mpd_name)
            tmp_mpd.write_text(m, encoding="utf-8")
            mpd_input = str(tmp_mpd)
        elif m.startswith("file://"):
            mpd_input = m[7:]
        else:
            mpd_input = m

        base_cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data",
        ]
        if mpd_input.startswith(("http://", "https://")):
            base_cmd.extend(_ffmpeg_header_args(headers))

        fd_r, remux_name = tempfile.mkstemp(prefix=".dash.", suffix=".flac", dir=str(dest.parent))
        os.close(fd_r)
        tmp_remux = Path(remux_name)

        res = subprocess.run(
            [*base_cmd, "-i", mpd_input, "-vn", "-c:a", "copy", "-f", "flac", str(tmp_remux)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        if res.returncode == 0 and tmp_remux.is_file() and tmp_remux.stat().st_size > 0:
            info = validate_audio_file(tmp_remux, 0.0, False, False)
            if info and info["codec"] == "flac" and info["duration"] <= 0:
                # Stream-copying fMP4 DASH leaves FLAC total_samples=0; rewrite FLAC framing losslessly so duration/seeking work.
                fix = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(tmp_remux), "-vn", "-c:a", "flac", "-f", "flac", str(dest)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                if fix.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
                    return True
            os.replace(tmp_remux, dest)
            return dest.is_file() and dest.stat().st_size > 0

        res_mka = subprocess.run(
            [*base_cmd, "-i", mpd_input, "-vn", "-c:a", "copy", "-f", "matroska", str(dest)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return res_mka.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False
    finally:
        if tmp_mpd is not None:
            tmp_mpd.unlink(missing_ok=True)
        if tmp_remux is not None:
            tmp_remux.unlink(missing_ok=True)


def fetch_validated_download(
    req: dict[str, Any],
    providers_dir: Path | str,
    tmp_dir: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    """Lazily fetch and validate candidate downloads (URL or DASH manifest), cascading hires -> cd."""
    eff_q = str(req.get("quality") or "cd")
    req_lossless = eff_q in ("cd", "hires")
    exp_dur = float(req.get("duration") or 0)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    fallback: tuple[Path, dict[str, Any], dict[str, Any]] | None = None
    for cand in iter_provider_candidates(req, providers_dir):
        fd, raw_name = tempfile.mkstemp(prefix=".prov.", dir=str(tmp_dir))
        os.close(fd)
        cand_raw = Path(raw_name)
        try:
            ok = False
            if cand.get("url"):
                ok = fetch_provider_file(cand["url"], cand_raw, cand.get("headers"))
            elif cand.get("manifest"):
                ok = fetch_dash_manifest(cand["manifest"], cand_raw, cand.get("headers"))
            info = validate_audio_file(cand_raw, exp_dur, req_lossless, False) if ok else None
        except Exception:
            info = None

        if not info:
            cand_raw.unlink(missing_ok=True)
            continue

        if eff_q == "hires":
            if info["bit_depth"] >= 24 or info["sample_rate"] > 48000:
                if fallback is not None:
                    fallback[0].unlink(missing_ok=True)
                return cand_raw, info, cand
            if fallback is None:
                fallback = (cand_raw, info, cand)
            else:
                cand_raw.unlink(missing_ok=True)
            continue

        return cand_raw, info, cand

    return fallback


def find_cached_audio(track_id: str, cache_audio_dir: Path | str | None) -> str:
    """Return non-empty cached audio file path for a YouTube video ID, or empty string."""
    if not cache_audio_dir or not VIDEO_ID_RE.match(track_id or ""):
        return ""
    cdir = Path(cache_audio_dir)
    if not cdir.is_dir():
        return ""
    for ext in CACHE_AUDIO_EXTS:
        p = cdir / f"{track_id}.{ext}"
        try:
            if p.is_file() and p.stat().st_size > 0:
                return str(p)
        except OSError:
            continue
    return ""


def _id_to_url(id_or_url: str) -> str:
    if URL_SCHEME_RE.match(id_or_url or ""):
        return id_or_url
    return f"https://www.youtube.com/watch?v={id_or_url}"


def _format_track_meta(artist: str, album: str, duration: int, source: str) -> str:
    parts: list[str] = []
    if artist:
        parts.append(artist)
    if album:
        parts.append(album)
    if duration > 0:
        parts.append(format_mmss(duration))
    return " • ".join(parts) if parts else source


def _default_ytm_fallback_id(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return ""
    try:
        import catalog

        found = catalog.search_ytm(q, "songs", limit=1)
        if found and found[0].get("id"):
            return str(found[0]["id"])
    except Exception:
        pass
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--ignore-config",
                "--flat-playlist",
                "--no-update",
                "--print",
                "%(id)s",
                f"ytsearch1:{q}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15.0,
        )
        for ln in (proc.stdout or "").splitlines():
            vid = ln.strip()
            if vid:
                return vid
    except Exception:
        pass
    return ""


def resolve_catalog_batch(
    items: list[dict[str, Any]],
    quality: str,
    providers_dir: Path | str,
    cache_audio_dir: Path | str | None = None,
    ytm_fallback_fn: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve browsed catalog song entities into mpv batch items in one Python pass."""
    use_prov = bool(discover_providers(providers_dir))
    search_fn = ytm_fallback_fn or _default_ytm_fallback_id
    resolved: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict) or item.get("type") != "song":
            continue
        t_eid = str(item.get("id") or "").strip()
        if not t_eid:
            continue
        t_src = str(item.get("source") or "ytm").strip() or "ytm"
        t_title = str(item.get("title") or "").strip()
        t_artist = str(item.get("artist") or "").strip()
        t_album = str(item.get("album") or "").strip()
        t_isrc = str(item.get("isrc") or "").strip()
        t_track_num = max(0, _safe_int(item.get("track_num"), 0))
        t_year = max(0, _safe_int(item.get("year"), 0))
        t_dur = max(0, _safe_int(item.get("duration"), 0))
        meta = _format_track_meta(t_artist, t_album, t_dur, t_src)

        target = ""
        headers: dict[str, Any] = {}
        hid = ""

        if t_src == "ytm":
            hid = t_eid
            target = find_cached_audio(t_eid, cache_audio_dir)
            if not target and use_prov:
                req = build_provider_request(
                    "stream",
                    quality,
                    t_eid,
                    t_title,
                    t_artist,
                    t_album,
                    t_dur,
                    t_isrc,
                    t_track_num,
                    t_year,
                    source=t_src,
                )
                cand = resolve_stream_candidate(req, providers_dir)
                if cand is not None:
                    target = str(cand.get("url") or cand.get("manifest") or "").strip()
                    headers = dict(cand.get("headers") or {})
            if not target:
                target = _id_to_url(t_eid)
        else:
            if use_prov:
                req = build_provider_request(
                    "stream",
                    quality,
                    t_eid,
                    t_title,
                    t_artist,
                    t_album,
                    t_dur,
                    t_isrc,
                    t_track_num,
                    t_year,
                    source=t_src,
                )
                cand = resolve_stream_candidate(req, providers_dir)
                if cand is not None:
                    target = str(cand.get("url") or cand.get("manifest") or "").strip()
                    headers = dict(cand.get("headers") or {})
                    if target:
                        hid = f"{t_src}:song:{t_eid}"
            if not target:
                q = f"{t_artist} {t_title}".strip() if t_artist else t_title
                yid = (search_fn(q) or "").strip()
                if not yid:
                    continue
                hid = yid
                target = find_cached_audio(yid, cache_audio_dir) or _id_to_url(yid)

        if not target:
            continue
        resolved.append(
            {
                "url": target,
                "title": t_title,
                "headers": headers,
                "hid": hid,
                "meta": meta,
            }
        )
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return 1
    cmd = args[0]
    pdir = os.environ.get("MUSICCTL_PROVIDERS_DIR") or ""

    if cmd == "resolve-first" and len(args) >= 8:
        req = build_provider_request(
            args[1],
            args[2],
            args[3],
            args[4],
            args[5],
            args[6],
            _safe_int(args[7], 0),
            args[8] if len(args) > 8 else "",
            args[9] if len(args) > 9 else 0,
            args[10] if len(args) > 10 else 0,
            source=args[11] if len(args) > 11 else "ytm",
            fmt=args[12] if len(args) > 12 else "auto",
        )
        first = next(iter_provider_candidates(req, pdir), None)
        if first is None:
            return 1
        print(json.dumps(first, separators=(",", ":")))
        return 0

    if cmd == "resolve-stream" and len(args) >= 7:
        req = build_provider_request(
            "stream",
            args[1],
            args[2],
            args[3],
            args[4],
            args[5],
            _safe_int(args[6], 0),
            args[7] if len(args) > 7 else "",
            args[8] if len(args) > 8 else 0,
            args[9] if len(args) > 9 else 0,
            source=args[10] if len(args) > 10 else "ytm",
        )
        chosen = resolve_stream_candidate(req, pdir)
        if chosen is None:
            return 1
        print(json.dumps(chosen, separators=(",", ":")))
        return 0

    if cmd == "validate-audio" and len(args) >= 2:
        exp_dur = float(args[2]) if len(args) > 2 and args[2] else 0.0
        req_ll = (args[3] if len(args) > 3 else "0") == "1"
        req_hr = (args[4] if len(args) > 4 else "0") == "1"
        info = validate_audio_file(args[1], exp_dur, req_ll, req_hr)
        if info is None:
            return 1
        print(json.dumps(info, separators=(",", ":")))
        return 0

    if cmd == "probe-stream" and len(args) >= 2:
        hdrs: dict[str, Any] = {}
        if len(args) > 2 and args[2].strip():
            try:
                obj = json.loads(args[2])
                if isinstance(obj, dict) and isinstance(obj.get("headers"), dict):
                    hdrs = obj["headers"]
            except Exception:
                pass
        return 0 if probe_stream_url(args[1], hdrs) else 1

    if cmd == "fetch-validated" and len(args) >= 8:
        req = build_provider_request(
            "download",
            args[1],
            args[2],
            args[3],
            args[4],
            args[5],
            _safe_int(args[6], 0),
            args[8] if len(args) > 8 else "",
            args[9] if len(args) > 9 else 0,
            args[10] if len(args) > 10 else 0,
            source=args[11] if len(args) > 11 else "ytm",
            fmt=args[12] if len(args) > 12 else "auto",
        )
        res = fetch_validated_download(req, pdir, Path(args[7]))
        if res is None:
            return 1
        raw_path, info, cand = res
        print(str(raw_path))
        print(json.dumps(info, separators=(",", ":")))
        print(json.dumps(cand, separators=(",", ":")))
        return 0

    if cmd == "resolve-batch" and len(args) >= 2:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return 1
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return 1
        cache_audio_dir = args[2] if len(args) > 2 and args[2] else None
        for row in resolve_catalog_batch(items, args[1], pdir, cache_audio_dir=cache_audio_dir):
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
