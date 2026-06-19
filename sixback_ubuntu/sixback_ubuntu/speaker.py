from __future__ import annotations

import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html import escape
from typing import Any


BOSE_BMX_PORT = 8090
BOSE_TELNET_PORT = 17000
BOSE_TS = "2012-09-19T12:43:00.000+00:00"


def _http_get(url: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sixback-ubuntu/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post_xml(url: str, body: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"User-Agent": "sixback-ubuntu/0.1", "Content-Type": "application/xml"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def probe_speaker(ip: str) -> dict[str, Any]:
    body = _http_get(f"http://{ip}:{BOSE_BMX_PORT}/info", timeout=6).decode("utf-8", "replace")
    root = ET.fromstring(body)
    device_id = root.attrib.get("deviceID", "").strip()
    if not device_id:
        raise ValueError("speaker /info did not include deviceID")
    return {
        "device_id": device_id,
        "ip": ip,
        "name": _text(root, "name"),
        "model": _text(root, "type"),
        "firmware": _text(root, "softwareVersion"),
        "account_id": _text(root, "margeAccountUUID"),
        "cloud_url": "",
        "migrated": 0,
    }


def parse_presets_xml(xml: str) -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    for match in re.finditer(r"<preset\b(?P<attrs>[^>]*)>(?P<body>.*?)</preset>", xml, re.S | re.I):
        attrs = match.group("attrs")
        body = match.group("body")
        slot = _attr(attrs, "id") or _attr(attrs, "buttonNumber") or "0"
        source = _attr(attrs, "source") or _attr(body, "source") or _tag(body, "source") or ""
        location = _attr(attrs, "location") or _attr(body, "location") or _tag(body, "location")
        name = _tag(body, "itemName") or _tag(body, "name")
        image = _tag(body, "containerArt")
        raw_content_item = _content_item(body)
        presets.append(_normalize_preset(slot, source, location, name, image, raw_content_item))
    return presets


def import_presets(ip: str) -> list[dict[str, Any]]:
    body = _http_get(f"http://{ip}:{BOSE_BMX_PORT}/presets", timeout=8).decode("utf-8", "replace")
    return parse_presets_xml(body)


def store_preset(ip: str, preset: dict[str, Any]) -> None:
    raw_content_item = str(preset.get("raw_content_item", "")).strip()
    if not raw_content_item:
        raw_content_item = _content_item(preset_to_xml(preset))
    if not raw_content_item:
        raise ValueError("preset does not include a ContentItem")
    body = store_preset_xml(int(preset["slot"]), raw_content_item)
    _http_post_xml(f"http://{ip}:{BOSE_BMX_PORT}/storePreset", body, timeout=5)


def store_preset_xml(slot: int, raw_content_item: str) -> str:
    return f'<preset id="{slot}">{raw_content_item}</preset>'


def select_content_item(ip: str, raw_content_item: str) -> None:
    if not raw_content_item.strip():
        raise ValueError("raw_content_item is required")
    _http_post_xml(f"http://{ip}:{BOSE_BMX_PORT}/select", raw_content_item, timeout=4)


def _normalize_preset(
    slot: str,
    source: str,
    location: str,
    name: str,
    image: str,
    raw_content_item: str,
) -> dict[str, Any]:
    source_upper = source.upper()
    station_id = ""
    stream_url = ""
    stored_source = "LOCAL_INTERNET_RADIO"
    if "SIRIUSXM" in source_upper:
        stored_source = "SIRIUSXM"
        station_id = location.rstrip("/").split("/")[-1]
    elif "TUNEIN" in source_upper or "Tune.ashx" in location:
        stored_source = "TUNEIN"
        station_id = location.rstrip("/").split("/")[-1]
        tune_id = re.search(r"[?&]id=([^&]+)", location)
        if tune_id:
            station_id = tune_id.group(1)
    elif raw_content_item:
        stored_source = "OPAQUE"
    else:
        stream_url = location
    return {
        "slot": int(slot or 0),
        "source": stored_source,
        "name": name,
        "station_id": station_id,
        "stream_url": stream_url,
        "image_url": image,
        "raw_content_item": raw_content_item,
    }


def _attr(text: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}=(?:"([^"]*)"|\'([^\']*)\')', text, re.I)
    if not match:
        return ""
    return _unescape(match.group(1) if match.group(1) is not None else match.group(2))


def _tag(text: str, name: str) -> str:
    match = re.search(rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>", text, re.S | re.I)
    return _unescape(re.sub(r"<[^>]+>", "", match.group(1)).strip()) if match else ""


def _content_item(text: str) -> str:
    match = re.search(r"(<ContentItem\b.*?</ContentItem>)", text, re.S | re.I)
    return match.group(1).strip() if match else ""


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def migrate_speaker(ip: str, base_url: str) -> str:
    commands = [
        f"sys configuration bmxRegistryUrl {base_url}/bmx/registry/v1/services",
        f"sys configuration statsServerUrl {base_url}",
        f"sys configuration margeServerUrl {base_url}",
        f"sys configuration swUpdateUrl {base_url}/updates/soundtouch",
        f"envswitch boseurls set {base_url} {base_url}/updates/soundtouch",
        "getpdo CurrentSystemConfiguration",
        "sys reboot",
    ]
    with socket.create_connection((ip, BOSE_TELNET_PORT), timeout=8) as sock:
        sock.settimeout(5)
        time.sleep(0.3)
        _drain(sock)
        transcript = []
        for command in commands:
            sock.sendall((command + "\n").encode("utf-8"))
            if command == "sys reboot":
                time.sleep(0.5)
                transcript.append("sys reboot")
                break
            reply = _read_until_prompt(sock)
            transcript.append(f"$ {command}\n{reply}")
            if _looks_like_error(reply):
                raise RuntimeError(f"speaker rejected command: {command}\n{reply}")
        return "\n".join(transcript)


def _drain(sock: socket.socket) -> None:
    sock.setblocking(False)
    try:
        while sock.recv(4096):
            pass
    except BlockingIOError:
        pass
    finally:
        sock.setblocking(True)


def _read_until_prompt(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"->" in chunk or b"->" in b"".join(chunks)[-64:]:
            break
    return b"".join(chunks).decode("utf-8", "replace")


def _looks_like_error(reply: str) -> bool:
    lowered = reply.lower()
    return any(marker in lowered for marker in ["not found", "usage:", "error", "invalid", "syntax"])


def preset_to_xml(preset: dict[str, Any]) -> str:
    slot = int(preset["slot"])
    source = preset.get("source", "EMPTY")
    if source == "SIRIUSXM" and preset.get("raw_content_item"):
        raw = preset["raw_content_item"]
        name = _tag(raw, "itemName") or preset.get("name", "")
        location = _attr(raw, "location") or preset.get("stream_url", "")
        source_account = _attr(raw, "sourceAccount")
        return (
            f'<preset buttonNumber="{slot}">'
            "<containerArt></containerArt><contentItemType></contentItemType>"
            f"<location>{escape(location)}</location><name>{escape(name)}</name>"
            f"{raw}{_siriusxm_source_xml(source_account)}"
            f"<username>{escape(name)}</username></preset>"
        )
    if source == "OPAQUE" and preset.get("raw_content_item"):
        raw = preset["raw_content_item"]
        name = _tag(raw, "itemName") or preset.get("name", "")
        location = _attr(raw, "location") or preset.get("stream_url", "")
        return (
            f'<preset buttonNumber="{slot}">'
            "<containerArt></containerArt><contentItemType></contentItemType>"
            f"<location>{escape(location)}</location><name>{escape(name)}</name>"
            f"{raw}</preset>"
        )
    if source == "TUNEIN":
        location = f"/v1/playback/station/{escape(preset.get('station_id', ''))}"
        source_name = "TUNEIN"
        provider = "25"
        source_id = "1"
        name = escape(preset.get("name", ""))
        image = escape(preset.get("image_url", ""))
        content_item = (
            f'<ContentItem source="TUNEIN" type="stationurl" location="{location}" isPresetable="true">'
            f"<itemName>{name}</itemName>"
            f"<containerArt>{image}</containerArt>"
            "</ContentItem>"
        )
    else:
        location = escape(preset.get("stream_url", ""))
        source_name = "LOCAL_INTERNET_RADIO"
        provider = "11"
        source_id = "3"
        content_item = ""
    name = escape(preset.get("name", ""))
    image = escape(preset.get("image_url", ""))
    return (
        f'<preset buttonNumber="{slot}">'
        f"<containerArt>{image}</containerArt>"
        "<contentItemType>stationurl</contentItemType>"
        f"<location>{location}</location><name>{name}</name>"
        f"{content_item}"
        f'<source id="{source_id}" type="Audio">'
        "<credential type=\"token\"></credential>"
        f"<name>{escape(source_name)}</name><sourceproviderid>{provider}</sourceproviderid>"
        f"<sourcename>{escape(source_name)}</sourcename><sourceSettings/>"
        "<username></username></source>"
        f"<username>{name}</username></preset>"
    )


def _siriusxm_source_xml(source_account: str) -> str:
    return (
        '<source id="4" type="Audio">'
        f"<createdOn>{BOSE_TS}</createdOn>"
        '<credential type="token"></credential>'
        "<name>SiriusXM</name>"
        "<sourceproviderid>38</sourceproviderid>"
        "<sourcename>SIRIUSXM_EVEREST</sourcename>"
        "<sourceSettings/>"
        f"<updatedOn>{BOSE_TS}</updatedOn>"
        f"<username>{escape(source_account)}</username>"
        "</source>"
    )
