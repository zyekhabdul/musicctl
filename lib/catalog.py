#!/usr/bin/env python3
"""Multi-catalog entity engine (YTM InnerTube, Deezer, iTunes) for musicctl."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

YTM_CLIENT_CONTEXT = {
    "client": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20260911.08.00",
        "hl": "en",
        "gl": "US",
    }
}

YTM_SEARCH_PARAMS = {
    "songs": "EgWKAQIIAWoMEAMQBBAJEAoQBRAV",
    "albums": "EgWKAQIYAWoMEAMQBBAJEAoQBRAV",
    "artists": "EgWKAQIgAWoMEAMQBBAJEAoQBRAV",
    "playlists": "EgWKAQIoAWoMEAMQBBAJEAoQBRAV",
}

CATALOG_SOURCES = ("ytm", "deezer", "itunes")
SEARCH_TYPES = ("songs", "albums", "artists", "playlists")
ENTITY_TYPES = ("song", "album", "artist", "playlist")

TYPE_SINGULAR = {
    "song": "song",
    "songs": "song",
    "track": "song",
    "tracks": "song",
    "album": "album",
    "albums": "album",
    "artist": "artist",
    "artists": "artist",
    "playlist": "playlist",
    "playlists": "playlist",
}

EXPLICIT_REF_RE = re.compile(
    r"^(ytm|deezer|itunes):(song|songs|track|tracks|album|albums|artist|artists|playlist|playlists):(.+)$",
    re.IGNORECASE,
)
DEEZER_URL_RE = re.compile(
    r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(track|album|artist|playlist)/(\d+)",
    re.IGNORECASE,
)
YTM_BROWSE_URL_RE = re.compile(
    r"^https?://(?:music|www)\.youtube\.com/browse/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
YTM_CHANNEL_URL_RE = re.compile(
    r"^https?://(?:music|www)\.youtube\.com/channel/(UC[A-Za-z0-9_-]{10,})",
    re.IGNORECASE,
)
YTM_PLAYLIST_URL_RE = re.compile(
    r"^https?://(?:music|www)\.youtube\.com/playlist\?(?:.*&)?list=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
DURATION_RE = re.compile(r"^(\d{1,3}):(\d{2})(?::(\d{2}))?$")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
NOISE_RUN_RE = re.compile(
    r"^(?:song|video|album|single|ep|artist|playlist|live|•|\s|\d+(?:[.,]\d+)?[kmbt]?\s*(?:plays?|views?|monthly\s+listeners?|subscribers?|songs?|tracks?))$",
    re.IGNORECASE,
)


def _clean_text(value: Any) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(value or "")).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_duration_str(text: str) -> int:
    """Convert M:SS or H:MM:SS string into integer seconds."""
    m = DURATION_RE.match((text or "").strip())
    if not m:
        return 0
    a = int(m.group(1))
    b = int(m.group(2))
    c = m.group(3)
    if c is not None:
        return a * 3600 + b * 60 + int(c)
    return a * 60 + b


def format_duration_mmss(secs: int) -> str:
    """Format integer seconds as M:SS or H:MM:SS."""
    if secs <= 0:
        return ""
    mins, rem = divmod(secs, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{rem:02d}"
    return f"{mins}:{rem:02d}"


def make_entity(
    *,
    source: str,
    id: str,
    type: str,
    title: str,
    isrc: str = "",
    artist: str = "",
    album: str = "",
    track_num: int = 0,
    year: int = 0,
    duration: int = 0,
    cover_url: str = "",
) -> dict[str, Any]:
    """Construct canonical catalog entity dict."""
    norm_type = TYPE_SINGULAR.get(str(type).lower(), "song")
    return {
        "source": str(source).lower(),
        "id": _clean_text(id),
        "type": norm_type,
        "isrc": _clean_text(isrc).upper(),
        "title": _clean_text(title),
        "artist": _clean_text(artist),
        "album": _clean_text(album),
        "track_num": max(0, _safe_int(track_num, 0)),
        "year": max(0, _safe_int(year, 0)),
        "duration": max(0, _safe_int(duration, 0)),
        "cover_url": _clean_text(cover_url),
    }


def format_ref(source: str, etype: str, eid: str) -> str:
    """Build canonical catalog ref string."""
    norm_type = TYPE_SINGULAR.get(etype.lower(), etype.lower())
    return f"{source.lower()}:{norm_type}:{eid}"


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """Parse canonical catalog ref, bare YTM browse ID, or supported catalog URL."""
    raw = (ref or "").strip()
    if not raw:
        return None

    m = EXPLICIT_REF_RE.match(raw)
    if m:
        src = m.group(1).lower()
        etype = TYPE_SINGULAR[m.group(2).lower()]
        eid = m.group(3).strip()
        if eid:
            return src, etype, eid
        return None

    m = DEEZER_URL_RE.match(raw)
    if m:
        return "deezer", TYPE_SINGULAR[m.group(1).lower()], m.group(2)

    m = YTM_BROWSE_URL_RE.match(raw)
    if m:
        return parse_ref(m.group(1))

    m = YTM_CHANNEL_URL_RE.match(raw)
    if m:
        return "ytm", "artist", m.group(1)

    m = YTM_PLAYLIST_URL_RE.match(raw)
    if m:
        lid = m.group(1)
        if lid.startswith("OLAK5uy_"):
            return "ytm", "album", lid
        if not lid.startswith("VL"):
            lid = f"VL{lid}"
        return "ytm", "playlist", lid

    if re.match(r"^(?:MPREb_|OLAK5uy_)[A-Za-z0-9_-]{6,}$", raw):
        return "ytm", "album", raw
    if re.match(r"^UC[A-Za-z0-9_-]{10,}$", raw):
        return "ytm", "artist", raw
    if re.match(r"^(?:VLPL|VL|PL)[A-Za-z0-9_-]{8,}$", raw):
        eid = raw if raw.startswith("VL") else f"VL{raw}"
        return "ytm", "playlist", eid

    return None


def entity_to_tsv_row(entity: dict[str, Any], include_tag: bool = False) -> str:
    """Format canonical entity as 3-column (or 4-column) TSV row."""
    src = entity.get("source", "ytm")
    etype = entity.get("type", "song")
    eid = entity.get("id", "")
    col1 = eid if (src == "ytm" and etype == "song") else format_ref(src, etype, eid)
    title = _clean_text(entity.get("title") or eid)
    artist = _clean_text(entity.get("artist"))
    album = _clean_text(entity.get("album"))
    year = _safe_int(entity.get("year"), 0)
    dur_str = format_duration_mmss(_safe_int(entity.get("duration"), 0))

    meta_parts: list[str] = []
    if etype == "song":
        if artist:
            meta_parts.append(artist)
        if album:
            meta_parts.append(album)
        if dur_str:
            meta_parts.append(dur_str)
    elif etype == "album":
        meta_parts.append("Album")
        if artist:
            meta_parts.append(artist)
        if year > 0:
            meta_parts.append(str(year))
    elif etype == "artist":
        meta_parts.append("Artist")
    elif etype == "playlist":
        meta_parts.append("Playlist")
        if artist:
            meta_parts.append(artist)

    meta = " • ".join(meta_parts)
    if include_tag:
        tag_map = {"ytm": "[YTM]", "deezer": "[DZR]", "itunes": "[ITN]"}
        return f"{col1}\t{title}\t{meta}\t{tag_map.get(src, '[CAT]')}"
    return f"{col1}\t{title}\t{meta}"


# ------------------------------------------------------------------------------
# HTTP Helpers
# ------------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 8.0) -> Any:
    hdrs = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ------------------------------------------------------------------------------
# YTM InnerTube (WEB_REMIX) Adapter
# ------------------------------------------------------------------------------


def _runs_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(str(r.get("text") or "") for r in runs if isinstance(r, dict)).strip()
    simple = node.get("simpleText")
    if simple:
        return str(simple).strip()
    return ""


def _runs_list(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    runs = node.get("runs")
    if not isinstance(runs, list):
        return []
    return [str(r.get("text") or "").strip() for r in runs if isinstance(r, dict) and str(r.get("text") or "").strip()]


def _best_thumbnail_url(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    thumbs = (
        node.get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails")
        or node.get("croppedSquareThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails")
        or node.get("thumbnail", {}).get("thumbnails")
        or node.get("thumbnails")
    )
    if isinstance(thumbs, list) and thumbs:
        best = max(
            (t for t in thumbs if isinstance(t, dict) and t.get("url")),
            key=lambda t: (_safe_int(t.get("width")), _safe_int(t.get("height"))),
            default=None,
        )
        if best:
            return str(best["url"])
    return ""


def _walk_key(obj: Any, target_key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        if target_key in obj:
            found.append(obj[target_key])
        for v in obj.values():
            found.extend(_walk_key(v, target_key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_key(item, target_key))
    return found


def _extract_ytm_meta_tokens(runs: list[str]) -> tuple[list[str], int, int]:
    """Return (text_tokens, year, duration_sec) from YTM subtitle/flex runs."""
    tokens: list[str] = []
    year = 0
    duration = 0
    for tok in runs:
        for piece in re.split(r"\s*[•·]\s*", tok):
            p = piece.strip()
            if not p:
                continue
            if DURATION_RE.match(p):
                if duration == 0:
                    duration = parse_duration_str(p)
                continue
            if YEAR_RE.match(p):
                if year == 0:
                    year = int(p)
                continue
            if NOISE_RUN_RE.match(p):
                continue
            tokens.append(p)
    return tokens, year, duration


def _ytm_browse_type_from_id(browse_id: str, fallback: str = "playlist") -> str:
    if browse_id.startswith(("MPREb_", "OLAK5uy_")):
        return "album"
    if browse_id.startswith("UC"):
        return "artist"
    if browse_id.startswith(("VL", "PL")):
        return "playlist"
    return fallback


def parse_ytm_search(data: Any, search_type: str = "songs") -> list[dict[str, Any]]:
    """Parse InnerTube search JSON into canonical entities."""
    if not isinstance(data, dict):
        return []
    want_type = TYPE_SINGULAR.get(search_type, "song")
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for renderer in _walk_key(data, "musicResponsiveListItemRenderer"):
        if not isinstance(renderer, dict):
            continue
        flex = renderer.get("flexColumns") or []
        if not flex:
            continue
        col0 = (flex[0] or {}).get("musicResponsiveListItemFlexColumnRenderer", {})
        col1 = (flex[1] or {}).get("musicResponsiveListItemFlexColumnRenderer", {}) if len(flex) > 1 else {}

        col0_runs = col0.get("text", {}).get("runs") or []
        title = _runs_text(col0.get("text"))
        if not title:
            continue

        vid = (
            (renderer.get("playlistItemData") or {}).get("videoId")
            or (
                renderer.get("overlay", {})
                .get("musicItemThumbnailOverlayRenderer", {})
                .get("content", {})
                .get("musicPlayButtonRenderer", {})
                .get("playNavigationEndpoint", {})
                .get("watchEndpoint", {})
                .get("videoId")
            )
            or (
                (col0_runs[0].get("navigationEndpoint", {}) if col0_runs else {})
                .get("watchEndpoint", {})
                .get("videoId")
            )
        )
        browse_id = (
            (renderer.get("navigationEndpoint") or {}).get("browseEndpoint", {}).get("browseId")
            or (
                (col0_runs[0].get("navigationEndpoint", {}) if col0_runs else {})
                .get("browseEndpoint", {})
                .get("browseId")
            )
        )

        if want_type == "song":
            if not vid:
                continue
            eid = str(vid)
            etype = "song"
        else:
            if not browse_id:
                continue
            eid = str(browse_id)
            etype = _ytm_browse_type_from_id(eid, want_type)
            if etype != want_type and want_type in ("album", "artist", "playlist"):
                etype = want_type

        if (etype, eid) in seen:
            continue
        seen.add((etype, eid))

        meta_runs = _runs_list(col1.get("text"))
        tokens, year, dur = _extract_ytm_meta_tokens(meta_runs)
        if dur == 0 and len(flex) > 2:
            col2 = (flex[2] or {}).get("musicResponsiveListItemFlexColumnRenderer", {})
            _, _, dur = _extract_ytm_meta_tokens(_runs_list(col2.get("text")))

        artist = ""
        album = ""
        if etype == "song":
            artist = tokens[0] if tokens else ""
            album = tokens[1] if len(tokens) > 1 else ""
        elif etype == "album":
            artist = tokens[0] if tokens else ""
        elif etype == "artist":
            artist = title
        elif etype == "playlist":
            artist = tokens[0] if tokens else ""

        cover = _best_thumbnail_url(renderer.get("thumbnail"))
        items.append(
            make_entity(
                source="ytm",
                id=eid,
                type=etype,
                title=title,
                artist=artist,
                album=album,
                year=year,
                duration=dur,
                cover_url=cover,
            )
        )

    return items


def _parse_ytm_browse_header(data: dict[str, Any], browse_id: str) -> dict[str, Any]:
    etype = _ytm_browse_type_from_id(browse_id, "playlist")
    for hdr_key in ("musicDetailHeaderRenderer", "musicResponsiveHeaderRenderer", "musicImmersiveHeaderRenderer"):
        headers = _walk_key(data, hdr_key)
        if not headers:
            continue
        hdr = headers[0]
        if not isinstance(hdr, dict):
            continue
        title = _runs_text(hdr.get("title")) or browse_id
        sub_runs = _runs_list(hdr.get("subtitle")) + _runs_list(hdr.get("straplineTextOne"))
        tokens, year, _ = _extract_ytm_meta_tokens(sub_runs)
        if year == 0:
            _, year, _ = _extract_ytm_meta_tokens(_runs_list(hdr.get("secondSubtitle")))
        artist = title if etype == "artist" else (tokens[0] if tokens else "")
        album = title if etype == "album" else ""
        cover = _best_thumbnail_url(hdr.get("thumbnail") or hdr.get("foregroundThumbnail"))
        return make_entity(
            source="ytm",
            id=browse_id,
            type=etype,
            title=title,
            artist=artist,
            album=album,
            year=year,
            cover_url=cover,
        )

    return make_entity(source="ytm", id=browse_id, type=etype, title=browse_id)


def _extract_ytm_continuation(data: dict[str, Any]) -> str:
    for node in _walk_key(data, "nextContinuationData"):
        if isinstance(node, dict) and node.get("continuation"):
            return str(node["continuation"])
    for node in _walk_key(data, "continuationItemRenderer"):
        if isinstance(node, dict):
            tok = (
                node.get("continuationEndpoint", {})
                .get("continuationCommand", {})
                .get("token")
            )
            if tok:
                return str(tok)
    return ""


def parse_ytm_browse(data: Any, browse_id: str) -> dict[str, Any]:
    """Parse InnerTube browse response into header entity, child items, and continuation."""
    if not isinstance(data, dict):
        return {
            "entity": make_entity(source="ytm", id=browse_id, type=_ytm_browse_type_from_id(browse_id), title=browse_id),
            "items": [],
            "continuation": "",
        }

    header = _parse_ytm_browse_header(data, browse_id)
    hdr_artist = header.get("artist", "")
    hdr_album = header.get("album", "")
    hdr_year = _safe_int(header.get("year"), 0)
    hdr_cover = header.get("cover_url", "")

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for idx_pos, renderer in enumerate(_walk_key(data, "musicResponsiveListItemRenderer"), 1):
        if not isinstance(renderer, dict):
            continue
        flex = renderer.get("flexColumns") or []
        if not flex:
            continue
        col0 = (flex[0] or {}).get("musicResponsiveListItemFlexColumnRenderer", {})
        col1 = (flex[1] or {}).get("musicResponsiveListItemFlexColumnRenderer", {}) if len(flex) > 1 else {}
        col0_runs = col0.get("text", {}).get("runs") or []
        title = _runs_text(col0.get("text"))
        if not title:
            continue

        vid = (
            (renderer.get("playlistItemData") or {}).get("videoId")
            or (
                renderer.get("overlay", {})
                .get("musicItemThumbnailOverlayRenderer", {})
                .get("content", {})
                .get("musicPlayButtonRenderer", {})
                .get("playNavigationEndpoint", {})
                .get("watchEndpoint", {})
                .get("videoId")
            )
            or (
                (col0_runs[0].get("navigationEndpoint", {}) if col0_runs else {})
                .get("watchEndpoint", {})
                .get("videoId")
            )
        )
        b_id = (
            (renderer.get("navigationEndpoint") or {}).get("browseEndpoint", {}).get("browseId")
            or (
                (col0_runs[0].get("navigationEndpoint", {}) if col0_runs else {})
                .get("browseEndpoint", {})
                .get("browseId")
            )
        )

        if vid:
            eid = str(vid)
            etype = "song"
        elif b_id:
            eid = str(b_id)
            etype = _ytm_browse_type_from_id(eid)
        else:
            continue

        if (etype, eid) in seen:
            continue
        seen.add((etype, eid))

        meta_runs = _runs_list(col1.get("text"))
        if len(flex) > 2:
            col2 = (flex[2] or {}).get("musicResponsiveListItemFlexColumnRenderer", {})
            meta_runs.extend(_runs_list(col2.get("text")))
        tokens, year, dur = _extract_ytm_meta_tokens(meta_runs)

        if dur == 0:
            for fc in renderer.get("fixedColumns") or []:
                fcr = (fc or {}).get("musicResponsiveListItemFixedColumnRenderer", {})
                _, _, f_dur = _extract_ytm_meta_tokens(_runs_list(fcr.get("text")))
                if f_dur > 0:
                    dur = f_dur
                    break

        idx_txt = _runs_text(renderer.get("index"))
        track_num = int(idx_txt) if idx_txt.isdigit() else (idx_pos if header["type"] == "album" and etype == "song" else 0)

        artist = tokens[0] if tokens else hdr_artist
        album = (tokens[1] if len(tokens) > 1 else "") or hdr_album
        cover = _best_thumbnail_url(renderer.get("thumbnail")) or hdr_cover

        items.append(
            make_entity(
                source="ytm",
                id=eid,
                type=etype,
                title=title,
                artist=artist,
                album=album,
                track_num=track_num,
                year=year or hdr_year,
                duration=dur,
                cover_url=cover,
            )
        )

    for card in _walk_key(data, "musicTwoRowItemRenderer"):
        if not isinstance(card, dict):
            continue
        nav = card.get("navigationEndpoint") or {}
        b_id = (nav.get("browseEndpoint") or {}).get("browseId")
        vid = (nav.get("watchEndpoint") or {}).get("videoId")
        title = _runs_text(card.get("title"))
        if not title:
            continue
        if b_id:
            eid = str(b_id)
            etype = _ytm_browse_type_from_id(eid)
        elif vid:
            eid = str(vid)
            etype = "song"
        else:
            continue
        if (etype, eid) in seen:
            continue
        seen.add((etype, eid))

        tokens, year, dur = _extract_ytm_meta_tokens(_runs_list(card.get("subtitle")))
        artist = tokens[0] if tokens else hdr_artist
        cover = _best_thumbnail_url(card.get("thumbnailRenderer")) or hdr_cover
        items.append(
            make_entity(
                source="ytm",
                id=eid,
                type=etype,
                title=title,
                artist=artist,
                album=title if etype == "album" else "",
                year=year,
                duration=dur,
                cover_url=cover,
            )
        )

    return {
        "entity": header,
        "items": items,
        "continuation": _extract_ytm_continuation(data),
    }


def search_ytm(query: str, search_type: str = "songs", limit: int = 25) -> list[dict[str, Any]]:
    """Search YouTube Music via InnerTube WEB_REMIX."""
    if not query.strip():
        return []
    params = YTM_SEARCH_PARAMS.get(search_type, YTM_SEARCH_PARAMS["songs"])
    payload = {
        "context": YTM_CLIENT_CONTEXT,
        "query": query,
        "params": params,
    }
    raw = _http_post_json(
        "https://music.youtube.com/youtubei/v1/search?prettyPrint=false",
        payload,
        headers={"Origin": "https://music.youtube.com"},
    )
    return parse_ytm_search(raw, search_type)[:limit]


def browse_ytm(browse_id: str, max_pages: int = 3) -> dict[str, Any]:
    """Browse YTM album, artist, or playlist with optional continuation paging."""
    bid = browse_id.strip()
    if bid.startswith("PL"):
        bid = f"VL{bid}"

    raw = _http_post_json(
        "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false",
        {"context": YTM_CLIENT_CONTEXT, "browseId": bid},
        headers={"Origin": "https://music.youtube.com"},
    )
    res = parse_ytm_browse(raw, bid)
    seen = {(it["type"], it["id"]) for it in res["items"]}
    token = res.get("continuation") or ""
    page = 1

    while token and page < max_pages:
        page += 1
        cont_url = (
            "https://music.youtube.com/youtubei/v1/browse?"
            + urllib.parse.urlencode({"prettyPrint": "false", "ctoken": token, "continuation": token})
        )
        next_raw = _http_post_json(
            cont_url,
            {"context": YTM_CLIENT_CONTEXT, "continuation": token},
            headers={"Origin": "https://music.youtube.com"},
        )
        next_res = parse_ytm_browse(next_raw, bid)
        for it in next_res["items"]:
            k = (it["type"], it["id"])
            if k not in seen:
                seen.add(k)
                res["items"].append(it)
        token = next_res.get("continuation") or ""

    res["continuation"] = token
    return res


# ------------------------------------------------------------------------------
# Deezer Public API Adapter
# ------------------------------------------------------------------------------


def _upgrade_deezer_cover(obj: dict[str, Any] | None) -> str:
    if not isinstance(obj, dict):
        return ""
    for k in ("cover_xl", "picture_xl", "cover_big", "picture_big", "cover_medium", "picture_medium", "cover", "picture"):
        val = obj.get(k)
        if val:
            return re.sub(r"/\d+x\d+(?=[-.])", "/1000x1000", str(val))
    md5 = obj.get("md5_image")
    if md5:
        return f"https://e-cdns-images.dzcdn.net/images/cover/{md5}/1000x1000-000000-80-0-0.jpg"
    return ""


def _parse_year_prefix(date_str: Any) -> int:
    m = re.match(r"^(\d{4})", str(date_str or "").strip())
    return int(m.group(1)) if m else 0


def parse_deezer_track(obj: dict[str, Any], album_ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map Deezer track JSON object to canonical song entity."""
    alb = obj.get("album") if isinstance(obj.get("album"), dict) else (album_ctx or {})
    art = obj.get("artist") if isinstance(obj.get("artist"), dict) else {}
    artist_name = str(art.get("name") or (album_ctx or {}).get("artist") or "")
    album_title = str(alb.get("title") or (album_ctx or {}).get("title") or "")
    year = _parse_year_prefix(obj.get("release_date") or alb.get("release_date") or (album_ctx or {}).get("release_date"))
    cover = _upgrade_deezer_cover(alb) or _upgrade_deezer_cover(album_ctx) or _upgrade_deezer_cover(art)
    return make_entity(
        source="deezer",
        id=str(obj.get("id") or ""),
        type="song",
        isrc=str(obj.get("isrc") or ""),
        title=str(obj.get("title") or obj.get("title_short") or ""),
        artist=artist_name,
        album=album_title,
        track_num=_safe_int(obj.get("track_position"), 0),
        year=year,
        duration=_safe_int(obj.get("duration"), 0),
        cover_url=cover,
    )


def parse_deezer_album(obj: dict[str, Any]) -> dict[str, Any]:
    """Map Deezer album JSON object to canonical album entity."""
    art = obj.get("artist") if isinstance(obj.get("artist"), dict) else {}
    return make_entity(
        source="deezer",
        id=str(obj.get("id") or ""),
        type="album",
        title=str(obj.get("title") or ""),
        artist=str(art.get("name") or ""),
        album=str(obj.get("title") or ""),
        year=_parse_year_prefix(obj.get("release_date")),
        cover_url=_upgrade_deezer_cover(obj),
    )


def parse_deezer_artist(
    artist_obj: dict[str, Any],
    top_data: dict[str, Any] | None = None,
    albums_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Deezer artist browse payload from artist, top tracks, and albums responses."""
    name = str(artist_obj.get("name") or "")
    ent = make_entity(
        source="deezer",
        id=str(artist_obj.get("id") or ""),
        type="artist",
        title=name,
        artist=name,
        cover_url=_upgrade_deezer_cover(artist_obj),
    )
    items: list[dict[str, Any]] = []
    for trk in (top_data or {}).get("data") or []:
        if isinstance(trk, dict) and trk.get("id"):
            items.append(parse_deezer_track(trk))
    for alb in (albums_data or {}).get("data") or []:
        if isinstance(alb, dict) and alb.get("id"):
            alb_ent = parse_deezer_album(alb)
            if not alb_ent["artist"]:
                alb_ent["artist"] = name
            items.append(alb_ent)
    return {"entity": ent, "items": items, "continuation": ""}


def parse_deezer_playlist(obj: dict[str, Any]) -> dict[str, Any]:
    """Build Deezer playlist browse payload."""
    creator = obj.get("creator") if isinstance(obj.get("creator"), dict) else {}
    ent = make_entity(
        source="deezer",
        id=str(obj.get("id") or ""),
        type="playlist",
        title=str(obj.get("title") or ""),
        artist=str(creator.get("name") or ""),
        cover_url=_upgrade_deezer_cover(obj),
    )
    tracks_raw = (obj.get("tracks") or {}).get("data") or []
    items = [parse_deezer_track(t) for t in tracks_raw if isinstance(t, dict) and t.get("id")]
    return {"entity": ent, "items": items, "continuation": ""}


def parse_deezer_search(data: Any, search_type: str = "songs") -> list[dict[str, Any]]:
    """Parse Deezer search endpoint JSON into canonical entities."""
    if not isinstance(data, dict):
        return []
    rows = data.get("data") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if search_type == "songs":
            out.append(parse_deezer_track(row))
        elif search_type == "albums":
            out.append(parse_deezer_album(row))
        elif search_type == "artists":
            name = str(row.get("name") or "")
            out.append(
                make_entity(
                    source="deezer",
                    id=str(row["id"]),
                    type="artist",
                    title=name,
                    artist=name,
                    cover_url=_upgrade_deezer_cover(row),
                )
            )
        elif search_type == "playlists":
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            out.append(
                make_entity(
                    source="deezer",
                    id=str(row["id"]),
                    type="playlist",
                    title=str(row.get("title") or ""),
                    artist=str(user.get("name") or ""),
                    cover_url=_upgrade_deezer_cover(row),
                )
            )
    return out


def search_deezer(query: str, search_type: str = "songs", limit: int = 25) -> list[dict[str, Any]]:
    """Query Deezer public search API."""
    if not query.strip():
        return []
    ep_map = {
        "songs": "track",
        "albums": "album",
        "artists": "artist",
        "playlists": "playlist",
    }
    ep = ep_map.get(search_type, "track")
    url = f"https://api.deezer.com/search/{ep}?" + urllib.parse.urlencode({"q": query, "limit": limit})
    return parse_deezer_search(_http_get_json(url), search_type)[:limit]


def browse_deezer(etype: str, eid: str) -> dict[str, Any]:
    """Browse Deezer track, album, artist, or playlist."""
    norm = TYPE_SINGULAR.get(etype, "song")
    if norm == "song":
        trk = parse_deezer_track(_http_get_json(f"https://api.deezer.com/track/{eid}"))
        return {"entity": trk, "items": [trk], "continuation": ""}
    if norm == "album":
        raw = _http_get_json(f"https://api.deezer.com/album/{eid}")
        ent = parse_deezer_album(raw)
        ctx = {
            "title": ent["title"],
            "artist": ent["artist"],
            "release_date": raw.get("release_date"),
            "cover_xl": ent["cover_url"],
        }
        tracks_raw = (raw.get("tracks") or {}).get("data") or []
        items = [parse_deezer_track(t, album_ctx=ctx) for t in tracks_raw if isinstance(t, dict) and t.get("id")]
        return {"entity": ent, "items": items, "continuation": ""}
    if norm == "artist":
        art = _http_get_json(f"https://api.deezer.com/artist/{eid}")
        top = _http_get_json(f"https://api.deezer.com/artist/{eid}/top?limit=25")
        albs = _http_get_json(f"https://api.deezer.com/artist/{eid}/albums?limit=25")
        return parse_deezer_artist(art, top, albs)
    if norm == "playlist":
        raw = _http_get_json(f"https://api.deezer.com/playlist/{eid}")
        return parse_deezer_playlist(raw)
    raise ValueError(f"Unsupported Deezer entity type: {etype}")


# ------------------------------------------------------------------------------
# iTunes Search & Lookup API Adapter
# ------------------------------------------------------------------------------


def _upgrade_itunes_cover(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    return re.sub(r"\d+x\d+bb", "1000x1000bb", raw)


def _itunes_track_entity(row: dict[str, Any]) -> dict[str, Any]:
    return make_entity(
        source="itunes",
        id=str(row.get("trackId") or ""),
        type="song",
        title=str(row.get("trackName") or ""),
        artist=str(row.get("artistName") or ""),
        album=str(row.get("collectionName") or ""),
        track_num=_safe_int(row.get("trackNumber"), 0),
        year=_parse_year_prefix(row.get("releaseDate")),
        duration=int(round(_safe_int(row.get("trackTimeMillis"), 0) / 1000.0)),
        cover_url=_upgrade_itunes_cover(row.get("artworkUrl100") or row.get("artworkUrl60")),
    )


def _itunes_album_entity(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("collectionName") or "")
    return make_entity(
        source="itunes",
        id=str(row.get("collectionId") or ""),
        type="album",
        title=title,
        artist=str(row.get("artistName") or ""),
        album=title,
        year=_parse_year_prefix(row.get("releaseDate")),
        cover_url=_upgrade_itunes_cover(row.get("artworkUrl100") or row.get("artworkUrl60")),
    )


def _itunes_artist_entity(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("artistName") or "")
    return make_entity(
        source="itunes",
        id=str(row.get("artistId") or ""),
        type="artist",
        title=name,
        artist=name,
    )


def parse_itunes_search(data: Any, search_type: str = "songs") -> list[dict[str, Any]]:
    """Parse iTunes Search API JSON into canonical entities."""
    if not isinstance(data, dict) or search_type == "playlists":
        return []
    out: list[dict[str, Any]] = []
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        if search_type == "songs" and row.get("trackId"):
            out.append(_itunes_track_entity(row))
        elif search_type == "albums" and row.get("collectionId"):
            out.append(_itunes_album_entity(row))
        elif search_type == "artists" and row.get("artistId"):
            out.append(_itunes_artist_entity(row))
    return out


def parse_itunes_lookup(data: Any, etype: str = "album") -> dict[str, Any]:
    """Parse iTunes Lookup API JSON into canonical browse payload."""
    norm = TYPE_SINGULAR.get(etype, "album")
    rows = (data.get("results") or []) if isinstance(data, dict) else []
    header: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        wtype = str(row.get("wrapperType") or "").lower()
        if wtype == "artist" and norm == "artist" and header is None:
            header = _itunes_artist_entity(row)
        elif wtype == "collection":
            alb_ent = _itunes_album_entity(row)
            if norm == "album" and header is None:
                header = alb_ent
            else:
                items.append(alb_ent)
        elif wtype == "track":
            trk = _itunes_track_entity(row)
            if norm == "song" and header is None:
                header = trk
            items.append(trk)

    if header is None:
        header = items[0] if items else make_entity(source="itunes", id="", type=norm, title="")
    return {"entity": header, "items": items, "continuation": ""}


def search_itunes(query: str, search_type: str = "songs", limit: int = 25) -> list[dict[str, Any]]:
    """Query iTunes public Search API."""
    if not query.strip() or search_type == "playlists":
        return []
    ent_map = {"songs": "song", "albums": "album", "artists": "musicArtist"}
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": query, "media": "music", "entity": ent_map.get(search_type, "song"), "limit": limit}
    )
    return parse_itunes_search(_http_get_json(url), search_type)[:limit]


def browse_itunes(etype: str, eid: str) -> dict[str, Any]:
    """Browse iTunes track, album, or artist via lookup API."""
    norm = TYPE_SINGULAR.get(etype, "song")
    params: dict[str, Any] = {"id": eid}
    if norm == "album":
        params["entity"] = "song"
    elif norm == "artist":
        params["entity"] = "album"
        params["limit"] = 25
    elif norm != "song":
        raise ValueError(f"Unsupported iTunes entity type: {etype}")
    url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(params)
    return parse_itunes_lookup(_http_get_json(url), norm)


# ------------------------------------------------------------------------------
# Unified Dispatch & CLI
# ------------------------------------------------------------------------------


def search_catalog(query: str, catalog: str = "ytm", search_type: str = "songs", limit: int = 25) -> list[dict[str, Any]]:
    src = catalog.lower()
    stype = search_type.lower()
    if src == "ytm":
        return search_ytm(query, stype, limit)
    if src == "deezer":
        return search_deezer(query, stype, limit)
    if src == "itunes":
        return search_itunes(query, stype, limit)
    raise ValueError(f"Unknown catalog source: {catalog}")


def browse_catalog(ref: str) -> dict[str, Any]:
    parsed = parse_ref(ref)
    if parsed is None:
        raise ValueError(f"Invalid catalog ref: {ref}")
    src, etype, eid = parsed
    if src == "ytm":
        return browse_ytm(eid)
    if src == "deezer":
        return browse_deezer(etype, eid)
    if src == "itunes":
        return browse_itunes(etype, eid)
    raise ValueError(f"Unknown catalog source: {src}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog.py", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search")
    p_search.add_argument("--catalog", "-c", default="ytm", choices=CATALOG_SOURCES)
    p_search.add_argument("--type", "-t", default="songs", choices=SEARCH_TYPES)
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--tsv-tag", action="store_true")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("query", nargs="+")

    p_browse = sub.add_parser("browse")
    p_browse.add_argument("--json", action="store_true")
    p_browse.add_argument("--tsv", action="store_true")
    p_browse.add_argument("ref")

    p_ref = sub.add_parser("parse-ref")
    p_ref.add_argument("ref")

    p_stdin = sub.add_parser("parse-stdin")
    p_stdin.add_argument("source", choices=CATALOG_SOURCES)
    p_stdin.add_argument("mode", choices=("search", "browse"))
    p_stdin.add_argument("subtype", nargs="?", default="songs")

    args = parser.parse_args(argv)

    if args.cmd == "parse-ref":
        res = parse_ref(args.ref)
        if res is None:
            return 1
        print(json.dumps({"source": res[0], "type": res[1], "id": res[2]}))
        return 0

    if args.cmd == "parse-stdin":
        raw = json.load(sys.stdin)
        if args.mode == "search":
            if args.source == "ytm":
                out: Any = parse_ytm_search(raw, args.subtype)
            elif args.source == "deezer":
                out = parse_deezer_search(raw, args.subtype)
            else:
                out = parse_itunes_search(raw, args.subtype)
        else:
            if args.source == "ytm":
                out = parse_ytm_browse(raw, args.subtype)
            elif args.source == "deezer":
                if args.subtype == "album":
                    ent = parse_deezer_album(raw)
                    ctx = {
                        "title": ent["title"],
                        "artist": ent["artist"],
                        "release_date": raw.get("release_date"),
                        "cover_xl": ent["cover_url"],
                    }
                    tracks_raw = (raw.get("tracks") or {}).get("data") or []
                    out = {
                        "entity": ent,
                        "items": [parse_deezer_track(t, album_ctx=ctx) for t in tracks_raw if isinstance(t, dict) and t.get("id")],
                        "continuation": "",
                    }
                elif args.subtype == "playlist":
                    out = parse_deezer_playlist(raw)
                else:
                    trk = parse_deezer_track(raw)
                    out = {"entity": trk, "items": [trk], "continuation": ""}
            else:
                out = parse_itunes_lookup(raw, args.subtype)
        print(json.dumps(out))
        return 0

    if args.cmd == "search":
        q = " ".join(args.query).strip()
        try:
            items = search_catalog(q, args.catalog, args.type, args.limit)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(items))
        else:
            for item in items:
                print(entity_to_tsv_row(item, include_tag=args.tsv_tag))
        return 0

    if args.cmd == "browse":
        try:
            res = browse_catalog(args.ref)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(res))
        elif args.tsv:
            for item in res.get("items") or []:
                print(entity_to_tsv_row(item, include_tag=True))
        else:
            ent = res.get("entity") or {}
            print(json.dumps(res))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
