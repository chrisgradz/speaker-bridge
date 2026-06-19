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


def sourceproviders_xml() -> bytes:
    timestamp = "2012-09-19T12:43:00.000+00:00"
    providers = [
        ("1", "PANDORA"),
        ("2", "INTERNET_RADIO"),
        ("3", "OFF"),
        ("4", "LOCAL"),
        ("5", "AIRPLAY"),
        ("6", "CURRATED_RADIO"),
        ("7", "STORED_MUSIC"),
        ("8", "SLAVE_SOURCE"),
        ("9", "AUX"),
        ("10", "RECOMMENDED_INTERNET_RADIO"),
        ("11", "LOCAL_INTERNET_RADIO"),
        ("12", "GLOBAL_INTERNET_RADIO"),
        ("13", "HELLO"),
        ("14", "DEEZER"),
        ("15", "SPOTIFY"),
        ("16", "IHEART"),
        ("17", "SIRIUSXM"),
        ("18", "GOOGLE_PLAY_MUSIC"),
        ("19", "QQMUSIC"),
        ("20", "AMAZON"),
        ("21", "LOCAL_MUSIC"),
        ("22", "WBMX"),
        ("23", "SOUNDCLOUD"),
        ("24", "TIDAL"),
        ("25", "TUNEIN"),
        ("38", "SIRIUSXM_EVEREST"),
        ("39", "RADIO_BROWSER"),
    ]
    body = ['<?xml version="1.0" standalone="yes"?><sourceProviders>']
    for provider_id, name in providers:
        body.append(
            f'<sourceprovider id="{provider_id}"><createdOn>{timestamp}</createdOn>'
            f"<name>{escape(name)}</name><updatedOn>{timestamp}</updatedOn></sourceprovider>"
        )
    body.append("</sourceProviders>")
    return "".join(body).encode("utf-8")


def account_full(store: Store, account_id: str) -> bytes:
    speakers = store.speakers_for_account(account_id)
    devices = "".join(_device_xml(store, speaker) for speaker in speakers)
    xml = (
        '<?xml version="1.0" standalone="yes"?>'
        f"<account><id>{escape(account_id or 'sixback-local')}</id>"
        "<accountStatus>ACTIVE</accountStatus><mode>NORMAL</mode>"
        "<preferredLanguage>en-US</preferredLanguage>"
        f"<devices>{devices}</devices>{sources_xml(store, account_id)}</account>"
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


def sources_xml(store: Store | None = None, account_id: str = "") -> str:
    parts = [
        "<sources>"
        '<source id="1" type="Audio"><name>TuneIn</name><sourceproviderid>25</sourceproviderid>'
        "<sourcename>TUNEIN</sourcename><credential type=\"token\"></credential><username></username></source>"
        '<source id="3" type="Audio"><name>Local Internet Radio</name><sourceproviderid>11</sourceproviderid>'
        "<sourcename>LOCAL_INTERNET_RADIO</sourcename><credential type=\"token\"></credential><username></username></source>"
    ]
    accounts = store.siriusxm_source_accounts(account_id) if store else []
    if accounts:
        for idx, account in enumerate(accounts, start=4):
            username = escape(account["source_account"])
            name = escape(account.get("name") or "SiriusXM")
            parts.append(
                f'<source id="{idx}" type="Audio"><name>{name}</name><sourceproviderid>38</sourceproviderid>'
                f'<sourcename>SIRIUSXM_EVEREST</sourcename><credential type="token"></credential>'
                f"<username>{username}</username></source>"
            )
    else:
        parts.append(
            '<source id="4" type="Audio"><name>SiriusXM</name><sourceproviderid>38</sourceproviderid>'
            '<sourcename>SIRIUSXM_EVEREST</sourcename><credential type="token"></credential>'
            "<username></username></source>"
        )
    parts.append("</sources>")
    return "".join(parts)


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


def siriusxm_token() -> bytes:
    return b'{"access_token":"sixback-siriusxm-preserved","token_type":"Bearer","expires_in":31536000}'


def siriusxm_availability() -> bytes:
    return b'{"available":true,"status":"AVAILABLE"}'


def siriusxm_station(store: Store, station_id: str, base_url: str) -> bytes:
    preset = store.find_preset_by_source_station("SIRIUSXM", station_id)
    channel = store.get_siriusxm_channel(station_id)
    name = channel.get("name") or (preset.get("name") if preset else station_id)
    image = preset.get("image_url") if preset else ""
    stream_url = channel.get("stream_url") or f"{base_url}/siriusxm/needs-auth/{urllib.parse.quote(station_id)}"
    needs_auth = not bool(channel.get("stream_url"))
    payload = {
        "name": name or station_id,
        "streamType": "liveRadio",
        "audio": {
            "hasPlaylist": True,
            "isRealtime": True,
            "maxTimeout": 60,
            "streamUrl": stream_url,
            "streams": [
                {
                    "bufferingTimeout": 20,
                    "connectingTimeout": 10,
                    "hasPlaylist": True,
                    "isRealtime": True,
                    "streamUrl": stream_url,
                }
            ],
        },
        "imageUrl": image or "",
        "_links": {
            "bmx_reporting": {"href": f"/v1/report?guide_id={station_id}"},
            "bmx_nowplaying": {
                "href": f"/v1/now-playing/station/{station_id}",
                "useInternalClient": "ALWAYS",
            },
        },
        "_meta": {
            "resolver": "sixback-ubuntu-siriusxm-preserved",
            "entityUrl": channel.get("entity_url") or "",
            "requiresAuthStreamResolver": needs_auth,
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def siriusxm_now_playing(store: Store, station_id: str) -> bytes:
    preset = store.find_preset_by_source_station("SIRIUSXM", station_id)
    name = preset.get("name") if preset else station_id
    image = preset.get("image_url") if preset else ""
    payload = {
        "stationId": station_id,
        "stationName": name or station_id,
        "channelName": name or station_id,
        "trackName": name or station_id,
        "artistName": "SiriusXM",
        "albumName": "",
        "imageUrl": image or "",
        "containerArt": image or "",
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
