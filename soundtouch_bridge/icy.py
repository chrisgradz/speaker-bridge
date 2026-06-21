from __future__ import annotations

import re
import urllib.request
from typing import Any, Callable

MAX_ICY_METAINT = 1024 * 1024
MAX_ICY_METADATA_BLOCK = 16 * 1024


def parse_icy_metadata_block(block: bytes) -> dict[str, str]:
    text = block.rstrip(b"\0").decode("utf-8", "replace").strip()
    metadata: dict[str, str] = {"raw": text}
    for key, value in re.findall(r"([A-Za-z0-9_-]+)='([^']*)';", text):
        metadata[key] = value
        metadata[key.lower()] = value
    stream_title = metadata.get("StreamTitle") or metadata.get("streamtitle") or ""
    if stream_title:
        metadata["stream_title"] = stream_title
        artist, separator, title = stream_title.partition(" - ")
        if separator:
            metadata["artist"] = artist.strip()
            metadata["title"] = title.strip()
        else:
            metadata["title"] = stream_title.strip()
    stream_url = metadata.get("StreamUrl") or metadata.get("streamurl") or ""
    if stream_url:
        metadata["stream_url"] = stream_url
    return metadata


def inspect_icy_stream(
    stream_url: str,
    opener: Callable[..., Any] | None = None,
    timeout: float = 8.0,
    max_packets: int = 5,
    max_metaint: int = MAX_ICY_METAINT,
    max_metadata_block: int = MAX_ICY_METADATA_BLOCK,
) -> dict[str, Any]:
    request = urllib.request.Request(
        stream_url,
        headers={
            "Icy-MetaData": "1",
            "User-Agent": "soundtouch-bridge/0.1",
        },
    )
    open_url = opener or urllib.request.urlopen
    response = open_url(request, timeout=timeout)
    try:
        headers = response.headers
        metaint = int(headers.get("icy-metaint", "0") or 0)
        result: dict[str, Any] = {
            "stream_url": stream_url,
            "status": int(getattr(response, "status", 0) or 0),
            "content_type": headers.get("content-type", ""),
            "icy_metadata_supported": metaint > 0,
            "icy_metaint": metaint,
            "metadata_packets_checked": 0,
            "metadata": {},
        }
        if metaint <= 0:
            return result
        if metaint > max_metaint:
            result["icy_metadata_supported"] = False
            result["error"] = "unsupported_metadata_interval"
            result["max_icy_metaint"] = max_metaint
            return result
        for _index in range(max_packets):
            audio = response.read(metaint)
            if len(audio) < metaint:
                return result
            length_byte = response.read(1)
            if not length_byte:
                return result
            result["metadata_packets_checked"] += 1
            metadata_length = length_byte[0] * 16
            result["metadata_length"] = metadata_length
            if metadata_length <= 0:
                continue
            if metadata_length > max_metadata_block:
                result["error"] = "unsupported_metadata_block"
                result["max_metadata_length"] = max_metadata_block
                return result
            metadata = parse_icy_metadata_block(response.read(metadata_length))
            if metadata.get("raw"):
                result["metadata"] = metadata
                return result
        return result
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
