from __future__ import annotations

import argparse
import json
import subprocess
import re
from pathlib import Path
from urllib.parse import quote


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert SID subset to OGG via VLC and generate catalog JSON."
    )
    parser.add_argument(
        "--sid-catalog",
        default="music/music_catalog_c64_sid.json",
        help="Input SID catalog JSON (default: music/music_catalog_c64_sid.json)",
    )
    parser.add_argument(
        "--out-root",
        default="music/C64Audio_OGG",
        help="Output root for converted OGG files (default: music/C64Audio_OGG)",
    )
    parser.add_argument(
        "--out-catalog",
        default="music/music_catalog_c64_ogg_subset.json",
        help="Output OGG catalog JSON path (default: music/music_catalog_c64_ogg_subset.json)",
    )
    parser.add_argument(
        "--sid-filter",
        choices=["all", "psid", "rsid"],
        default="psid",
        help="Filter tracks by SID type before conversion (default: psid)",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=120,
        help="Maximum number of tracks to include in subset (default: 120)",
    )
    parser.add_argument(
        "--pick",
        action="append",
        default=[],
        help="Query a song name and interactively choose a matching SID to add to the catalog. Can be repeated.",
    )
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=75,
        help="Max duration in seconds per output file (default: 75)",
    )
    parser.add_argument(
        "--vlc-path",
        default=r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        help="Path to vlc.exe",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-convert files even if output already exists",
    )
    return parser.parse_args()


def to_web_path(path: Path) -> str:
    return "/".join(quote(p) for p in path.as_posix().split("/"))


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_text(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_track_matches(tracks: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    needle = normalize_text(query)
    if not needle:
        return []

    matches: list[dict[str, object]] = []
    for track in tracks:
        haystacks = [
            normalize_text(track.get("title")),
            normalize_text(track.get("author")),
            normalize_text(track.get("id")),
            normalize_text(track.get("released")),
        ]
        if any(needle in hay for hay in haystacks):
            matches.append(track)

    def sort_key(track: dict[str, object]) -> tuple[int, str, str, str]:
        title = normalize_text(track.get("title"))
        author = normalize_text(track.get("author"))
        rel_id = normalize_text(track.get("id"))
        exact_hit = 0 if needle == title or needle == rel_id else 1
        return (exact_hit, title, author, rel_id)

    matches.sort(key=sort_key)
    return matches


def choose_track_interactively(query: str, matches: list[dict[str, object]]) -> dict[str, object] | None:
    if not matches:
        print(f"query '{query}': no matches")
        return None

    if len(matches) == 1:
        track = matches[0]
        print(f"query '{query}': auto-selected 1 match -> {track.get('title')} | {track.get('author')} | {track.get('id')}")
        return track

    print(f"query '{query}': {len(matches)} matches")
    for index, track in enumerate(matches, start=1):
        print(f"  {index:>2}. {track.get('title')} | {track.get('author')} | {track.get('id')}")

    while True:
        choice = input(f"Select track for '{query}' [1-{len(matches)}, Enter=1, s=skip]: ").strip().lower()
        if choice in {"s", "skip", "q", "quit"}:
            return None
        if not choice:
            return matches[0]
        if choice.isdigit():
            selected_index = int(choice)
            if 1 <= selected_index <= len(matches):
                return matches[selected_index - 1]
        print("Invalid choice, try again.")


def convert_with_vlc(vlc: Path, sid_in: Path, ogg_out: Path, duration_sec: int) -> bool:
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


def main() -> None:
    args = parse_args()
    workspace_root = Path.cwd().resolve()

    sid_catalog_path = Path(args.sid_catalog).resolve()
    out_root = Path(args.out_root).resolve()
    out_catalog = Path(args.out_catalog).resolve()
    vlc_path = Path(args.vlc_path)

    if not sid_catalog_path.exists():
        raise FileNotFoundError(f"SID catalog not found: {sid_catalog_path}")
    if not vlc_path.exists():
        raise FileNotFoundError(f"vlc.exe not found: {vlc_path}")

    sid_catalog = json.loads(sid_catalog_path.read_text(encoding="utf-8"))
    source_root = Path(str(sid_catalog.get("source", ""))).resolve()
    tracks = list(sid_catalog.get("tracks", []))

    selected: list[dict[str, object]] = []

    if args.pick:
        for query in args.pick:
            matches = find_track_matches(tracks, query)
            chosen = choose_track_interactively(query, matches)
            if chosen and chosen.get("id"):
                rel_id = str(chosen.get("id"))
                if not any(str(existing.get("id")) == rel_id for existing in selected):
                    selected.append(chosen)
    else:
        if args.sid_filter != "all":
            tracks = [t for t in tracks if str(t.get("sidType", "")).upper() == args.sid_filter.upper()]

        tracks.sort(key=lambda t: str(t.get("id", "")).lower())
        selected = tracks[: max(0, args.max_tracks)]

    converted = 0
    skipped_existing = 0
    failed = 0
    out_tracks: list[dict[str, object]] = []

    for idx, t in enumerate(selected, start=1):
        rel_id = str(t.get("id", ""))
        sid_in = source_root / rel_id
        if not sid_in.exists():
            failed += 1
            continue

        rel_ogg = Path(rel_id).with_suffix(".ogg")
        ogg_out = out_root / rel_ogg

        ok = False
        if ogg_out.exists() and ogg_out.stat().st_size > 1024 and not args.overwrite:
            ok = True
            skipped_existing += 1
        else:
            ok = convert_with_vlc(vlc_path, sid_in, ogg_out, args.duration_sec)

        if not ok:
            failed += 1
            continue

        converted += 1
        out_tracks.append(
            {
                "id": rel_ogg.as_posix(),
                "title": t.get("title") or Path(rel_id).stem,
                "author": t.get("author") or "Unknown",
                "released": t.get("released") or "",
                "platform": "Commodore 64",
                "format": "OGG",
                "sourceFormat": "SID",
                "sidType": t.get("sidType") or "",
                "songs": t.get("songs") or 1,
                "license": t.get("license") or "Verify rights per track",
                "url": to_web_path(ogg_out.resolve().relative_to(workspace_root)),
            }
        )

        if idx % 20 == 0:
            print(f"progress: {idx}/{len(selected)} processed, ok={converted}, fail={failed}")

    out_catalog_data = {
        "name": "C64 OGG Subset Catalog",
        "description": "Subset converted from SID to OGG with VLC for browser playback.",
        "sourceSidCatalog": str(sid_catalog_path),
        "exportAudioRoot": str(out_root),
        "trackCount": len(out_tracks),
        "sidFilter": args.sid_filter,
        "maxTracksRequested": args.max_tracks,
        "durationSec": args.duration_sec,
        "tracks": out_tracks,
    }

    ensure_parent(out_catalog)
    out_catalog.write_text(json.dumps(out_catalog_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("C64 OGG subset build done")
    print(f"selected={len(selected)}")
    print(f"converted={converted}")
    print(f"skipped_existing={skipped_existing}")
    print(f"failed={failed}")
    print(f"catalog={out_catalog}")
    print(f"audio_root={out_root}")


if __name__ == "__main__":
    main()
