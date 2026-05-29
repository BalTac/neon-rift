from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


ALLOWED_DIRECT_AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".flac", ".m4a"}
SID_EXTENSIONS = {".sid", ".psid", ".rsid"}
ALLOWED_INPUT_EXTENSIONS = ALLOWED_DIRECT_AUDIO_EXTENSIONS | SID_EXTENSIONS


@dataclass
class CatalogTrack:
    id: str
    title: str
    author: str
    released: str
    platform: str
    format: str
    sourceFormat: str
    sidType: str
    songs: int
    license: str
    url: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "released": self.released,
            "platform": self.platform,
            "format": self.format,
            "sourceFormat": self.sourceFormat,
            "sidType": self.sidType,
            "songs": self.songs,
            "license": self.license,
            "url": self.url,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest new songs from local folders and/or remote URLs into music/in_game "
            "and update music catalog JSON."
        )
    )
    parser.add_argument(
        "--catalog",
        default="music/music_catalog_c64_ogg_subset.json",
        help="Catalog JSON to update (default: music/music_catalog_c64_ogg_subset.json)",
    )
    parser.add_argument(
        "--dest-root",
        default="music/in_game",
        help="Destination folder for in-game audio assets (default: music/in_game)",
    )
    parser.add_argument(
        "--source-dir",
        action="append",
        default=[],
        help="Local directory to scan for audio files (ogg/mp3/... and sid/psid/rsid). Repeatable.",
    )
    parser.add_argument(
        "--ingest",
        action="append",
        default=[],
        help=(
            "Single-entry ingest input (repeatable): local file, local directory, manifest JSON, "
            "or remote URL (http/https)."
        ),
    )
    parser.add_argument(
        "--manifest",
        help=(
            "Optional JSON manifest with full metadata for mixed sources. "
            "Supports localPath and remoteUrl per track."
        ),
    )
    parser.add_argument(
        "--remove",
        action="append",
        default=[],
        help=(
            "Remove track(s) by title, id, url, relative path, absolute path or filename. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace catalog tracks instead of appending/merging.",
    )
    parser.add_argument(
        "--overwrite-files",
        action="store_true",
        help="Overwrite existing destination audio files.",
    )
    parser.add_argument(
        "--vlc-path",
        default=r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        help="Path to vlc.exe used for SID conversion to OGG.",
    )
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=75,
        help="Max duration in seconds for SID->OGG conversion (default: 75).",
    )
    parser.add_argument(
        "--lookup-online",
        action="store_true",
        help="Try MusicBrainz lookup to enrich missing metadata (author/released) for non-SID tracks.",
    )
    parser.add_argument(
        "--lookup-timeout",
        type=int,
        default=8,
        help="Timeout in seconds for online metadata lookup (default: 8).",
    )
    parser.add_argument(
        "--title",
        help="Override title for ingested track(s).",
    )
    parser.add_argument(
        "--author",
        help="Override author/artist for ingested track(s).",
    )
    parser.add_argument(
        "--released",
        help="Override release date/year for ingested track(s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing files.",
    )
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def to_web_path(path: Path) -> str:
    return "/".join(quote(part) for part in path.as_posix().split("/"))


def infer_title_from_name(stem: str) -> str:
    text = stem.replace("_", " ").strip()
    text = re.sub(r"^\s*\d+\s*[-.)]*\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or stem


def compose_preferred_stem(title: str, author: str) -> str:
    safe_title = (title or "").strip()
    safe_author = (author or "").strip()
    if safe_author and safe_author.casefold() != "unknown":
        return f"{safe_author} - {safe_title}".strip(" -")
    return safe_title or "track"


def filename_stem_from_title(title: str) -> str:
    normalized = "_".join(title.strip().split())
    return sanitize_filename(normalized)


def sanitize_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in name)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("._") or "track.ogg"


def unique_destination_path(dest_root: Path, preferred_name: str) -> Path:
    candidate = dest_root / sanitize_filename(preferred_name)
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        numbered = dest_root / f"{stem}_{index}{suffix}"
        if not numbered.exists():
            return numbered
        index += 1


def guess_format_from_suffix(suffix: str) -> str:
    fmt = suffix.replace(".", "").upper()
    return fmt or "OGG"


def read_c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="ignore").strip()


def parse_sid_header(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        data = handle.read(0x76)
    if len(data) < 0x76:
        return {"valid": False, "sidType": "UNKNOWN"}

    magic = data[0:4].decode("ascii", errors="ignore")
    if magic not in {"PSID", "RSID"}:
        return {"valid": False, "sidType": magic or "UNKNOWN"}

    title = read_c_string(data[0x16:0x36])
    author = read_c_string(data[0x36:0x56])
    released = read_c_string(data[0x56:0x76])
    songs = int.from_bytes(data[0x0E:0x10], byteorder="big", signed=False)

    return {
        "valid": True,
        "title": title,
        "author": author,
        "released": released,
        "songs": songs,
        "sidType": magic,
        "sourceFormat": "SID",
        "platform": "Commodore 64",
        "format": "OGG",
        "license": "HVSC archive (verify per-track rights before distribution)",
    }


def _first_list_value(payload: object) -> str:
    if isinstance(payload, list) and payload:
        return str(payload[0]).strip()
    if payload is None:
        return ""
    return str(payload).strip()


def _to_int_or_default(value: object, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    match = re.match(r"^(\d+)", text)
    if not match:
        return default
    return int(match.group(1))


def _decode_id3_text(payload: bytes) -> str:
    if not payload:
        return ""
    encoding = payload[0]
    data = payload[1:]

    try:
        if encoding == 0:
            text = data.decode("latin-1", errors="ignore")
        elif encoding == 1:
            text = data.decode("utf-16", errors="ignore")
        elif encoding == 2:
            text = data.decode("utf-16-be", errors="ignore")
        elif encoding == 3:
            text = data.decode("utf-8", errors="ignore")
        else:
            text = data.decode("latin-1", errors="ignore")
    except Exception:
        return ""

    return text.replace("\x00", "").strip()


def _syncsafe_to_int(raw: bytes) -> int:
    if len(raw) != 4:
        return 0
    return ((raw[0] & 0x7F) << 21) | ((raw[1] & 0x7F) << 14) | ((raw[2] & 0x7F) << 7) | (raw[3] & 0x7F)


def parse_mp3_id3v2_metadata(src: Path) -> dict[str, object]:
    try:
        with src.open("rb") as handle:
            header = handle.read(10)
            if len(header) < 10 or header[0:3] != b"ID3":
                return {}

            version = header[3]
            flags = header[5]
            tag_size = _syncsafe_to_int(header[6:10])
            tag_data = handle.read(tag_size)
    except Exception:
        return {}

    if not tag_data:
        return {}

    offset = 0
    if flags & 0x40 and len(tag_data) >= 4:
        # Extended header size differs across versions; this is a conservative skip.
        ext_size = int.from_bytes(tag_data[0:4], byteorder="big", signed=False)
        if 0 < ext_size <= len(tag_data):
            offset = ext_size

    values: dict[str, str] = {}
    while offset + 10 <= len(tag_data):
        frame_id = tag_data[offset:offset + 4].decode("latin-1", errors="ignore")
        if not frame_id.strip("\x00"):
            break

        size_bytes = tag_data[offset + 4:offset + 8]
        if version == 4:
            frame_size = _syncsafe_to_int(size_bytes)
        else:
            frame_size = int.from_bytes(size_bytes, byteorder="big", signed=False)

        if frame_size <= 0:
            break

        payload_start = offset + 10
        payload_end = payload_start + frame_size
        if payload_end > len(tag_data):
            break

        payload = tag_data[payload_start:payload_end]
        if frame_id in {"TIT2", "TPE1", "TDRC", "TYER", "TRCK"}:
            values[frame_id] = _decode_id3_text(payload)

        offset = payload_end

    title = values.get("TIT2", "").strip()
    author = values.get("TPE1", "").strip()
    released = values.get("TDRC", "").strip() or values.get("TYER", "").strip()
    songs = _to_int_or_default(values.get("TRCK", ""), 1)

    out: dict[str, object] = {}
    if title:
        out["title"] = title
    if author:
        out["author"] = author
    if released:
        out["released"] = released
    out["songs"] = songs
    return out


def infer_author_from_path(src: Path) -> str:
    try:
        parent = src.parent
        grand = parent.parent if parent else None
        if not grand:
            return ""
        candidate = str(grand.name or "").strip()
        low = candidate.casefold()
        if low in {"music", "in_game", ""}:
            return ""
        return candidate
    except Exception:
        return ""


def lookup_musicbrainz_metadata(title: str, author_hint: str, timeout_sec: int) -> dict[str, object]:
    clean_title = str(title or "").strip()
    clean_author = str(author_hint or "").strip()
    if not clean_title:
        return {}

    query_parts = [f'recording:"{clean_title}"']
    if clean_author and clean_author.casefold() != "unknown":
        query_parts.append(f'artist:"{clean_author}"')
    query = " AND ".join(query_parts)

    params = {
        "query": query,
        "fmt": "json",
        "limit": "3",
        "inc": "releases",
    }
    url = "https://musicbrainz.org/ws/2/recording/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "asteroids-ingest/1.0 (local metadata helper)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=max(2, int(timeout_sec))) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception:
        return {}

    recordings = payload.get("recordings", [])
    if not isinstance(recordings, list) or not recordings:
        return {}

    def extract_recording_info(rec: dict[str, object]) -> tuple[str, str, str]:
        mb_title = str(rec.get("title") or "").strip()

        artist_name = ""
        artist_credit = rec.get("artist-credit", [])
        if isinstance(artist_credit, list) and artist_credit:
            artist_name = str((artist_credit[0] or {}).get("name") or "").strip()

        release_date = ""
        release_list = rec.get("releases", [])
        if isinstance(release_list, list) and release_list:
            for rel in release_list:
                date_candidate = str((rel or {}).get("date") or "").strip()
                if date_candidate:
                    release_date = date_candidate
                    break
        if not release_date:
            release_date = str(rec.get("first-release-date") or "").strip()

        return mb_title, artist_name, release_date

    best: dict[str, object] | None = None
    best_score = -1
    for rec in recordings:
        if not isinstance(rec, dict):
            continue
        mb_title, artist_name, release_date = extract_recording_info(rec)
        score = 0
        if mb_title and mb_title.casefold() == clean_title.casefold():
            score += 2
        if clean_author and artist_name and clean_author.casefold() in artist_name.casefold():
            score += 2
        if release_date:
            score += 1
        if score > best_score:
            best = rec
            best_score = score

    if not best:
        return {}

    out: dict[str, object] = {}
    mb_title, artist_name, release_date = extract_recording_info(best)

    if mb_title:
        out["title"] = mb_title
    if artist_name:
        out["author"] = artist_name
    if release_date:
        out["released"] = release_date

    return out


def enrich_with_online_metadata(metadata: dict[str, object], timeout_sec: int) -> dict[str, object]:
    title = str(metadata.get("title") or "").strip()
    author = str(metadata.get("author") or "").strip()
    released = str(metadata.get("released") or "").strip()

    needs_lookup = bool(title) and ((not author) or author.casefold() == "unknown" or (not released))
    if not needs_lookup:
        return metadata

    online = lookup_musicbrainz_metadata(title=title, author_hint=author, timeout_sec=timeout_sec)
    if not online:
        return metadata

    # Keep local/user-provided fields as priority, but let online fill empty values.
    return merge_online_with_local(online, metadata)


def extract_embedded_audio_metadata(src: Path) -> dict[str, object]:
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return {}

    try:
        audio = MutagenFile(str(src), easy=True)
    except Exception:
        return {}

    if not audio or not getattr(audio, "tags", None):
        if src.suffix.lower() == ".mp3":
            return parse_mp3_id3v2_metadata(src)
        return {}

    tags = audio.tags
    title = _first_list_value(tags.get("title"))
    author = _first_list_value(tags.get("artist"))
    released = _first_list_value(tags.get("date")) or _first_list_value(tags.get("year"))
    songs = _to_int_or_default(_first_list_value(tags.get("tracknumber")), 1)

    metadata: dict[str, object] = {}
    if title:
        metadata["title"] = title
    if author:
        metadata["author"] = author
    if released:
        metadata["released"] = released
    metadata["songs"] = songs
    return metadata


def convert_sid_to_ogg(vlc: Path, sid_in: Path, ogg_out: Path, duration_sec: int) -> bool:
    ensure_parent(ogg_out)
    sout = (
        f"#transcode{{acodec=vorb,ab=160,channels=2,samplerate=44100}}:"
        f"std{{access=file,mux=ogg,dst='{ogg_out.as_posix()}'}}"
    )

    cmd = [
        str(vlc),
        "-I",
        "dummy",
        "--no-video",
        "--play-and-exit",
        f"--run-time={duration_sec}",
        str(sid_in),
        "--sout",
        sout,
        "vlc://quit",
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(45, duration_sec + 20),
            check=False,
        )
        if result.returncode != 0:
            return False
        if not ogg_out.exists() or ogg_out.stat().st_size < 1024:
            return False
        return True
    except subprocess.TimeoutExpired:
        return False


def load_catalog(catalog_path: Path, dest_root: Path) -> dict[str, object]:
    if catalog_path.exists():
        return json.loads(catalog_path.read_text(encoding="utf-8"))

    return {
        "name": "C64 OGG Subset Catalog",
        "description": "In-game music catalog.",
        "exportAudioRoot": str(dest_root.resolve()),
        "trackCount": 0,
        "sidFilter": "psid",
        "maxTracksRequested": 120,
        "durationSec": 20,
        "tracks": [],
    }


def track_from_dest(
    workspace_root: Path,
    dest_file: Path,
    metadata: dict[str, object] | None = None,
) -> CatalogTrack:
    metadata = metadata or {}
    file_suffix = dest_file.suffix.lower()
    fmt = str(metadata.get("format") or guess_format_from_suffix(file_suffix))
    title = str(metadata.get("title") or infer_title_from_name(dest_file.stem))
    track_id = str(metadata.get("id") or dest_file.name)

    rel_dest = dest_file.resolve().relative_to(workspace_root)
    url = to_web_path(rel_dest)

    return CatalogTrack(
        id=track_id,
        title=title,
        author=str(metadata.get("author") or "Unknown"),
        released=str(metadata.get("released") or ""),
        platform=str(metadata.get("platform") or "General Audio"),
        format=fmt,
        sourceFormat=str(metadata.get("sourceFormat") or fmt),
        sidType=str(metadata.get("sidType") or ""),
        songs=int(metadata.get("songs") or 1),
        license=str(metadata.get("license") or "User provided file (verify rights before distribution)"),
        url=url,
    )


def merge_metadata(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    merged.update(override)
    return merged


def merge_online_with_local(online: dict[str, object], local: dict[str, object]) -> dict[str, object]:
    merged = dict(online)
    for key, value in local.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value
    return merged


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def copy_local_file(
    src: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    preferred_stem: str | None = None,
) -> Path:
    base_stem = preferred_stem or src.stem
    preferred = sanitize_filename(f"{filename_stem_from_title(base_stem)}{src.suffix.lower()}")
    target = (dest_root / preferred)
    if target.exists() and not overwrite:
        return target
    if target.exists() and overwrite:
        if not dry_run:
            shutil.copy2(src, target)
        return target

    target = unique_destination_path(dest_root, preferred)
    if not dry_run:
        ensure_parent(target)
        shutil.copy2(src, target)
    return target


def convert_local_sid(
    src: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
) -> tuple[Path, dict[str, object]]:
    sid_meta = parse_sid_header(src)
    preferred_title = str(sid_meta.get("title") or infer_title_from_name(src.stem)).strip()
    target_name = f"{filename_stem_from_title(preferred_title)}.ogg"
    target = dest_root / sanitize_filename(target_name)

    if target.exists() and not overwrite:
        return target, sid_meta

    if target.exists() and overwrite:
        if not dry_run:
            ok = convert_sid_to_ogg(vlc_path, src, target, duration_sec)
            if not ok:
                raise RuntimeError(f"SID conversion failed: {src}")
        return target, sid_meta

    target = unique_destination_path(dest_root, target_name)
    if not dry_run:
        ok = convert_sid_to_ogg(vlc_path, src, target, duration_sec)
        if not ok:
            raise RuntimeError(f"SID conversion failed: {src}")
    return target, sid_meta


def download_remote_file(
    url: str,
    dest_name: str,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    preferred_stem: str | None = None,
) -> Path:
    extension = Path(dest_name).suffix.lower() or ".ogg"
    base_stem = preferred_stem or Path(dest_name).stem
    preferred = sanitize_filename(f"{filename_stem_from_title(base_stem)}{extension}")
    target = dest_root / preferred
    if target.exists() and not overwrite:
        return target
    if target.exists() and overwrite:
        if not dry_run:
            urllib.request.urlretrieve(url, target)
        return target

    target = unique_destination_path(dest_root, preferred)
    if not dry_run:
        ensure_parent(target)
        urllib.request.urlretrieve(url, target)
    return target


def convert_remote_sid(
    url: str,
    parsed_name: str,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
) -> tuple[Path, dict[str, object]]:
    initial_stem = infer_title_from_name(Path(parsed_name).stem or "remote_track")
    target_name = f"{filename_stem_from_title(initial_stem)}.ogg"
    target = dest_root / sanitize_filename(target_name)
    sid_meta: dict[str, object] = {}

    if target.exists() and not overwrite:
        return target, sid_meta

    if target.exists() and overwrite and dry_run:
        return target, sid_meta

    if target.exists() and overwrite and not dry_run:
        with tempfile.TemporaryDirectory(prefix="sid_ingest_") as tmp:
            tmp_sid = Path(tmp) / sanitize_filename(parsed_name)
            ensure_parent(tmp_sid)
            urllib.request.urlretrieve(url, tmp_sid)
            sid_meta = parse_sid_header(tmp_sid)
            sid_title = str(sid_meta.get("title") or initial_stem).strip()
            target = dest_root / sanitize_filename(f"{filename_stem_from_title(sid_title)}.ogg")
            ok = convert_sid_to_ogg(vlc_path, tmp_sid, target, duration_sec)
            if not ok:
                raise RuntimeError(f"SID conversion failed for remote URL: {url}")
        return target, sid_meta

    target = unique_destination_path(dest_root, target_name)
    if dry_run:
        return target, sid_meta

    with tempfile.TemporaryDirectory(prefix="sid_ingest_") as tmp:
        tmp_sid = Path(tmp) / sanitize_filename(parsed_name)
        ensure_parent(tmp_sid)
        urllib.request.urlretrieve(url, tmp_sid)
        sid_meta = parse_sid_header(tmp_sid)
        sid_title = str(sid_meta.get("title") or initial_stem).strip()
        target = unique_destination_path(dest_root, f"{filename_stem_from_title(sid_title)}.ogg")
        ok = convert_sid_to_ogg(vlc_path, tmp_sid, target, duration_sec)
        if not ok:
            raise RuntimeError(f"SID conversion failed for remote URL: {url}")
    return target, sid_meta


def ingest_local_file(
    src: Path,
    workspace_root: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
    lookup_online: bool,
    lookup_timeout: int,
    metadata: dict[str, object] | None = None,
) -> CatalogTrack:
    suffix = src.suffix.lower()
    if suffix not in ALLOWED_INPUT_EXTENSIONS:
        raise ValueError(f"Unsupported local file extension: {src}")

    metadata = metadata or {}
    if suffix in SID_EXTENSIONS:
        dest_file, sid_meta = convert_local_sid(
            src=src,
            dest_root=dest_root,
            overwrite=overwrite,
            dry_run=dry_run,
            vlc_path=vlc_path,
            duration_sec=duration_sec,
        )
        merged = merge_metadata(sid_meta, metadata)
        return track_from_dest(workspace_root, dest_file, merged)

    base_direct = {
        "platform": "General Audio",
        "sourceFormat": guess_format_from_suffix(suffix),
        "sidType": "",
        "license": "User provided file (verify rights before distribution)",
    }
    embedded = extract_embedded_audio_metadata(src)
    merged = merge_metadata(base_direct, embedded)
    merged = merge_metadata(merged, metadata)

    if not str(merged.get("author") or "").strip():
        inferred_author = infer_author_from_path(src)
        if inferred_author:
            merged["author"] = inferred_author

    if lookup_online:
        merged = enrich_with_online_metadata(merged, timeout_sec=lookup_timeout)

    preferred_title = str(merged.get("title") or infer_title_from_name(src.stem)).strip()
    preferred_author = str(merged.get("author") or "").strip()
    preferred_stem = compose_preferred_stem(preferred_title, preferred_author)
    dest_file = copy_local_file(
        src,
        dest_root,
        overwrite=overwrite,
        dry_run=dry_run,
        preferred_stem=preferred_stem,
    )
    enriched = merge_metadata(merged, {"title": preferred_title})
    return track_from_dest(workspace_root, dest_file, enriched)


def ingest_remote_url(
    remote_url: str,
    workspace_root: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
    lookup_online: bool,
    lookup_timeout: int,
    metadata: dict[str, object] | None = None,
) -> CatalogTrack:
    parsed = urlparse(remote_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"remote URL must be http/https: {remote_url}")

    metadata = metadata or {}
    candidate_name = Path(parsed.path).name or "remote_track.ogg"
    candidate_suffix = Path(candidate_name).suffix.lower()

    if candidate_suffix in SID_EXTENSIONS:
        dest_file, sid_meta = convert_remote_sid(
            url=remote_url,
            parsed_name=candidate_name,
            dest_root=dest_root,
            overwrite=overwrite,
            dry_run=dry_run,
            vlc_path=vlc_path,
            duration_sec=duration_sec,
        )
        merged = merge_metadata(sid_meta, metadata)
        return track_from_dest(workspace_root, dest_file, merged)

    if candidate_suffix not in ALLOWED_DIRECT_AUDIO_EXTENSIONS:
        candidate_name = f"{Path(candidate_name).stem or 'remote_track'}.ogg"

    base_direct = {
        "platform": "General Audio",
        "sourceFormat": guess_format_from_suffix(Path(candidate_name).suffix.lower() or ".ogg"),
        "sidType": "",
        "license": "User provided file (verify rights before distribution)",
    }
    merged = merge_metadata(base_direct, metadata)

    if lookup_online:
        merged = enrich_with_online_metadata(merged, timeout_sec=lookup_timeout)

    preferred_title = str(merged.get("title") or infer_title_from_name(Path(candidate_name).stem)).strip()
    preferred_author = str(merged.get("author") or "").strip()
    preferred_stem = compose_preferred_stem(preferred_title, preferred_author)

    dest_file = download_remote_file(
        remote_url,
        dest_name=candidate_name,
        dest_root=dest_root,
        overwrite=overwrite,
        dry_run=dry_run,
        preferred_stem=preferred_stem,
    )

    if not dry_run:
        merged = merge_metadata(merged, extract_embedded_audio_metadata(dest_file))
        preferred_title = str(merged.get("title") or preferred_title).strip()

    enriched = merge_metadata(merged, {"title": preferred_title})
    return track_from_dest(workspace_root, dest_file, enriched)


def build_tracks_from_dirs(
    source_dirs: list[str],
    workspace_root: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
    lookup_online: bool,
    lookup_timeout: int,
    metadata_overrides: dict[str, object] | None = None,
) -> list[CatalogTrack]:
    tracks: list[CatalogTrack] = []
    metadata_overrides = metadata_overrides or {}
    for source in source_dirs:
        src_dir = Path(source).expanduser().resolve()
        if not src_dir.exists() or not src_dir.is_dir():
            raise FileNotFoundError(f"Source dir not found: {src_dir}")

        for file_path in sorted(src_dir.rglob("*")):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in ALLOWED_INPUT_EXTENSIONS:
                continue
            tracks.append(
                ingest_local_file(
                    src=file_path,
                    workspace_root=workspace_root,
                    dest_root=dest_root,
                    overwrite=overwrite,
                    dry_run=dry_run,
                    vlc_path=vlc_path,
                    duration_sec=duration_sec,
                    lookup_online=lookup_online,
                    lookup_timeout=lookup_timeout,
                    metadata=metadata_overrides,
                )
            )
    return tracks


def build_tracks_from_manifest(
    manifest_path: Path,
    workspace_root: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
    lookup_online: bool,
    lookup_timeout: int,
    metadata_overrides: dict[str, object] | None = None,
) -> list[CatalogTrack]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = payload.get("tracks", [])
    if not isinstance(entries, list):
        raise ValueError("Manifest must contain a list field named 'tracks'.")

    tracks: list[CatalogTrack] = []
    metadata_overrides = metadata_overrides or {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Each manifest track must be an object.")

        local_path = item.get("localPath")
        remote_url = item.get("remoteUrl")
        if not local_path and not remote_url:
            raise ValueError("Each manifest track needs localPath or remoteUrl.")

        metadata = dict(item)
        metadata.pop("localPath", None)
        metadata.pop("remoteUrl", None)
        metadata = merge_metadata(metadata, metadata_overrides)

        if local_path:
            src = Path(str(local_path)).expanduser().resolve()
            if not src.exists() or not src.is_file():
                raise FileNotFoundError(f"Manifest localPath not found: {src}")
            tracks.append(
                ingest_local_file(
                    src=src,
                    workspace_root=workspace_root,
                    dest_root=dest_root,
                    overwrite=overwrite,
                    dry_run=dry_run,
                    vlc_path=vlc_path,
                    duration_sec=duration_sec,
                    lookup_online=lookup_online,
                    lookup_timeout=lookup_timeout,
                    metadata=metadata,
                )
            )
            continue

        tracks.append(
            ingest_remote_url(
                remote_url=str(remote_url),
                workspace_root=workspace_root,
                dest_root=dest_root,
                overwrite=overwrite,
                dry_run=dry_run,
                vlc_path=vlc_path,
                duration_sec=duration_sec,
                lookup_online=lookup_online,
                lookup_timeout=lookup_timeout,
                metadata=metadata,
            )
        )

    return tracks


def build_tracks_from_ingest_entries(
    ingest_entries: list[str],
    workspace_root: Path,
    dest_root: Path,
    overwrite: bool,
    dry_run: bool,
    vlc_path: Path,
    duration_sec: int,
    lookup_online: bool,
    lookup_timeout: int,
    metadata_overrides: dict[str, object] | None = None,
) -> list[CatalogTrack]:
    tracks: list[CatalogTrack] = []
    metadata_overrides = metadata_overrides or {}
    for entry in ingest_entries:
        candidate = str(entry).strip()
        if not candidate:
            continue

        if is_http_url(candidate):
            tracks.append(
                ingest_remote_url(
                    remote_url=candidate,
                    workspace_root=workspace_root,
                    dest_root=dest_root,
                    overwrite=overwrite,
                    dry_run=dry_run,
                    vlc_path=vlc_path,
                    duration_sec=duration_sec,
                    lookup_online=lookup_online,
                    lookup_timeout=lookup_timeout,
                    metadata=metadata_overrides,
                )
            )
            continue

        local = Path(candidate).expanduser().resolve()
        if not local.exists():
            raise FileNotFoundError(f"Ingest path not found: {local}")

        if local.is_dir():
            tracks.extend(
                build_tracks_from_dirs(
                    source_dirs=[str(local)],
                    workspace_root=workspace_root,
                    dest_root=dest_root,
                    overwrite=overwrite,
                    dry_run=dry_run,
                    vlc_path=vlc_path,
                    duration_sec=duration_sec,
                    lookup_online=lookup_online,
                    lookup_timeout=lookup_timeout,
                    metadata_overrides=metadata_overrides,
                )
            )
            continue

        if local.suffix.lower() == ".json":
            tracks.extend(
                build_tracks_from_manifest(
                    manifest_path=local,
                    workspace_root=workspace_root,
                    dest_root=dest_root,
                    overwrite=overwrite,
                    dry_run=dry_run,
                    vlc_path=vlc_path,
                    duration_sec=duration_sec,
                    lookup_online=lookup_online,
                    lookup_timeout=lookup_timeout,
                    metadata_overrides=metadata_overrides,
                )
            )
            continue

        tracks.append(
            ingest_local_file(
                src=local,
                workspace_root=workspace_root,
                dest_root=dest_root,
                overwrite=overwrite,
                dry_run=dry_run,
                vlc_path=vlc_path,
                duration_sec=duration_sec,
                lookup_online=lookup_online,
                lookup_timeout=lookup_timeout,
                metadata=metadata_overrides,
            )
        )

    return tracks


def detect_requires_vlc(args: argparse.Namespace) -> bool:
    if args.source_dir:
        for source in args.source_dir:
            src_dir = Path(source).expanduser().resolve()
            if src_dir.exists() and src_dir.is_dir():
                if any(p.is_file() and p.suffix.lower() in SID_EXTENSIONS for p in src_dir.rglob("*")):
                    return True

    if args.manifest:
        payload = json.loads(Path(args.manifest).resolve().read_text(encoding="utf-8"))
        for item in payload.get("tracks", []):
            if not isinstance(item, dict):
                continue
            local_path = str(item.get("localPath") or "")
            remote_url = str(item.get("remoteUrl") or "")
            if Path(local_path).suffix.lower() in SID_EXTENSIONS:
                return True
            if Path(urlparse(remote_url).path).suffix.lower() in SID_EXTENSIONS:
                return True

    for entry in args.ingest:
        candidate = str(entry).strip()
        if not candidate:
            continue
        if is_http_url(candidate):
            if Path(urlparse(candidate).path).suffix.lower() in SID_EXTENSIONS:
                return True
            continue

        local = Path(candidate).expanduser().resolve()
        if not local.exists():
            continue
        if local.is_dir():
            if any(p.is_file() and p.suffix.lower() in SID_EXTENSIONS for p in local.rglob("*")):
                return True
            continue
        if local.suffix.lower() == ".json":
            payload = json.loads(local.read_text(encoding="utf-8"))
            for item in payload.get("tracks", []):
                if not isinstance(item, dict):
                    continue
                local_path = str(item.get("localPath") or "")
                remote_url = str(item.get("remoteUrl") or "")
                if Path(local_path).suffix.lower() in SID_EXTENSIONS:
                    return True
                if Path(urlparse(remote_url).path).suffix.lower() in SID_EXTENSIONS:
                    return True
            continue
        if local.suffix.lower() in SID_EXTENSIONS:
            return True

    return False


def dedupe_tracks(tracks: list[dict[str, object]]) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for track in tracks:
        track_id = str(track.get("id") or "").strip()
        if not track_id:
            continue
        by_id[track_id] = track
    return [by_id[key] for key in sorted(by_id.keys(), key=lambda v: v.lower())]


def track_file_path(track: dict[str, object], workspace_root: Path, dest_root: Path) -> Path:
    url_value = str(track.get("url") or "").strip()
    if url_value:
        decoded = unquote(url_value)
        as_path = Path(decoded.replace("/", "\\"))
        if as_path.is_absolute():
            return as_path.resolve()
        return (workspace_root / as_path).resolve()

    track_id = str(track.get("id") or "").strip()
    if track_id:
        candidate = Path(track_id)
        if candidate.suffix:
            return (dest_root / candidate.name).resolve()
    return (dest_root / "unknown_track.ogg").resolve()


def remove_tracks_and_files(
    tracks: list[dict[str, object]],
    remove_entries: list[str],
    workspace_root: Path,
    dest_root: Path,
    dry_run: bool,
) -> tuple[list[dict[str, object]], int, int, list[str]]:
    if not remove_entries:
        return tracks, 0, 0, []

    working_tracks = list(tracks)
    to_remove_indices: set[int] = set()
    unmatched: list[str] = []

    for raw_entry in remove_entries:
        query = str(raw_entry or "").strip()
        if not query:
            continue

        query_lower = query.casefold()
        query_is_url = is_http_url(query)

        query_abs: Path | None = None
        query_rel_workspace: str | None = None
        query_rel_dest: str | None = None
        query_basename = Path(query).name.casefold() if Path(query).name else ""

        if not query_is_url:
            maybe_path = Path(query)
            if maybe_path.is_absolute() or any(sep in query for sep in ("/", "\\")):
                query_abs = maybe_path.expanduser().resolve()
            else:
                query_rel_workspace = query.replace("\\", "/").casefold()
                query_rel_dest = query.replace("\\", "/").casefold()

        matched_any = False
        for idx, track in enumerate(working_tracks):
            if idx in to_remove_indices:
                continue

            title = str(track.get("title") or "").strip()
            track_id = str(track.get("id") or "").strip()
            track_url = str(track.get("url") or "").strip()
            abs_path = track_file_path(track, workspace_root, dest_root)

            rel_workspace = ""
            rel_dest = ""
            try:
                rel_workspace = abs_path.relative_to(workspace_root).as_posix().casefold()
            except ValueError:
                rel_workspace = ""
            try:
                rel_dest = abs_path.relative_to(dest_root).as_posix().casefold()
            except ValueError:
                rel_dest = ""

            values = {
                title.casefold(),
                track_id.casefold(),
                track_url.casefold(),
                abs_path.as_posix().casefold(),
                abs_path.name.casefold(),
                rel_workspace,
                rel_dest,
            }
            values.discard("")

            direct_match = query_lower in values
            path_match = query_abs is not None and abs_path.resolve() == query_abs
            basename_match = bool(query_basename) and abs_path.name.casefold() == query_basename

            if direct_match or path_match or basename_match:
                to_remove_indices.add(idx)
                matched_any = True

        if not matched_any:
            unmatched.append(query)

    kept_tracks: list[dict[str, object]] = [
        track for idx, track in enumerate(working_tracks) if idx not in to_remove_indices
    ]
    removed_tracks: list[dict[str, object]] = [
        track for idx, track in enumerate(working_tracks) if idx in to_remove_indices
    ]

    kept_file_paths: set[str] = {
        str(track_file_path(track, workspace_root, dest_root).resolve()).casefold()
        for track in kept_tracks
    }

    removed_files = 0
    for track in removed_tracks:
        file_path = track_file_path(track, workspace_root, dest_root).resolve()
        file_key = str(file_path).casefold()
        if file_key in kept_file_paths:
            continue
        if file_path.exists():
            removed_files += 1
            if not dry_run:
                file_path.unlink()

    return kept_tracks, len(removed_tracks), removed_files, unmatched


def main() -> None:
    args = parse_args()
    workspace_root = Path.cwd().resolve()
    catalog_path = Path(args.catalog).resolve()
    dest_root = Path(args.dest_root).resolve()
    vlc_path = Path(args.vlc_path)

    requires_vlc = detect_requires_vlc(args)

    if requires_vlc and not args.dry_run and not vlc_path.exists():
        raise FileNotFoundError(f"vlc.exe not found: {vlc_path}")

    if not args.dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    if not args.source_dir and not args.manifest and not args.ingest and not args.remove:
        raise ValueError("Provide at least one input: --ingest, --source-dir, --manifest or --remove.")

    metadata_overrides: dict[str, object] = {}
    if args.title and str(args.title).strip():
        metadata_overrides["title"] = str(args.title).strip()
    if args.author and str(args.author).strip():
        metadata_overrides["author"] = str(args.author).strip()
    if args.released and str(args.released).strip():
        metadata_overrides["released"] = str(args.released).strip()

    catalog = load_catalog(catalog_path, dest_root)
    existing_tracks = list(catalog.get("tracks", [])) if isinstance(catalog.get("tracks", []), list) else []

    removed_tracks = 0
    removed_files = 0
    unmatched_remove_entries: list[str] = []
    if args.remove:
        existing_tracks, removed_tracks, removed_files, unmatched_remove_entries = remove_tracks_and_files(
            tracks=existing_tracks,
            remove_entries=args.remove,
            workspace_root=workspace_root,
            dest_root=dest_root,
            dry_run=args.dry_run,
        )

    new_tracks: list[CatalogTrack] = []
    if args.source_dir:
        new_tracks.extend(
            build_tracks_from_dirs(
                source_dirs=args.source_dir,
                workspace_root=workspace_root,
                dest_root=dest_root,
                overwrite=args.overwrite_files,
                dry_run=args.dry_run,
                vlc_path=vlc_path,
                duration_sec=args.duration_sec,
                lookup_online=args.lookup_online,
                lookup_timeout=args.lookup_timeout,
                metadata_overrides=metadata_overrides,
            )
        )

    if args.manifest:
        new_tracks.extend(
            build_tracks_from_manifest(
                manifest_path=Path(args.manifest).resolve(),
                workspace_root=workspace_root,
                dest_root=dest_root,
                overwrite=args.overwrite_files,
                dry_run=args.dry_run,
                vlc_path=vlc_path,
                duration_sec=args.duration_sec,
                lookup_online=args.lookup_online,
                lookup_timeout=args.lookup_timeout,
                metadata_overrides=metadata_overrides,
            )
        )

    if args.ingest:
        new_tracks.extend(
            build_tracks_from_ingest_entries(
                ingest_entries=args.ingest,
                workspace_root=workspace_root,
                dest_root=dest_root,
                overwrite=args.overwrite_files,
                dry_run=args.dry_run,
                vlc_path=vlc_path,
                duration_sec=args.duration_sec,
                lookup_online=args.lookup_online,
                lookup_timeout=args.lookup_timeout,
                metadata_overrides=metadata_overrides,
            )
        )

    new_track_dicts = [track.to_dict() for track in new_tracks]
    final_tracks = new_track_dicts if args.replace else (existing_tracks + new_track_dicts)
    final_tracks = dedupe_tracks(final_tracks)

    catalog["exportAudioRoot"] = str(dest_root)
    catalog["trackCount"] = len(final_tracks)
    catalog["tracks"] = final_tracks

    if not args.dry_run:
        ensure_parent(catalog_path)
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ingest completed")
    print(f"catalog={catalog_path}")
    print(f"dest_root={dest_root}")
    print(f"new_tracks={len(new_track_dicts)}")
    print(f"total_tracks={len(final_tracks)}")
    print(f"removed_tracks={removed_tracks}")
    print(f"removed_files={removed_files}")
    print(f"mode={'replace' if args.replace else 'merge'}")
    print(f"dry_run={bool(args.dry_run)}")
    if unmatched_remove_entries:
        print(f"remove_unmatched={json.dumps(unmatched_remove_entries, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
