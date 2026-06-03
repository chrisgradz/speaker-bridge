from __future__ import annotations

import json
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

from .db import Store
from .speaker import preset_to_xml


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def bmx_services(base_url: str) -> bytes:
    body = (DATA_DIR / "bmx_services.json").read_text(encoding="utf-8")
    body = body.replace("{BMX_SERVER}", base_url).replace("{MEDIA_SERVER}", f"{base_url}/media")
    return body.encode("utf-8")


def bmx_services_availability() -> bytes:
    path = DATA_DIR / "bmx_services_availability.json"
    if path.exists():
        return path.read_bytes()
    return b'{"services":[]}'


def account_full(store: Store, account_id: str) -> bytes:
    speakers = store.speakers_for_account(account_id)
    devices = "".join(_device_xml(store, speaker) for speaker in speakers)
    xml = (
        '<?xml version="1.0" standalone="yes"?>'
        f"<account><id>{escape(account_id or 'sixback-local')}</id>"
        "<accountStatus>ACTIVE</accountStatus><mode>NORMAL</mode>"
        "<preferredLanguage>en-US</preferredLanguage>"
        f"<devices>{devices}</devices>{sources_xml()}</account>"
    )
    return xml.encode("utf-8")


def device_presets(store: Store, device_id: str) -> bytes:
    presets = store.presets_for_speaker(device_id)
    if not presets:
        return b""
    body = "".join(preset_to_xml(preset) for preset in presets)
    return f'<?xml version="1.0" standalone="yes"?><presets>{body}</presets>'.encode("utf-8")


def account_presets(store: Store, account_id: str) -> bytes:
    chunks = []
    for speaker in store.speakers_for_account(account_id):
        chunks.extend(preset_to_xml(preset) for preset in store.presets_for_speaker(speaker["device_id"]))
    if not chunks:
        return b""
    return f'<?xml version="1.0" standalone="yes"?><presets>{"".join(chunks)}</presets>'.encode("utf-8")


def _device_xml(store: Store, speaker: dict[str, Any]) -> str:
    device_id = escape(speaker["device_id"])
    name = escape(speaker.get("name", ""))
    model = escape(speaker.get("model", "SoundTouch"))
    firmware = escape(speaker.get("firmware", ""))
    ip = escape(speaker.get("ip", ""))
    presets = "".join(preset_to_xml(preset) for preset in store.presets_for_speaker(speaker["device_id"]))
    presets_xml = f"<presets>{presets}</presets>" if presets else ""
    return (
        f"<device><deviceid>{device_id}</deviceid><name>{name}</name>"
        f"<product>{model}</product><product_code>{model}</product_code>"
        f"<softwareVersion>{firmware}</softwareVersion><ipAddress>{ip}</ipAddress>"
        f"{presets_xml}</device>"
    )


def sources_xml() -> str:
    return (
        "<sources>"
        '<source id="1" type="Audio"><name>TuneIn</name><sourceproviderid>25</sourceproviderid>'
        "<sourcename>TUNEIN</sourcename><credential type=\"token\"></credential><username></username></source>"
        '<source id="3" type="Audio"><name>Local Internet Radio</name><sourceproviderid>11</sourceproviderid>'
        "<sourcename>LOCAL_INTERNET_RADIO</sourcename><credential type=\"token\"></credential><username></username></source>"
        "</sources>"
    )


def tunein_token() -> bytes:
    return b'{"access_token":"sixback-local","token_type":"Bearer","expires_in":31536000}'


def tunein_station(station_id: str, base_url: str) -> bytes:
    resolved = _resolve_tunein(station_id)
    payload = {
        "id": station_id,
        "name": resolved.get("name") or station_id,
        "type": "stationurl",
        "url": resolved.get("url") or f"{base_url}/silence.mp3",
        "containerArt": resolved.get("image") or "",
        "_links": {"bmx_reporting": {"href": f"/v1/report?guide_id={station_id}"}},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _resolve_tunein(station_id: str) -> dict[str, str]:
    url = "http://opml.radiotime.com/Tune.ashx?" + urllib.parse.urlencode(
        {"id": station_id, "render": "json"}
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    item = data.get("body", data)
    if isinstance(item, list) and item:
        item = item[0]
    if not isinstance(item, dict):
        return {}
    return {
        "name": str(item.get("text") or item.get("name") or ""),
        "url": str(item.get("URL") or item.get("url") or ""),
        "image": str(item.get("image") or item.get("logo") or ""),
    }
