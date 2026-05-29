from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export C64Music tree under music/ and build SID catalog JSON.")
    parser.add_argument(
        "--source",
        default="music/C64Music",
        help="Source HVSC-like directory (default: music/C64Music)",
    )
    parser.add_argument(
        "--dest",
        default="music/C64Music_Export",
        help="Destination directory under music/ (default: music/C64Music_Export)",
    )
    parser.add_argument(
        "--catalog",
        default="music/music_catalog_c64_sid.json",
        help="Output catalog JSON path (default: music/music_catalog_c64_sid.json)",
    )
    parser.add_argument(
        "--copy-mode",
        choices=["hardlink", "copy"],
        default="hardlink",
        help="Export mode: hardlink (default) or copy",
    )
    parser.add_argument(
        "--sid-filter",
        choices=["all", "psid", "rsid"],
        default="all",
        help="Catalog filter by SID header (default: all)",
    )
    return parser.parse_args()


def to_web_path(path: Path) -> str:
    parts = [quote(part) for part in path.as_posix().split("/")]
    return "/".join(parts)


def read_c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="ignore").strip()


def parse_sid_header(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        data = handle.read(0x76)
    if len(data) < 0x76:
        return {"valid": False, "sid_type": "UNKNOWN"}

    magic = data[0:4].decode("ascii", errors="ignore")
    if magic not in {"PSID", "RSID"}:
        return {"valid": False, "sid_type": magic or "UNKNOWN"}

    title = read_c_string(data[0x16:0x36])
    author = read_c_string(data[0x36:0x56])
    released = read_c_string(data[0x56:0x76])
    songs = int.from_bytes(data[0x0E:0x10], byteorder="big", signed=False)
    start_song = int.from_bytes(data[0x10:0x12], byteorder="big", signed=False)

    return {
        "valid": True,
        "sid_type": magic,
        "title": title,
        "author": author,
        "released": released,
        "songs": songs,
        "start_song": start_song,
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def link_or_copy(src: Path, dst: Path, copy_mode: str) -> str:
    if dst.exists():
        return "skip"

    ensure_parent(dst)
    if copy_mode == "copy":
        shutil.copy2(src, dst)
        return "copy"

    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy-fallback"


def build_export_and_catalog(
    source: Path,
    dest: Path,
    catalog_path: Path,
    copy_mode: str,
    sid_filter: str,
) -> dict[str, int]:
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Source not found or not a directory: {source}")

    file_count = 0
    sid_count = 0
    hardlink_count = 0
    copy_count = 0
    copy_fallback_count = 0

    tracks: list[dict[str, object]] = []

    workspace_root = Path.cwd().resolve()

    for src in source.rglob("*"):
        if not src.is_file():
            continue

        rel = src.relative_to(source)
        dst = dest / rel

        result = link_or_copy(src, dst, copy_mode)
        file_count += 1
        if result == "hardlink":
            hardlink_count += 1
        elif result == "copy":
            copy_count += 1
        elif result == "copy-fallback":
            copy_fallback_count += 1

        if src.suffix.lower() != ".sid":
            continue

        meta = parse_sid_header(src)
        if not meta.get("valid"):
            continue

        sid_type = str(meta.get("sid_type", "UNKNOWN"))
        if sid_filter == "psid" and sid_type != "PSID":
            continue
        if sid_filter == "rsid" and sid_type != "RSID":
            continue

        sid_count += 1
        rel_url_path = to_web_path(dst.resolve().relative_to(workspace_root))

        title = str(meta.get("title") or src.stem)
        author = str(meta.get("author") or "Unknown")
        released = str(meta.get("released") or "")
        songs = int(meta.get("songs") or 1)

        tracks.append(
            {
                "id": rel.as_posix(),
                "title": title,
                "author": author,
                "released": released,
                "platform": "Commodore 64",
                "format": "SID",
                "sidType": sid_type,
                "songs": songs,
                "license": "HVSC archive (verify per-track rights before distribution)",
                "url": rel_url_path,
            }
        )

    tracks.sort(key=lambda t: str(t["id"]).lower())

    ensure_parent(catalog_path)
    catalog = {
        "name": "C64 SID Export Catalog",
        "description": "Catalog generated from local C64Music export. SID playback requires a SID-compatible player.",
        "source": source.as_posix(),
        "export": dest.as_posix(),
        "trackCount": len(tracks),
        "sidFilter": sid_filter,
        "tracks": tracks,
    }
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "files_total": file_count,
        "sid_tracks": sid_count,
        "hardlinks": hardlink_count,
        "copies": copy_count,
        "copy_fallback": copy_fallback_count,
    }


def main() -> None:
    args = parse_args()
    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    catalog_path = Path(args.catalog).resolve()

    stats = build_export_and_catalog(
        source=source,
        dest=dest,
        catalog_path=catalog_path,
        copy_mode=args.copy_mode,
        sid_filter=args.sid_filter,
    )

    print("C64 export done")
    for key, value in stats.items():
        print(f"{key}={value}")
    print(f"catalog={catalog_path}")
    print(f"export={dest}")


if __name__ == "__main__":
    main()
