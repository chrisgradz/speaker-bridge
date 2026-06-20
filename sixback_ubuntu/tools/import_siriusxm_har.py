#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sixback_ubuntu.db import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a SiriusXM web-player HAR capture into sixback_ubuntu SQLite."
    )
    parser.add_argument("har", help="Path to the .har file captured from the SiriusXM player")
    parser.add_argument("--db", required=True, help="Path to sixback_ubuntu state.sqlite3")
    parser.add_argument(
        "--station-id",
        default="",
        help="SoundTouch/SiriusXM slug such as firstwave, newcountry, or primecountry. Guessed from playlist when omitted.",
    )
    parser.add_argument("--name", default="", help="Friendly channel name. Guessed from HAR when omitted.")
    parser.add_argument(
        "--entity-url",
        default="",
        help="Optional SiriusXM web-player entity URL to store as metadata.",
    )
    args = parser.parse_args()

    har = json.loads(Path(args.har).read_text(encoding="utf-8", errors="replace"))
    entries = har.get("log", {}).get("entries", [])
    playlist_url = find_playlist_url(entries)
    if not playlist_url:
        raise SystemExit("No SiriusXM application/x-mpegURL playlist URL found in HAR")

    station_id = args.station_id or guess_station_id(entries) or guess_station_id_from_url(playlist_url)
    if not station_id:
        raise SystemExit("Could not infer station id; rerun with --station-id firstwave")

    metadata = find_channel_metadata(entries)
    name = args.name or metadata.get("name") or station_id
    entity_url = args.entity_url or metadata.get("entity_url") or ""

    store = Store(args.db)
    store.upsert_siriusxm_channel(
        station_id,
        {
            "name": name,
            "entity_url": entity_url,
            "stream_url": playlist_url,
        },
    )

    print(f"Imported SiriusXM stream mapping for {station_id!r}.")
    print(f"Name: {name}")
    if entity_url:
        print(f"Entity URL: {entity_url}")
    print("Stored stream_url from HAR without printing it, because it may be session-like.")
    print("Restart soundtouch-bridge, then press the preset and watch the service logs.")
    return 0


def find_playlist_url(entries: list[dict]) -> str:
    candidates: list[str] = []
    for entry in entries:
        req = entry.get("request", {})
        res = entry.get("response", {})
        url = req.get("url", "")
        content_type = response_header(res, "content-type").lower()
        if "application/x-mpegurl" in content_type and "streaming.siriusxm.com" in url:
            candidates.append(url)
    return candidates[-1] if candidates else ""


def guess_station_id(entries: list[dict]) -> str:
    for entry in entries:
        res = entry.get("response", {})
        content = res.get("content", {})
        if "application/x-mpegurl" not in response_header(res, "content-type").lower():
            continue
        text = content.get("text", "")
        for line in text.splitlines():
            if "_256k_" in line and line.endswith(".aac"):
                return line.split("_256k_", 1)[0]
    return ""


def guess_station_id_from_url(url: str) -> str:
    path = urlparse(url).path
    last = path.rsplit("/", 1)[-1]
    if "_256k_" in last:
        return last.split("_256k_", 1)[0]
    return ""


def find_channel_metadata(entries: list[dict]) -> dict[str, str]:
    for entry in entries:
        req = entry.get("request", {})
        if "/playback/play/v1/liveUpdate" not in req.get("url", ""):
            continue
        text = entry.get("response", {}).get("content", {}).get("text", "")
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        channel_id = str(data.get("id", ""))
        name = str(data.get("channelName", ""))
        entity_url = ""
        if channel_id:
            entity_url = f"https://www.siriusxm.com/player/channel-linear/entity/{channel_id}"
        return {"name": name, "entity_url": entity_url}
    return {}


def response_header(response: dict, name: str) -> str:
    for header in response.get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
