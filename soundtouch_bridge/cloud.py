from __future__ import annotations

import json
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

from .db import Store
from .speaker import preset_to_xml


DATA_DIR = Path(__file__).resolve().parent / "data"
BOSE_TS = "2012-09-19T12:43:00.000+00:00"


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
            f'<sourceprovider id="{provider_id}"><createdOn>{BOSE_TS}</createdOn>'
            f"<name>{escape(name)}</name><updatedOn>{BOSE_TS}</updatedOn></sourceprovider>"
        )
    body.append("</sourceProviders>")
    return "".join(body).encode("utf-8")


def account_full(store: Store, account_id: str) -> bytes:
    speakers = store.speakers_for_account(account_id)
    devices = "".join(_device_xml(store, speaker) for speaker in speakers)
    xml = (
        '<?xml version="1.0" standalone="yes"?>'
        f"<account><id>{escape(account_id or 'soundtouch-bridge-local')}</id>"
        "<accountStatus>ACTIVE</accountStatus><mode>NORMAL</mode>"
        "<preferredLanguage>en-US</preferredLanguage>"
        "<providerSettings/>"
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
    parts = ["<sources>"]
    parts.append(_source_xml("1", "TuneIn", "25", "TUNEIN", ""))
    parts.append(_source_xml("3", "Local Internet Radio", "11", "LOCAL_INTERNET_RADIO", ""))
    accounts = store.siriusxm_source_accounts(account_id) if store else []
    if accounts:
        for idx, account in enumerate(accounts, start=4):
            username = escape(account["source_account"])
            parts.append(_source_xml(str(idx), "SiriusXM", "38", "SIRIUSXM_EVEREST", username))
    else:
        parts.append(_source_xml("4", "SiriusXM", "38", "SIRIUSXM_EVEREST", ""))
    parts.append("</sources>")
    return "".join(parts)


def _source_xml(source_id: str, name: str, provider_id: str, source_name: str, username: str) -> str:
    return (
        f'<source id="{escape(source_id)}" type="Audio">'
        f"<createdOn>{BOSE_TS}</createdOn>"
        '<credential type="token"></credential>'
        f"<name>{escape(name)}</name>"
        f"<sourceproviderid>{escape(provider_id)}</sourceproviderid>"
        f"<sourcename>{escape(source_name)}</sourcename>"
        "<sourceSettings/>"
        f"<updatedOn>{BOSE_TS}</updatedOn>"
        f"<username>{escape(username)}</username>"
        "</source>"
    )


def tunein_token() -> bytes:
    return b'{"access_token":"soundtouch-bridge-local","token_type":"Bearer","expires_in":31536000}'


def tunein_station(store: Store, station_id: str, base_url: str) -> bytes:
    resolved = _resolve_tunein(station_id)
    preset = store.find_preset_by_source_station("TUNEIN", station_id)
    name = resolved.get("name") or (preset.get("name") if preset else "") or station_id
    image = resolved.get("image") or (preset.get("image_url") if preset else "") or ""
    stream_url = resolved.get("url") or f"{base_url}/silence.mp3"
    has_playlist = resolved.get("media_type", "").lower() in {"hls", "m3u", "m3u8"} or ".m3u8" in stream_url.lower()
    payload = {
        "id": station_id,
        "name": name,
        "type": "stationurl",
        "url": stream_url,
        "streamType": "liveRadio",
        "audio": {
            "hasPlaylist": has_playlist,
            "isRealtime": True,
            "maxTimeout": 180,
            "streamUrl": stream_url,
            "streams": [
                {
                    "bufferingTimeout": 120,
                    "connectingTimeout": 20,
                    "hasPlaylist": has_playlist,
                    "isRealtime": True,
                    "streamUrl": stream_url,
                }
            ],
        },
        "containerArt": image,
        "imageUrl": image,
        "nowPlaying": {
            "source": "TUNEIN",
            "playStatus": "PLAY_STATE",
            "stationName": {"text": name},
            "track": {"text": name},
            "artist": {"text": "TuneIn"},
            "album": {"text": ""},
            "art": {
                "artImageStatus": "IMAGE_PRESENT" if image else "SHOW_DEFAULT_IMAGE",
                "text": image,
            },
            "streamTypeField": {"text": "RADIO_STREAMING"},
            "contentItem": {
                "source": "TUNEIN",
                "type": "stationurl",
                "isPresetable": "true",
                "location": f"/v1/playback/station/{station_id}",
                "itemName": {"text": name},
                "containerArt": image,
            },
        },
        "_links": {"bmx_reporting": {"href": f"/v1/report?guide_id={station_id}"}},
        "_meta": {"resolver": "soundtouch-bridge-tunein", "mediaType": resolved.get("media_type", "")},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def tunein_siriusxm_alias_station(store: Store, old_station_id: str, station_id: str, base_url: str) -> bytes:
    preset = store.find_preset_by_source_station("SIRIUSXM", station_id)
    channel = store.get_siriusxm_channel(station_id)
    name = channel.get("name") or (preset.get("name") if preset else station_id)
    image = preset.get("image_url") if preset else ""
    metadata = {
        "stationId": station_id,
        "stationName": name or station_id,
        "channelName": name or station_id,
        "trackName": name or station_id,
        "artistName": "SiriusXM",
        "imageUrl": image or "",
        "containerArt": image or "",
    }
    payload = json.loads(siriusxm_station(store, station_id, base_url, metadata).decode("utf-8"))
    payload.setdefault("_meta", {})
    payload["_meta"].update(
        {
            "resolver": "soundtouch-bridge-cross-source-alias",
            "source": "TUNEIN",
            "targetSource": "SIRIUSXM",
            "targetStationId": station_id,
        }
    )
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def siriusxm_token() -> bytes:
    return b'{"access_token":"soundtouch-bridge-siriusxm-preserved","token_type":"Bearer","expires_in":31536000}'


def siriusxm_availability() -> bytes:
    return b'{"available":true,"status":"AVAILABLE"}'


def siriusxm_station(
    store: Store,
    station_id: str,
    base_url: str,
    metadata: dict[str, str] | None = None,
    display_name: str = "",
) -> bytes:
    preset = store.find_preset_by_source_station("SIRIUSXM", station_id)
    channel = store.get_siriusxm_channel(station_id)
    name = channel.get("name") or (preset.get("name") if preset else "") or display_name or station_id
    image = preset.get("image_url") if preset else ""
    stream_url = f"{base_url}/siriusxm/proxy/{urllib.parse.quote(station_id)}/playlist.m3u8"
    needs_auth = not bool(channel.get("stream_url"))
    payload = {
        "name": name or station_id,
        "streamType": "liveRadio",
        "audio": {
            "hasPlaylist": True,
            "isRealtime": True,
            "maxTimeout": 180,
            "streamUrl": stream_url,
            "streams": [
                {
                    "bufferingTimeout": 120,
                    "connectingTimeout": 20,
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
            "resolver": "soundtouch-bridge-siriusxm-preserved",
            "entityUrl": channel.get("entity_url") or "",
            "requiresAuthStreamResolver": needs_auth,
        },
    }
    if metadata:
        now_playing = json.loads(siriusxm_now_playing(store, station_id, metadata).decode("utf-8"))
        payload["nowPlaying"] = {
            "source": "SIRIUSXM_EVEREST",
            "playStatus": "PLAY_STATE",
            "stationName": {"text": str(now_playing.get("stationName") or name or station_id)},
            "track": now_playing.get("track") or {"text": ""},
            "artist": now_playing.get("artist") or {"text": ""},
            "album": now_playing.get("album") or {"text": ""},
            "art": now_playing.get("art") or {"artImageStatus": "SHOW_DEFAULT_IMAGE", "text": ""},
            "streamTypeField": {"text": "RADIO_STREAMING"},
            "contentItem": {
                "source": "SIRIUSXM_EVEREST",
                "type": "stationurl",
                "isPresetable": "true",
                "location": f"/playback/station/{station_id}",
                "itemName": {"text": name or station_id},
                "containerArt": str(now_playing.get("containerArt") or now_playing.get("imageUrl") or image or ""),
            },
        }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def siriusxm_station_display_experiment(
    store: Store,
    station_id: str,
    base_url: str,
    metadata: dict[str, str] | None = None,
) -> bytes:
    payload = json.loads(siriusxm_station(store, station_id, base_url, metadata).decode("utf-8"))
    metadata_stream_url = f"{base_url}/siriusxm/proxy/{urllib.parse.quote(station_id)}/metadata-playlist.m3u8"
    payload["audio"]["streamUrl"] = metadata_stream_url
    for stream in payload["audio"].get("streams", []):
        if isinstance(stream, dict):
            stream["streamUrl"] = metadata_stream_url
    now_playing = json.loads(siriusxm_now_playing(store, station_id, metadata).decode("utf-8"))
    station_name = str(now_playing.get("stationName") or payload.get("name") or station_id)
    track = now_playing.get("track") or {"text": str(now_playing.get("trackName") or "")}
    artist = now_playing.get("artist") or {"text": str(now_playing.get("artistName") or "")}
    album = now_playing.get("album") or {"text": str(now_playing.get("albumName") or "")}
    art = now_playing.get("art") or {"artImageStatus": "SHOW_DEFAULT_IMAGE", "text": ""}
    content_item = {
        "source": "SIRIUSXM_EVEREST",
        "type": "stationurl",
        "isPresetable": "true",
        "location": f"/playback/station/{station_id}",
        "itemName": {"text": payload.get("name") or station_name},
        "containerArt": str(now_playing.get("containerArt") or now_playing.get("imageUrl") or ""),
    }
    payload.update(
        {
            "source": "SIRIUSXM_EVEREST",
            "sourceAccount": "",
            "playStatus": "PLAY_STATE",
            "stationName": {"text": station_name},
            "track": track,
            "artist": artist,
            "album": album,
            "art": art,
            "streamTypeField": {"text": "RADIO_STREAMING"},
            "contentItem": content_item,
            "nowPlaying": {
                "source": "SIRIUSXM_EVEREST",
                "playStatus": "PLAY_STATE",
                "stationName": {"text": station_name},
                "track": track,
                "artist": artist,
                "album": album,
                "art": art,
                "streamTypeField": {"text": "RADIO_STREAMING"},
                "contentItem": content_item,
            },
        }
    )
    payload.setdefault("_meta", {})
    payload["_meta"].update(
        {
            "resolver": "soundtouch-bridge-siriusxm-display-experiment",
            "experiment": "iheart-like-now-playing-fields",
            "hlsTimedMetadata": "id3-prepend",
        }
    )
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def siriusxm_now_playing(store: Store, station_id: str, metadata: dict[str, str] | None = None) -> bytes:
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
    if metadata:
        payload.update({key: value for key, value in metadata.items() if value})
        if payload.get("imageUrl") and not payload.get("containerArt"):
            payload["containerArt"] = payload["imageUrl"]
    image = str(payload.get("imageUrl") or payload.get("containerArt") or "")
    payload.update(
        {
            "track": {"text": str(payload.get("trackName") or "")},
            "artist": {"text": str(payload.get("artistName") or "")},
            "album": {"text": str(payload.get("albumName") or "")},
            "art": {
                "artImageStatus": "IMAGE_PRESENT" if image else "SHOW_DEFAULT_IMAGE",
                "text": image,
            },
        }
    )
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
        "media_type": str(item.get("media_type") or item.get("mediaType") or ""),
    }
