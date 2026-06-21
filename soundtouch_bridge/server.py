from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

from . import __version__
from . import cloud as cloud_api
from .cloud import (
    account_full,
    account_presets,
    bmx_services,
    bmx_services_availability,
    device_presets,
    siriusxm_availability,
    siriusxm_now_playing,
    siriusxm_station_display_experiment,
    siriusxm_station,
    siriusxm_token,
    sourceproviders_xml,
    sources_xml,
    tunein_station,
    tunein_token,
)
from .db import Store
from .icy import inspect_icy_stream
from .speaker import (
    import_presets,
    migrate_speaker,
    now_playing_xml,
    press_speaker_key,
    probe_speaker,
    select_content_item,
    store_preset,
)
from .siriusxm import (
    DEFAULT_ENV_FILE,
    SiriusXmCredentials,
    SiriusXmError,
    SiriusXmNotConfigured,
    SiriusXmSession,
    should_refresh_stream,
)


Json = dict[str, Any]
SIRIUSXM_HLS_AES_KEY = base64.b64decode("0Nsco7MAgxowGvkUT8aYag==")
MAX_JSON_REQUEST_BYTES = 1024 * 1024
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BODY_CHARS = 64 * 1024
MAX_SIRIUSXM_FETCH_BYTES = 8 * 1024 * 1024


class PayloadTooLarge(ValueError):
    pass


class SoundTouchBridgeHandler(BaseHTTPRequestHandler):
    server: "SoundTouchBridgeServer"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        for route_method, pattern, handler in self.server.routes:
            match = pattern.fullmatch(path)
            if route_method == method and match:
                try:
                    handler(self, **match.groupdict())
                except PayloadTooLarge as exc:
                    self.close_connection = True
                    self.send_json({"error": "payload_too_large", "message": str(exc)}, 413)
                except Exception as exc:
                    self.send_json({"error": type(exc).__name__, "message": str(exc)}, 500)
                return
        self.send_json({"error": "not_found", "path": path}, 404)

    def read_json(self) -> Json:
        length = content_length(self)
        if length == 0:
            return {}
        if length > MAX_JSON_REQUEST_BYTES:
            raise PayloadTooLarge(f"request body exceeds {MAX_JSON_REQUEST_BYTES} bytes")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def read_text(self, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> str:
        return read_request_text(self, max_bytes=max_bytes)

    def send_bytes(self, body: bytes, status: int = 200, content_type: str = "application/octet-stream") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            print(f"[client-disconnect] {self.client_address[0]} {self.command} {self.path}")

    def send_text(self, body: str, status: int = 200, content_type: str = "text/plain") -> None:
        self.send_bytes(body.encode("utf-8"), status, content_type)

    def send_json(self, body: Json, status: int = 200) -> None:
        self.send_bytes(json.dumps(body, indent=2).encode("utf-8"), status, "application/json")


RouteHandler = Callable[[SoundTouchBridgeHandler], None]


def content_length(req: SoundTouchBridgeHandler) -> int:
    raw = req.headers.get("Content-Length", "0") or "0"
    try:
        length = int(raw)
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    return max(length, 0)


def read_request_text(req: SoundTouchBridgeHandler, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> str:
    length = content_length(req)
    if length == 0:
        return ""
    if length > max_bytes:
        req.close_connection = True
        raise PayloadTooLarge(f"request body exceeds {max_bytes} bytes")
    return req.rfile.read(length).decode("utf-8", "replace")


def drain_request_body(req: SoundTouchBridgeHandler, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
    length = content_length(req)
    if length == 0:
        return
    if length > max_bytes:
        req.rfile.read(max_bytes)
        req.close_connection = True
        return
    req.rfile.read(length)


def truncate_diagnostic_body(body: str, max_chars: int = MAX_DIAGNOSTIC_BODY_CHARS) -> str:
    if len(body) <= max_chars:
        return body
    marker = f"\n[truncated {len(body) - max_chars} chars]"
    keep = max(max_chars - len(marker), 0)
    return body[:keep] + marker


class SoundTouchBridgeServer(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], store: Store, public_base: str):
        super().__init__(addr, SoundTouchBridgeHandler)
        self.store = store
        self.public_base = public_base.rstrip("/")
        siriusxm_env_file = os.environ.get("SOUNDTOUCH_BRIDGE_SIRIUSXM_ENV_FILE") or DEFAULT_ENV_FILE
        self.siriusxm = SiriusXmSession.from_env(siriusxm_env_file)
        self.siriusxm_proxy_urls: dict[str, str] = {}
        self.siriusxm_fetch_cache: dict[str, tuple[float, bytes]] = {}
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., None]]] = []
        self._register_routes()

    def route(self, method: str, pattern: str, handler: Callable[..., None]) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def _register_routes(self) -> None:
        self.route("GET", r"/", handle_root)
        self.route("GET", r"/admin", handle_admin)
        self.route("GET", r"/play", handle_play)
        self.route("GET", r"/healthz", handle_healthz)
        self.route("GET", r"/api/speakers", handle_list_speakers)
        self.route("POST", r"/api/speakers", handle_add_speaker)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/import-presets", handle_import_presets)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/migrate", handle_migrate)
        self.route("GET", r"/api/speakers/(?P<device_id>[^/]+)/events", handle_speaker_events)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/play", handle_play_station)
        self.route(
            "POST",
            r"/api/experiments/play/speakers/(?P<device_id>[^/]+)/select",
            handle_play_station,
        )
        self.route("GET", r"/api/accounts/(?P<account_id>[^/]+)/cloud-responses", handle_cloud_responses)
        self.route("GET", r"/api/speakers/(?P<device_id>[^/]+)/presets", handle_get_presets)
        self.route("PUT", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])", handle_put_preset)
        self.route("DELETE", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])", handle_delete_preset)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])/copy", handle_copy_preset)
        self.route(
            "POST",
            r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])/siriusxm-display-experiment",
            handle_siriusxm_display_experiment_preset,
        )
        self.route("GET", r"/api/siriusxm/channels", handle_siriusxm_channels_list)
        self.route("GET", r"/api/siriusxm/catalog", handle_siriusxm_catalog)
        self.route("GET", r"/api/siriusxm/session", handle_siriusxm_session)
        self.route("POST", r"/api/siriusxm/session/login", handle_siriusxm_session_login)
        self.route("GET", r"/api/siriusxm/channels/(?P<station_id>[^/]+)", handle_siriusxm_channel_get)
        self.route("PUT", r"/api/siriusxm/channels/(?P<station_id>[^/]+)", handle_siriusxm_channel_put)
        self.route("POST", r"/api/siriusxm/channels/(?P<station_id>[^/]+)/refresh", handle_siriusxm_channel_refresh)
        self.route(
            "GET",
            r"/api/siriusxm/channels/(?P<station_id>[^/]+)/metadata-proxy-debug",
            handle_siriusxm_metadata_proxy_debug,
        )
        self.route("GET", r"/api/tunein/search", handle_tunein_search)
        self.route("GET", r"/api/tunein/stations/(?P<station_id>[^/]+)/icy-debug", handle_tunein_icy_debug)
        self.route("GET", r"/api/iheart/search", handle_iheart_search)
        self.route("GET", r"/api/iheart/stations/(?P<station_id>[^/]+)/stream", handle_iheart_station_stream)
        self.route("GET", r"/iheart/stations/(?P<station_id>[^/]+)/station\.json", handle_iheart_station_descriptor)
        self.route("GET", r"/siriusxm/stations/(?P<station_id>[^/]+)/station\.json", handle_siriusxm_station_descriptor)
        self.route("GET", r"/iheart/proxy/(?P<station_id>[^/]+)/playlist\.m3u", handle_iheart_proxy_playlist)
        self.route("GET", r"/iheart/proxy/(?P<station_id>[^/]+)/stream\.aac", handle_iheart_proxy_stream)
        self.route("GET", r"/iheart/proxy/(?P<station_id>[^/]+)/stream", handle_iheart_proxy_stream)
        self.route(
            "GET",
            r"/api/siriusxm/channels/(?P<station_id>[^/]+)/now-playing-debug",
            handle_siriusxm_now_playing_debug,
        )
        self.route("GET", r"/bmx/registry/v1/services", handle_bmx_services)
        self.route("GET", r"/bmx/registry/v1/servicesAvailability", handle_bmx_services_availability)
        self.route("GET", r"/streaming/sourceproviders", handle_sourceproviders)
        self.route("POST", r"/bmx/tunein/v1/token", handle_tunein_token)
        self.route("GET", r"/bmx/tunein/v1/playback/station/(?P<station_id>[^/]+)", handle_tunein_station)
        self.route("GET", r"/v1/playback/station/(?P<station_id>[^/]+)", handle_tunein_station)
        self.route("POST", r"/bmx/tunein/v1/report", handle_report)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/token", handle_siriusxm_token)
        self.route("POST", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/token", handle_siriusxm_token)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/availability", handle_siriusxm_availability)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/playback/station/(?P<station_id>[^/]+)", handle_siriusxm_station)
        self.route("GET", r"/experiments/siriusxm/display/playback/station/(?P<station_id>[^/]+)", handle_siriusxm_display_experiment)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/experiments/siriusxm/display/playback/station/(?P<station_id>[^/]+)", handle_siriusxm_display_experiment)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/v1/now-playing/station/(?P<station_id>[^/]+)", handle_siriusxm_now_playing)
        self.route("POST", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/v1/report", handle_report)
        self.route("GET", r"/siriusxm/proxy/(?P<station_id>[^/]+)/playlist\.m3u8", handle_siriusxm_proxy_playlist)
        self.route("GET", r"/siriusxm/proxy/(?P<station_id>[^/]+)/metadata-playlist\.m3u8", handle_siriusxm_metadata_proxy_playlist)
        self.route("GET", r"/siriusxm/proxy/meta/(?P<station_id>[^/]+)/(?P<token>[^/]+)", handle_siriusxm_metadata_proxy_fetch)
        self.route("GET", r"/siriusxm/proxy/fetch/(?P<token>[^/]+)", handle_siriusxm_proxy_fetch)
        self.route("GET", r"/siriusxm/proxy/fetch", handle_siriusxm_proxy_fetch)
        self.route("GET", r"/siriusxm/needs-auth/(?P<station_id>[^/]+)", handle_siriusxm_needs_auth)
        self.route("POST", r"/v1/scmudc/(?P<device_id>[^/]+)", handle_scmudc)
        self.route("GET", r"/streaming/account/(?P<account_id>[^/]+)/full", handle_account_full)
        self.route("GET", r"/streaming/account/(?P<account_id>[^/]+)/sources", handle_sources)
        self.route("GET", r"/streaming/account/(?P<account_id>[^/]+)/presets", handle_account_presets)
        self.route("GET", r"/streaming/account/(?P<account_id>[^/]+)/presets/all", handle_account_presets)
        self.route("POST", r"/streaming/account/(?P<account_id>[^/]+)/device/", handle_device_add)
        self.route("POST", r"/streaming/account/(?P<account_id>[^/]+)/source", handle_source_add)
        self.route(
            "GET",
            r"/streaming/account/(?P<account_id>[^/]+)/device/(?P<device_id>[^/]+)/presets",
            handle_device_presets,
        )
        self.route("GET", r"/updates/soundtouch", handle_updates)


def handle_root(req: SoundTouchBridgeHandler) -> None:
    req.send_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>SoundTouch Bridge</title></head>'
        '<body><h1>SoundTouch Bridge</h1><p><a href="/admin">Admin</a> <a href="/play">Play</a></p></body></html>',
        content_type="text/html; charset=utf-8",
    )


def handle_admin(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(ADMIN_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")


def handle_play(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(PLAY_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")


def handle_healthz(req: SoundTouchBridgeHandler) -> None:
    req.send_json({"ok": True})


def handle_list_speakers(req: SoundTouchBridgeHandler) -> None:
    req.send_json({"speakers": req.server.store.list_speakers()})


def handle_add_speaker(req: SoundTouchBridgeHandler) -> None:
    body = req.read_json()
    ip = str(body.get("ip", "")).strip()
    if not ip:
        req.send_json({"error": "missing ip"}, 400)
        return
    speaker = probe_speaker(ip)
    req.server.store.upsert_speaker(speaker)
    req.send_json({"speaker": speaker}, HTTPStatus.CREATED)


def handle_import_presets(req: SoundTouchBridgeHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    presets = import_presets(speaker["ip"])
    req.server.store.replace_presets(device_id, presets)
    req.send_json({"device_id": device_id, "imported": len(presets), "presets": presets})


def handle_speaker_events(req: SoundTouchBridgeHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    req.send_json({"device_id": device_id, "events": req.server.store.recent_scmudc_events(device_id)})


def handle_play_station(req: SoundTouchBridgeHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    try:
        body = req.read_json()
        raw_content_item = build_play_content_item(
            req.server.store,
            device_id,
            req.server.public_base,
            body,
        )
        result = push_station_to_speaker(speaker, raw_content_item, wake=bool(body.get("wake", False)))
    except Exception as exc:
        req.send_json({"error": type(exc).__name__, "message": str(exc)}, 400)
        return
    req.send_json({"device_id": device_id, "play": result, "raw_content_item": raw_content_item})


def handle_cloud_responses(req: SoundTouchBridgeHandler, account_id: str) -> None:
    query = urlparse(req.path).query
    raw = "raw=1" in query or "raw=true" in query.lower()
    responses = req.server.store.recent_cloud_responses(account_id)
    if not raw:
        responses = [
            {
                **response,
                "body": redact_cloud_response(str(response.get("body", ""))),
            }
            for response in responses
        ]
    req.send_json({"account_id": account_id, "redacted": not raw, "responses": responses})


def handle_get_presets(req: SoundTouchBridgeHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    req.send_json({"device_id": device_id, "presets": req.server.store.preset_slots_for_speaker(device_id)})


def handle_put_preset(req: SoundTouchBridgeHandler, device_id: str, slot: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    old_preset = speaker_onboard_preset(speaker, int(slot)) or req.server.store.get_preset(device_id, int(slot))
    try:
        preset = prepare_admin_preset(req.server.store, device_id, body, int(slot))
    except ValueError as exc:
        req.send_json({"error": "invalid_preset", "message": str(exc)}, 400)
        return
    saved = req.server.store.set_preset(device_id, {"device_id": device_id, **preset})
    remember_siriusxm_station_alias(req.server.store, old_preset, saved)
    speaker_store = store_onboard_preset(speaker, saved)
    req.send_json({"device_id": device_id, "preset": saved, "speaker_store": speaker_store})


def handle_copy_preset(req: SoundTouchBridgeHandler, device_id: str, slot: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    try:
        source_slot = int(body.get("source_slot", 0))
        target_slot = int(slot)
        if source_slot < 1 or source_slot > 6:
            raise ValueError("source_slot must be 1 through 6")
        saved = req.server.store.copy_preset(device_id, source_slot, target_slot)
    except ValueError as exc:
        req.send_json({"error": "invalid_copy", "message": str(exc)}, 400)
        return
    req.send_json({"device_id": device_id, "source_slot": source_slot, "preset": saved})


def handle_siriusxm_display_experiment_preset(req: SoundTouchBridgeHandler, device_id: str, slot: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    preset = req.server.store.get_preset(device_id, int(slot))
    try:
        rewritten = rewrite_siriusxm_preset_content_item(
            {**preset, "device_id": device_id, "slot": int(slot)},
            experiment=bool(body.get("enabled", True)),
        )
    except ValueError as exc:
        req.send_json({"error": "invalid_preset", "message": str(exc)}, 400)
        return
    saved = req.server.store.set_preset(device_id, {**rewritten, "device_id": device_id, "slot": int(slot)})
    speaker_store = store_onboard_preset(speaker, saved)
    req.send_json({"device_id": device_id, "preset": saved, "speaker_store": speaker_store})


def handle_delete_preset(req: SoundTouchBridgeHandler, device_id: str, slot: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    req.server.store.clear_preset(device_id, int(slot))
    req.send_json({"device_id": device_id, "slot": int(slot), "cleared": True})


def normalize_admin_preset(body: Json, slot: int) -> Json:
    source = str(body.get("source", "")).strip().upper()
    name = str(body.get("name", "")).strip()
    station_id = str(body.get("station_id", "")).strip()
    stream_url = str(body.get("stream_url", "")).strip()
    image_url = str(body.get("image_url", "")).strip()
    raw_content_item = str(body.get("raw_content_item", "")).strip()
    if source not in {"TUNEIN", "LOCAL_INTERNET_RADIO", "SIRIUSXM", "OPAQUE"}:
        raise ValueError("source must be TUNEIN, LOCAL_INTERNET_RADIO, SIRIUSXM, or OPAQUE")
    if not name:
        raise ValueError("name is required")
    if source == "TUNEIN" and not station_id:
        raise ValueError("station_id is required for TuneIn presets")
    if source == "OPAQUE" and not raw_content_item:
        raise ValueError("raw_content_item is required for preserved presets")
    if source == "LOCAL_INTERNET_RADIO":
        if not stream_url:
            raise ValueError("stream_url is required for direct stream presets")
        if not (stream_url.startswith("http://") or stream_url.startswith("https://")):
            raise ValueError("stream_url must start with http:// or https://")
    return {
        "slot": slot,
        "source": source,
        "name": name,
        "station_id": station_id if source in {"TUNEIN", "SIRIUSXM"} else "",
        "stream_url": stream_url if source == "LOCAL_INTERNET_RADIO" else "",
        "image_url": image_url,
        "raw_content_item": raw_content_item if source in {"SIRIUSXM", "OPAQUE"} else "",
    }


def prepare_admin_preset(store: Store, device_id: str, body: Json, slot: int) -> Json:
    preset = normalize_admin_preset(body, slot)
    if preset["source"] == "SIRIUSXM" and is_preserved_siriusxm(preset):
        preset["raw_content_item"] = normalize_siriusxm_content_item_location(preset["raw_content_item"])
        preset["station_id"] = preset_station_slug(preset)
        return preset
    if preset["source"] != "SIRIUSXM" or is_preserved_siriusxm(preset):
        return preset
    if not preset["station_id"]:
        raise ValueError("station_id is required for SiriusXM presets")
    existing_channel = store.get_siriusxm_channel(preset["station_id"])
    entity_url = str(body.get("entity_url", "")).strip() or str(existing_channel.get("entity_url", ""))
    store.upsert_siriusxm_channel(
        preset["station_id"],
        {
            "name": preset["name"],
            "entity_url": entity_url,
            "stream_url": existing_channel.get("stream_url", ""),
        },
    )
    source_account = first_siriusxm_source_account(store, device_id)
    preset["raw_content_item"] = build_siriusxm_content_item(
        preset["station_id"],
        preset["name"],
        preset.get("image_url", ""),
        source_account,
    )
    return preset


def is_preserved_siriusxm(preset: Json) -> bool:
    raw = str(preset.get("raw_content_item", ""))
    return "SIRIUSXM_EVEREST" in raw and "<ContentItem" in raw


def first_siriusxm_source_account(store: Store, device_id: str) -> str:
    for preset in store.preset_slots_for_speaker(device_id):
        raw = str(preset.get("raw_content_item", ""))
        source_account = xml_attr(raw, "sourceAccount")
        if source_account:
            return source_account
    accounts = store.siriusxm_source_accounts()
    if accounts:
        return str(accounts[0].get("source_account", ""))
    return ""


def first_source_account_for_source(store: Store, device_id: str, source: str) -> str:
    source = source.strip().upper()
    for preset in store.preset_slots_for_speaker(device_id):
        raw = str(preset.get("raw_content_item", ""))
        if xml_attr(raw, "source").strip().upper() != source:
            continue
        source_account = xml_attr(raw, "sourceAccount")
        if source_account:
            return source_account
    if source == "IHEART":
        return cloud_api.configured_iheart_source_account()
    return ""


def build_siriusxm_content_item(
    station_id: str,
    name: str,
    image_url: str = "",
    source_account: str = "",
) -> str:
    location = f"/playback/station/{station_id}?preset_play=True"
    item = (
        f'<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" location="{escape(location)}" '
        f'sourceAccount="{escape(source_account)}" isPresetable="true">'
        f"<itemName>{escape(name)}</itemName>"
    )
    if image_url:
        item += f"<containerArt>{escape(image_url)}</containerArt>"
    return item + "</ContentItem>"


def build_iheart_content_item(
    station_id: str,
    name: str,
    image_url: str = "",
    source_account: str = "",
) -> str:
    location = (
        f'<IHeartCILocation id="{escape(station_id)}" '
        'locationType="LIVE_STATION" />'
    )
    item = (
        f'<ContentItem source="IHEART" location="{escape(location)}" '
        f'sourceAccount="{escape(source_account)}" isPresetable="true">'
        f"<itemName>{escape(name)}</itemName>"
    )
    if image_url:
        item += f"<containerArt>{escape(image_url)}</containerArt>"
    return item + "</ContentItem>"


def build_siriusxm_display_experiment_content_item(
    station_id: str,
    name: str,
    image_url: str = "",
    source_account: str = "",
) -> str:
    location = f"/experiments/siriusxm/display/playback/station/{escape(station_id)}"
    item = (
        f'<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" location="{location}" '
        f'sourceAccount="{escape(source_account)}" isPresetable="true">'
        f"<itemName>{escape(name)}</itemName>"
    )
    if image_url:
        item += f"<containerArt>{escape(image_url)}</containerArt>"
    return item + "</ContentItem>"


def build_play_content_item(store: Store, device_id: str, base_url: str, body: Json) -> str:
    source = str(body.get("source", "")).strip().upper()
    name = str(body.get("name", "")).strip()
    station_id = str(body.get("station_id", "")).strip()
    image_url = str(body.get("image_url", "")).strip()
    stream_url = str(body.get("stream_url", "")).strip()
    if not source:
        raise ValueError("source is required")
    if not name:
        raise ValueError("name is required")
    if source == "TUNEIN":
        if not station_id:
            raise ValueError("station_id is required for TuneIn")
        query = urllib.parse.urlencode({"name": name}) if name else ""
        suffix = f"?{query}" if query else ""
        return build_basic_content_item("TUNEIN", f"/v1/playback/station/{station_id}{suffix}", name, image_url)
    if source == "SIRIUSXM":
        if not station_id:
            raise ValueError("station_id is required for SiriusXM")
        station_id = station_id.rstrip("/").split("/")[-1].split("?", 1)[0]
        existing_channel = store.get_siriusxm_channel(station_id)
        store.upsert_siriusxm_channel(
            station_id,
            {
                "name": name,
                "entity_url": str(body.get("entity_url", "")).strip()
                or existing_channel.get("entity_url", ""),
                "stream_url": existing_channel.get("stream_url", ""),
            },
        )
        source_account = first_siriusxm_source_account(store, device_id)
        return build_siriusxm_content_item(
            station_id,
            name,
            image_url,
            source_account,
        )
    if source == "IHEART":
        if not station_id:
            raise ValueError("station_id is required for iHeart")
        return build_iheart_content_item(
            station_id,
            name,
            image_url,
            first_source_account_for_source(store, device_id, "IHEART"),
        )
    if source == "LOCAL_INTERNET_RADIO":
        if not stream_url:
            raise ValueError("stream_url is required for direct streams")
        if not (stream_url.startswith("http://") or stream_url.startswith("https://")):
            raise ValueError("stream_url must start with http:// or https://")
        stream_url = rewrite_iheart_descriptor_stream_url(base_url, stream_url)
        return build_basic_content_item("LOCAL_INTERNET_RADIO", stream_url, name, image_url)
    raise ValueError(f"unsupported source: {source}")


def rewrite_iheart_descriptor_stream_url(base_url: str, stream_url: str) -> str:
    parsed = urllib.parse.urlparse(stream_url)
    match = re.fullmatch(r"/iheart/stations/([^/]+)/station\.json", parsed.path)
    if not match:
        return stream_url
    return iheart_proxy_stream_url(base_url, urllib.parse.unquote(match.group(1)))


def build_basic_content_item(source: str, location: str, name: str, image_url: str = "") -> str:
    item_type = "stationurl" if source == "TUNEIN" else "url"
    item = (
        f'<ContentItem source="{escape(source)}" type="{item_type}" '
        f'location="{escape(location)}" sourceAccount="" isPresetable="true">'
        f"<itemName>{escape(name)}</itemName>"
    )
    if image_url:
        item += f"<containerArt>{escape(image_url)}</containerArt>"
    return item + "</ContentItem>"


def push_station_to_speaker(speaker: Json, raw_content_item: str, wake: bool = False) -> Json:
    ip = str(speaker.get("ip", "")).strip()
    if not ip:
        return {"attempted": False, "ok": False, "message": "speaker IP is not known"}
    before = speaker_now_playing_snapshot(ip)
    sent_content_item = redact_content_item(raw_content_item)
    print(f"[push-play] {speaker.get('device_id', '')} sent_item={sent_content_item}", flush=True)
    is_standby = str(before.get("source", "")).upper() == "STANDBY"
    woke_from_standby = False
    if is_standby and wake:
        try:
            press_speaker_key(ip, "POWER")
            time.sleep(1.2)
            woke_from_standby = True
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"[push-play] {speaker.get('device_id', '')} wake failed: {message}", flush=True)
            return {"attempted": True, "ok": False, "message": message, "before": before}
    try:
        select_content_item(ip, raw_content_item)
    except Exception as exc:
        message = speaker_select_error_message(exc)
        print(f"[push-play] {speaker.get('device_id', '')} failed: {message}", flush=True)
        return {
            "attempted": True,
            "ok": False,
            "message": message,
            "before": before,
            "woke_from_standby": woke_from_standby,
            "sent_content_item": sent_content_item,
        }
    snapshot = wait_for_selected_now_playing(ip)
    print(
        f"[push-play] {speaker.get('device_id', '')} sent /select"
        f" woke={1 if woke_from_standby else 0}"
        f" source={snapshot.get('source', '')} location={snapshot.get('location', '')}"
        f" status={snapshot.get('play_status', '')}",
        flush=True,
    )
    return {
        "attempted": True,
        "ok": True,
        "message": "sent to speaker",
        "before": before,
        "woke_from_standby": woke_from_standby,
        "sent_content_item": sent_content_item,
        "now_playing": snapshot,
    }


def speaker_select_error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if not isinstance(exc, urllib.error.HTTPError):
        return message
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        body = ""
    finally:
        try:
            exc.close()
        except Exception:
            pass
    if body:
        message += f" response={body[:1000]}"
    return message


def redact_content_item(raw_content_item: str) -> str:
    def repl(match: re.Match[str]) -> str:
        marker = "[set]" if match.group(2) else "[empty]"
        return f'{match.group(1)}{marker}{match.group(3)}'

    return re.sub(r'(sourceAccount=")([^"]*)(")', repl, raw_content_item)


def speaker_now_playing_snapshot(ip: str) -> Json:
    try:
        xml = now_playing_xml(ip)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    content_item = re.search(r"(<ContentItem\b.*?</ContentItem>)", xml, re.S | re.I)
    raw = content_item.group(1) if content_item else ""
    return {
        "source": xml_attr(raw, "source") or xml_attr(xml, "source"),
        "location": xml_attr(raw, "location"),
        "item_name": xml_tag(raw, "itemName") or xml_tag(xml, "stationName"),
        "play_status": xml_tag(xml, "playStatus"),
        "stream_type": xml_tag(xml, "streamType"),
    }


def wait_for_selected_now_playing(ip: str, attempts: int = 5, delay_seconds: float = 0.5) -> Json:
    snapshot: Json = {}
    for attempt in range(attempts):
        snapshot = speaker_now_playing_snapshot(ip)
        source = str(snapshot.get("source", "")).upper()
        if source and source != "STANDBY":
            return snapshot
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return snapshot


def rewrite_siriusxm_preset_content_item(preset: Json, experiment: bool) -> Json:
    if str(preset.get("source", "")).upper() != "SIRIUSXM":
        raise ValueError("preset is not a SiriusXM preset")
    raw = str(preset.get("raw_content_item", ""))
    source_account = xml_attr(raw, "sourceAccount")
    station_id = preset_station_slug(preset)
    station_for_normal = station_id
    if not station_for_normal.endswith("?preset_play=True"):
        station_for_normal = f"{station_for_normal}?preset_play=True"
    name = str(preset.get("name", "") or station_id)
    image_url = str(preset.get("image_url", ""))
    rewritten = dict(preset)
    rewritten["station_id"] = station_for_normal
    if experiment:
        rewritten["raw_content_item"] = build_siriusxm_display_experiment_content_item(
            station_id,
            name,
            image_url,
            source_account,
        )
    else:
        rewritten["raw_content_item"] = build_siriusxm_content_item(
            station_id,
            name,
            image_url,
            source_account,
        )
    return rewritten


def normalize_siriusxm_content_item_location(raw_content_item: str) -> str:
    location = xml_attr(raw_content_item, "location")
    if not location.startswith("/playback/station/"):
        return raw_content_item
    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("name") == [] and "name" not in query:
        return raw_content_item
    kept = [(name, value) for name, values in query.items() if name != "name" for value in values]
    normalized_query = urllib.parse.urlencode(kept)
    normalized_location = urllib.parse.urlunparse(parsed._replace(query=normalized_query))
    old_escaped = escape(location)
    new_escaped = escape(normalized_location)
    return raw_content_item.replace(f'location="{old_escaped}"', f'location="{new_escaped}"', 1)


def xml_attr(text: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}=(?:"([^"]*)"|\'([^\']*)\')', text, re.I)
    if not match:
        return ""
    value = match.group(1) if match.group(1) is not None else match.group(2)
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


def xml_tag(text: str, name: str) -> str:
    match = re.search(rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>", text, re.S | re.I)
    if not match:
        return ""
    return re.sub(r"<[^>]+>", "", match.group(1)).strip()


def remember_siriusxm_station_alias(store: Store, old_preset: Json, new_preset: Json) -> None:
    old_source = str(old_preset.get("source", "")).upper()
    new_source = str(new_preset.get("source", "")).upper()
    if new_source == "TUNEIN":
        store.clear_station_alias("TUNEIN", preset_station_slug(new_preset))
        return
    if old_source not in {"SIRIUSXM", "TUNEIN"} or new_source != "SIRIUSXM":
        return
    store.upsert_station_alias(
        old_source,
        preset_station_slug(old_preset),
        preset_station_slug(new_preset),
        new_source,
    )


def store_onboard_preset(speaker: Json, preset: Json) -> Json:
    if str(preset.get("source", "")).upper() == "EMPTY":
        return {"attempted": False, "ok": False, "message": "empty presets are not stored on the speaker"}
    ip = str(speaker.get("ip", "")).strip()
    if not ip:
        return {"attempted": False, "ok": False, "message": "speaker IP is not known"}
    slot = preset.get("slot", "")
    try:
        store_preset(ip, preset)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"[store-preset] {speaker.get('device_id', '')} slot={slot} failed: {message}", flush=True)
        return {"attempted": True, "ok": False, "message": message}
    print(f"[store-preset] {speaker.get('device_id', '')} slot={slot} stored on speaker", flush=True)
    return {"attempted": True, "ok": True, "message": "stored on speaker"}


def speaker_onboard_preset(speaker: Json, slot: int) -> Json | None:
    try:
        presets = import_presets(str(speaker.get("ip", "")))
    except Exception:
        return None
    for preset in presets:
        if int(preset.get("slot", 0)) == slot:
            return preset
    return None


def preset_station_slug(preset: Json) -> str:
    raw = str(preset.get("raw_content_item", ""))
    location = xml_attr(raw, "location") or str(preset.get("station_id", ""))
    return location.rstrip("/").split("/")[-1].split("?", 1)[0].strip()


def resolve_siriusxm_station_alias(store: Store, station_id: str) -> str:
    return store.resolve_station_alias("SIRIUSXM", station_id.split("?", 1)[0])


def resolve_station_alias_target(store: Store, source: str, station_id: str) -> Json:
    return store.resolve_station_alias_target(source, station_id.split("?", 1)[0])


def handle_migrate(req: SoundTouchBridgeHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    base_url = str(body.get("base_url") or req.server.public_base).rstrip("/")
    transcript = migrate_speaker(speaker["ip"], base_url)
    req.server.store.set_migrated(device_id, base_url)
    req.send_json({"device_id": device_id, "base_url": base_url, "transcript": transcript})


def handle_siriusxm_channels_list(req: SoundTouchBridgeHandler) -> None:
    req.send_json({"channels": req.server.store.list_siriusxm_channels()})


def handle_siriusxm_catalog(req: SoundTouchBridgeHandler) -> None:
    try:
        channels = [normalize_siriusxm_catalog_channel(channel) for channel in req.server.siriusxm.get_channels()]
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        req.send_json({"error": "siriusxm_catalog_failed", "message": message}, 502)
        return
    channels = [channel for channel in channels if channel.get("station_id")]
    req.send_json({"channels": channels, "session": req.server.siriusxm.status()})


def handle_siriusxm_session(req: SoundTouchBridgeHandler) -> None:
    req.send_json({"session": req.server.siriusxm.status()})


def handle_siriusxm_session_login(req: SoundTouchBridgeHandler) -> None:
    try:
        req.server.siriusxm.login()
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        req.send_json({"error": "siriusxm_login_failed", "message": message}, 502)
        return
    req.send_json({"session": req.server.siriusxm.status()})


def handle_siriusxm_channel_get(req: SoundTouchBridgeHandler, station_id: str) -> None:
    req.send_json({"channel": req.server.store.get_siriusxm_channel(station_id)})


def handle_siriusxm_channel_put(req: SoundTouchBridgeHandler, station_id: str) -> None:
    body = req.read_json()
    try:
        channel = normalize_siriusxm_channel(body)
    except ValueError as exc:
        req.send_json({"error": "invalid_siriusxm_channel", "message": str(exc)}, 400)
        return
    saved = req.server.store.upsert_siriusxm_channel(station_id, channel)
    req.send_json({"channel": saved})


def handle_siriusxm_channel_refresh(req: SoundTouchBridgeHandler, station_id: str) -> None:
    try:
        stream_url = resolve_siriusxm_stream_url(req.server.store, req.server.siriusxm, station_id, force=True)
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        req.send_json({"error": "siriusxm_refresh_failed", "message": message}, 502)
        return
    req.send_json(
        {
            "station_id": station_id,
            "stream_url_refreshed": bool(stream_url),
            "channel": req.server.store.get_siriusxm_channel(station_id),
            "session": req.server.siriusxm.status(),
        }
    )


def handle_siriusxm_now_playing_debug(req: SoundTouchBridgeHandler, station_id: str) -> None:
    channel = req.server.store.get_siriusxm_channel(station_id)
    visible_channel = {
        "station_id": channel.get("station_id") or station_id,
        "name": channel.get("name", ""),
        "entity_url": channel.get("entity_url", ""),
        "has_stream_url": bool(channel.get("stream_url")),
    }
    if not req.server.siriusxm.credentials.configured:
        req.send_json(
            {
                "station_id": station_id,
                "channel": visible_channel,
                "metadata": {},
                "debug": {"sources": [], "error": "SiriusXM credentials are not configured"},
                "session": req.server.siriusxm.status(),
            },
            501,
        )
        return
    try:
        metadata = req.server.siriusxm.now_playing(station_id, channel, force=True)
        status = 200
        error = ""
    except Exception as exc:
        metadata = {}
        status = 502
        error = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
    debug = dict(req.server.siriusxm.last_now_playing_debug.get(station_id, {}))
    if error:
        debug["error"] = error
    req.send_json(
        {
            "station_id": station_id,
            "channel": visible_channel,
            "metadata": metadata,
            "debug": debug,
            "session": req.server.siriusxm.status(),
        },
        status,
    )


def handle_tunein_icy_debug(req: SoundTouchBridgeHandler, station_id: str) -> None:
    try:
        payload = tunein_icy_debug_payload(req.server.store, station_id, req.server.public_base)
    except Exception as exc:
        req.send_json({"station_id": station_id, "error": type(exc).__name__, "message": str(exc)}, 502)
        return
    req.send_json(payload)


def handle_siriusxm_metadata_proxy_debug(req: SoundTouchBridgeHandler, station_id: str) -> None:
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    channel = req.server.store.get_siriusxm_channel(station_id)
    metadata: Json = {}
    error = ""
    if req.server.siriusxm.credentials.configured:
        try:
            metadata = req.server.siriusxm.now_playing(station_id, channel, force=True)
        except Exception as exc:
            error = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
    payload = siriusxm_metadata_proxy_debug_payload(req.server.store, station_id, req.server.public_base, metadata)
    payload["session"] = req.server.siriusxm.status()
    if error:
        payload["metadata_error"] = error
    req.send_json(payload)


def tunein_icy_debug_payload(
    store: Store,
    station_id: str,
    base_url: str,
    inspector: Callable[[str], Json] = inspect_icy_stream,
) -> Json:
    resolved = cloud_api._resolve_tunein(station_id)
    preset = store.find_preset_by_source_station("TUNEIN", station_id)
    name = resolved.get("name") or (preset.get("name") if preset else "") or station_id
    stream_url = resolved.get("url") or f"{base_url}/silence.mp3"
    icy: Json = {}
    if stream_url.startswith(("http://", "https://")):
        icy = inspector(stream_url)
    return {
        "station_id": station_id,
        "name": name,
        "stream_url": stream_url,
        "tunein": {
            "media_type": resolved.get("media_type", ""),
            "image": resolved.get("image", ""),
        },
        "icy": icy,
    }


def siriusxm_metadata_proxy_debug_payload(
    store: Store,
    station_id: str,
    base_url: str,
    metadata: Json | None = None,
) -> Json:
    preset = store.find_preset_by_source_station("SIRIUSXM", station_id)
    channel = store.get_siriusxm_channel(station_id)
    name = channel.get("name") or (preset.get("name") if preset else "") or station_id
    stream_url = f"{base_url}/siriusxm/proxy/{urllib.parse.quote(station_id)}/playlist.m3u8"
    return {
        "station_id": station_id,
        "name": name,
        "transport": "hls",
        "stream_url": stream_url,
        "metadata": metadata or {},
        "icy_metadata_injection_feasible": False,
        "reason": (
            "ICY metadata is for continuous streams with an icy-metaint interval. "
            "SiriusXM playback here is HLS, so simple ICY injection cannot be added to this playlist proxy."
        ),
        "next_experiment": (
            "Test whether the speaker displays timed ID3 metadata inside HLS AAC segments; "
            "that would require segment rewriting and is separate from ICY metadata."
        ),
    }


def normalize_siriusxm_channel(body: Json) -> Json:
    name = str(body.get("name", "")).strip()
    entity_url = str(body.get("entity_url", "")).strip()
    stream_url = str(body.get("stream_url", "")).strip()
    if entity_url and not entity_url.startswith("https://www.siriusxm.com/player/"):
        raise ValueError("entity_url should be the SiriusXM web player URL")
    if stream_url:
        if "siriusxm.com/player/" in stream_url:
            raise ValueError("stream_url must be a direct playable audio URL, not the SiriusXM web player URL")
        validate_siriusxm_proxy_url(stream_url)
    return {"name": name, "entity_url": entity_url, "stream_url": stream_url}


def normalize_siriusxm_catalog_channel(channel: Json) -> Json:
    name = str(channel.get("channelName") or channel.get("name") or "").strip()
    station_id = str(
        channel.get("urlKey")
        or channel.get("key")
        or channel.get("stationId")
        or channel.get("channelId")
        or channel.get("channelGuid")
        or channel.get("guid")
        or ""
    ).strip()
    if station_id.isdigit():
        station_id = siriusxm_display_slug(name) or station_id
    guid = str(
        channel.get("channelGuid")
        or channel.get("assetGUID")
        or channel.get("guid")
        or channel.get("entityGuid")
        or ""
    ).strip()
    name = name or station_id
    number = str(channel.get("channelNumberCanonical") or channel.get("channelNumber") or "").strip()
    image_url = first_siriusxm_image_url(channel)
    return {
        "station_id": station_id,
        "name": name,
        "number": number,
        "entity_url": f"https://www.siriusxm.com/player/channel-linear/entity/{guid}" if guid else "",
        "image_url": image_url,
    }


def siriusxm_display_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def handle_tunein_search(req: SoundTouchBridgeHandler) -> None:
    query = parse_qs(urlparse(req.path).query).get("q", [""])[0].strip()
    if not query:
        req.send_json({"stations": []})
        return
    try:
        stations = search_tunein_stations(query)
    except Exception as exc:
        req.send_json({"error": "tunein_search_failed", "message": str(exc)}, 502)
        return
    req.send_json({"stations": stations})


def handle_iheart_search(req: SoundTouchBridgeHandler) -> None:
    query = parse_qs(urlparse(req.path).query).get("q", [""])[0].strip()
    if not query:
        req.send_json({"stations": []})
        return
    try:
        stations = [
            iheart_station_with_proxy_urls(req.server.public_base, station)
            for station in search_iheart_stations(query)
        ]
    except Exception as exc:
        req.send_json({"error": "iheart_search_failed", "message": str(exc)}, 502)
        return
    req.send_json({"stations": stations})


def handle_iheart_station_stream(req: SoundTouchBridgeHandler, station_id: str) -> None:
    query = parse_qs(urlparse(req.path).query)
    name = (query.get("name") or [""])[0]
    image_url = (query.get("image") or [""])[0]
    try:
        upstream_stream_url = resolve_iheart_stream_url(station_id)
    except Exception as exc:
        req.send_json({"error": "iheart_stream_failed", "message": str(exc)}, 502)
        return
    req.send_json(
        {
            "station_id": station_id,
            "stream_url": iheart_station_descriptor_url(req.server.public_base, station_id, name, image_url),
            "playlist_url": iheart_playlist_url(req.server.public_base, station_id),
            "proxy_stream_url": iheart_proxy_stream_url(req.server.public_base, station_id),
            "upstream_stream_url": upstream_stream_url,
        }
    )


def handle_iheart_station_descriptor(req: SoundTouchBridgeHandler, station_id: str) -> None:
    query = parse_qs(urlparse(req.path).query)
    name = (query.get("name") or [station_id])[0]
    image_url = (query.get("image") or [""])[0]
    req.send_json(iheart_station_descriptor(req.server.public_base, station_id, name, image_url))


def handle_siriusxm_station_descriptor(req: SoundTouchBridgeHandler, station_id: str) -> None:
    query = parse_qs(urlparse(req.path).query)
    name = (query.get("name") or [station_id])[0]
    image_url = (query.get("image") or [""])[0]
    channel = req.server.store.get_siriusxm_channel(station_id)
    if name and not channel.get("name"):
        req.server.store.upsert_siriusxm_channel(station_id, {"name": name})
    metadata = {
        "stationName": name,
        "channelName": name,
        "trackName": name,
        "artistName": "SiriusXM",
        "imageUrl": image_url,
        "containerArt": image_url,
    }
    req.send_bytes(
        siriusxm_station(req.server.store, station_id, req.server.public_base, metadata),
        content_type="application/json",
    )


def handle_iheart_proxy_playlist(req: SoundTouchBridgeHandler, station_id: str) -> None:
    body = iheart_playlist_body(req.server.public_base, station_id)
    req.send_text(body, content_type="audio/x-mpegurl")


def handle_iheart_proxy_stream(req: SoundTouchBridgeHandler, station_id: str) -> None:
    try:
        upstream_stream_url = resolve_iheart_stream_url(station_id)
        with urllib.request.urlopen(upstream_stream_url, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type") or "audio/aac"
            req.send_response(200)
            req.send_header("Content-Type", content_type)
            req.send_header("Cache-Control", "no-store")
            req.end_headers()
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                req.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        print(f"[iheart-proxy] client disconnected station={station_id}", flush=True)
    except Exception as exc:
        try:
            req.send_json({"error": "iheart_proxy_failed", "message": str(exc)}, 502)
        except (BrokenPipeError, ConnectionResetError):
            print(f"[iheart-proxy] client disconnected after error station={station_id}", flush=True)


def search_tunein_stations(query: str, limit: int = 60) -> list[Json]:
    url = "http://opml.radiotime.com/Search.ashx?" + urllib.parse.urlencode(
        {"query": query, "types": "station", "render": "json"}
    )
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    items = data.get("body", data)
    if isinstance(items, dict):
        items = items.get("children") or items.get("items") or [items]
    if not isinstance(items, list):
        return []
    stations: list[Json] = []
    seen: set[str] = set()
    for item in items:
        station = normalize_tunein_search_station(item)
        station_id = station.get("station_id", "")
        if not station_id or station_id in seen:
            continue
        stations.append(station)
        seen.add(station_id)
        if len(stations) >= limit:
            break
    return stations


def normalize_tunein_search_station(item: Any) -> Json:
    if not isinstance(item, dict):
        return {}
    item_type = str(item.get("type", "")).strip().lower()
    if item_type and item_type not in {"audio", "station"}:
        return {}
    station_id = str(item.get("guide_id") or item.get("id") or item.get("station_id") or "").strip()
    if not station_id:
        url = str(item.get("URL") or item.get("url") or "").strip()
        station_id = parse_qs(urlparse(url).query).get("id", [""])[0].strip()
    if not station_id or not station_id.lower().startswith("s"):
        return {}
    name = str(item.get("text") or item.get("name") or "").strip()
    if not name:
        return {}
    return {
        "station_id": station_id,
        "name": name,
        "description": str(item.get("subtext") or item.get("description") or "").strip(),
        "image_url": str(item.get("image") or item.get("logo") or "").strip(),
    }


def search_iheart_stations(query: str, limit: int = 60) -> list[Json]:
    url = "https://api.iheart.com/api/v1/catalog/searchAll?" + urllib.parse.urlencode({"keywords": query})
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    items = data.get("stations", [])
    if not isinstance(items, list):
        return []
    stations: list[Json] = []
    seen: set[str] = set()
    for item in items:
        station = normalize_iheart_search_station(item)
        station_id = station.get("station_id", "")
        if not station_id or station_id in seen:
            continue
        stations.append(station)
        seen.add(station_id)
        if len(stations) >= limit:
            break
    return stations


def normalize_iheart_search_station(item: Any) -> Json:
    if not isinstance(item, dict):
        return {}
    station_id = str(item.get("id") or item.get("station_id") or "").strip()
    name = str(item.get("name") or "").strip()
    if not station_id or not name:
        return {}
    description = str(item.get("description") or "").strip()
    city = str(item.get("city") or "").strip()
    state = str(item.get("state") or "").strip()
    if not description:
        description = ", ".join(part for part in (city, state) if part)
    return {
        "station_id": station_id,
        "name": name,
        "description": description,
        "image_url": str(item.get("logo") or item.get("newlogo") or "").strip(),
    }


def iheart_station_with_proxy_urls(base_url: str, station: Json) -> Json:
    station_id = str(station.get("station_id", "")).strip()
    if not station_id:
        return station
    stream_url = iheart_proxy_stream_url(base_url, station_id)
    return {
        **station,
        "stream_url": stream_url,
        "proxy_stream_url": stream_url,
    }


def iheart_proxy_stream_url(base_url: str, station_id: str) -> str:
    return f"{base_url.strip().rstrip('/')}/iheart/proxy/{urllib.parse.quote(station_id.strip())}/stream.aac"


def iheart_playlist_url(base_url: str, station_id: str) -> str:
    return f"{base_url.strip().rstrip('/')}/iheart/proxy/{urllib.parse.quote(station_id.strip())}/playlist.m3u"


def iheart_playlist_body(base_url: str, station_id: str) -> str:
    return f"{iheart_proxy_stream_url(base_url, station_id)}\n"


def iheart_station_descriptor_url(
    base_url: str,
    station_id: str,
    name: str = "",
    image_url: str = "",
) -> str:
    url = f"{base_url.strip().rstrip('/')}/iheart/stations/{urllib.parse.quote(station_id.strip())}/station.json"
    query: dict[str, str] = {}
    if name:
        query["name"] = name
    if image_url:
        query["image"] = image_url
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def siriusxm_station_descriptor_url(
    base_url: str,
    station_id: str,
    name: str = "",
    image_url: str = "",
) -> str:
    url = f"{base_url.strip().rstrip('/')}/siriusxm/stations/{urllib.parse.quote(station_id.strip())}/station.json"
    query: dict[str, str] = {}
    if name:
        query["name"] = name
    if image_url:
        query["image"] = image_url
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def siriusxm_playlist_url(base_url: str, station_id: str) -> str:
    return f"{base_url.strip().rstrip('/')}/siriusxm/proxy/{urllib.parse.quote(station_id.strip())}/playlist.m3u8"


def iheart_station_descriptor(base_url: str, station_id: str, name: str, image_url: str = "") -> Json:
    playlist = iheart_playlist_url(base_url, station_id)
    return {
        "audio": {
            "hasPlaylist": True,
            "isRealtime": True,
            "streamUrl": playlist,
        },
        "imageUrl": image_url,
        "name": name or station_id,
        "streamType": "liveRadio",
    }


def resolve_iheart_stream_url(station_id: str) -> str:
    station_id = station_id.strip()
    if not station_id.isdigit():
        raise ValueError("iHeart station id must be numeric")
    url = f"https://api.iheart.com/api/v2/content/liveStations/{urllib.parse.quote(station_id)}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    hits = data.get("hits", [])
    if not isinstance(hits, list) or not hits:
        raise ValueError("iHeart station was not found")
    streams = hits[0].get("streams", {})
    if not isinstance(streams, dict):
        raise ValueError("iHeart station has no streams")
    for key in (
        "secure_shoutcast_stream",
        "shoutcast_stream",
        "secure_hls_stream",
        "hls_stream",
        "stw_stream",
        "secure_pls_stream",
        "pls_stream",
    ):
        stream_url = str(streams.get(key) or "").strip()
        if stream_url:
            return stream_url
    raise ValueError("iHeart station has no playable stream URL")


def first_siriusxm_image_url(value: Any) -> str:
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and url:
            if url.startswith("http://") or url.startswith("https://"):
                return url
            if url.startswith("/content/"):
                return f"https://www.siriusxm.com{url}"
            return f"http://pri.art.prod.streaming.siriusxm.com/{url.lstrip('/')}"
        for child in value.values():
            found = first_siriusxm_image_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = first_siriusxm_image_url(child)
            if found:
                return found
    return ""


def resolve_siriusxm_stream_url(store: Store, session: SiriusXmSession, station_id: str, force: bool = False) -> str:
    channel = store.get_siriusxm_channel(station_id)
    stream_url = str(channel.get("stream_url", ""))
    if stream_url and not force:
        return stream_url
    if session.credentials.configured:
        try:
            stream_url = session.refresh_stream_url(station_id, channel)
        except Exception as exc:
            if is_siriusxm_auth_error(exc):
                try:
                    session.login()
                    stream_url = session.refresh_stream_url(station_id, channel)
                except Exception as retry_exc:
                    message = sanitize_siriusxm_error(str(retry_exc), session.credentials)
                    store.update_siriusxm_stream_status(
                        station_id,
                        stream_url=None,
                        last_refresh_error=message,
                    )
                    raise SiriusXmError(message) from retry_exc
                store.update_siriusxm_stream_status(
                    station_id,
                    stream_url=stream_url,
                    last_refresh_error="",
                )
                return stream_url
            message = sanitize_siriusxm_error(str(exc), session.credentials)
            store.update_siriusxm_stream_status(
                station_id,
                stream_url=None,
                last_refresh_error=message,
            )
            raise SiriusXmError(message) from exc
        store.update_siriusxm_stream_status(
            station_id,
            stream_url=stream_url,
            last_refresh_error="",
        )
        return stream_url
    if stream_url:
        return stream_url
    raise SiriusXmNotConfigured("SiriusXM credentials are not configured")


def should_retry_siriusxm_fetch(session: SiriusXmSession, exc: Exception) -> bool:
    return session.credentials.configured and should_refresh_stream("configured", exc)


def is_siriusxm_auth_error(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)


def sanitize_siriusxm_error(message: str, credentials: SiriusXmCredentials) -> str:
    sanitized = re.sub(r"https://[^\s\"']+", "[redacted-url]", message)
    for secret in (credentials.username, credentials.password):
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    sanitized = re.sub(r"(token|gupId|password|username)=([^&\s]+)", r"\1=[redacted]", sanitized, flags=re.I)
    return sanitized[:500]


def handle_bmx_services(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(bmx_services(req.server.public_base), content_type="application/json")


def handle_bmx_services_availability(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(bmx_services_availability(), content_type="application/json")


def handle_sourceproviders(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(sourceproviders_xml(), content_type="application/vnd.bose.streaming-v1.2+xml")


def handle_tunein_token(req: SoundTouchBridgeHandler) -> None:
    req.send_bytes(tunein_token(), content_type="application/json")


def handle_tunein_station(req: SoundTouchBridgeHandler, station_id: str) -> None:
    query = parse_qs(urlparse(getattr(req, "path", "")).query)
    display_name = query.get("name", [""])[0].strip()
    body = tunein_station(req.server.store, station_id, req.server.public_base, display_name=display_name)
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_token(req: SoundTouchBridgeHandler) -> None:
    drain_request_body(req)
    body = siriusxm_token()
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_availability(req: SoundTouchBridgeHandler) -> None:
    body = siriusxm_availability()
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_station(req: SoundTouchBridgeHandler, station_id: str) -> None:
    requested_station_id = station_id
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    query = parse_qs(urlparse(req.path).query)
    display_name = query.get("name", [""])[0].strip()
    metadata = {}
    channel = req.server.store.get_siriusxm_channel(station_id)
    if req.server.siriusxm.credentials.configured:
        try:
            metadata = req.server.siriusxm.now_playing(station_id, channel)
        except Exception as exc:
            message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
            capture_cloud_response(req, "siriusxm", f"resolver metadata failed station={requested_station_id}->{station_id}: {message}")
    body = siriusxm_station(req.server.store, station_id, req.server.public_base, metadata, display_name=display_name)
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_display_experiment(req: SoundTouchBridgeHandler, station_id: str) -> None:
    requested_station_id = station_id
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    metadata = {}
    channel = req.server.store.get_siriusxm_channel(station_id)
    if req.server.siriusxm.credentials.configured:
        try:
            metadata = req.server.siriusxm.now_playing(station_id, channel, force=True)
        except Exception as exc:
            message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
            capture_cloud_response(req, "siriusxm", f"display experiment metadata failed station={requested_station_id}->{station_id}: {message}")
    body = siriusxm_station_display_experiment(req.server.store, station_id, req.server.public_base, metadata)
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_now_playing(req: SoundTouchBridgeHandler, station_id: str) -> None:
    requested_station_id = station_id
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    metadata = {}
    channel = req.server.store.get_siriusxm_channel(station_id)
    if req.server.siriusxm.credentials.configured:
        try:
            metadata = req.server.siriusxm.now_playing(station_id, channel)
        except Exception as exc:
            message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
            capture_cloud_response(req, "siriusxm", f"now-playing metadata failed station={requested_station_id}->{station_id}: {message}")
    body = siriusxm_now_playing(req.server.store, station_id, metadata)
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_proxy_playlist(req: SoundTouchBridgeHandler, station_id: str) -> None:
    handle_siriusxm_proxy_playlist_impl(req, station_id, inject_metadata=False)


def handle_siriusxm_metadata_proxy_playlist(req: SoundTouchBridgeHandler, station_id: str) -> None:
    handle_siriusxm_proxy_playlist_impl(req, station_id, inject_metadata=True)


def handle_siriusxm_proxy_playlist_impl(req: SoundTouchBridgeHandler, station_id: str, inject_metadata: bool = False) -> None:
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    try:
        stream_url = resolve_siriusxm_stream_url(req.server.store, req.server.siriusxm, station_id)
    except SiriusXmNotConfigured:
        channel = req.server.store.get_siriusxm_channel(station_id)
        stream_url = channel.get("stream_url", "")
        if not stream_url:
            req.send_json(
                {
                    "error": "siriusxm_not_configured",
                    "station_id": station_id,
                    "message": "Create /etc/soundtouch-bridge/siriusxm.env with SIRIUSXM_USERNAME and SIRIUSXM_PASSWORD.",
                },
                501,
            )
            return
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        capture_cloud_response(req, "siriusxm", f"authenticated stream refresh failed station={station_id}: {message}")
        req.send_json({"error": "siriusxm_refresh_failed", "message": message}, 502)
        return
    try:
        body = cached_fetch_siriusxm_url(stream_url, req.server.siriusxm_fetch_cache).decode("utf-8", "replace")
    except Exception as exc:
        if should_retry_siriusxm_fetch(req.server.siriusxm, exc):
            try:
                stream_url = resolve_siriusxm_stream_url(
                    req.server.store,
                    req.server.siriusxm,
                    station_id,
                    force=True,
                )
                body = cached_fetch_siriusxm_url(
                    stream_url,
                    req.server.siriusxm_fetch_cache,
                    force=True,
                ).decode("utf-8", "replace")
            except Exception as retry_exc:
                message = sanitize_siriusxm_error(str(retry_exc), req.server.siriusxm.credentials)
                capture_cloud_response(req, "siriusxm", f"authenticated playlist fetch failed station={station_id}: {message}")
                req.send_json({"error": "siriusxm_playlist_failed", "message": message}, 502)
                return
        else:
            message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
            capture_cloud_response(req, "siriusxm", f"playlist fetch failed station={station_id}: {message}")
            req.send_json({"error": type(exc).__name__, "message": message}, 502)
            return
    metadata: Json = {}
    if inject_metadata:
        try:
            channel = req.server.store.get_siriusxm_channel(station_id)
            metadata = req.server.siriusxm.now_playing(station_id, channel)
        except Exception as exc:
            message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
            capture_cloud_response(req, "siriusxm", f"playlist metadata lookup failed station={station_id}: {message}")
    trimmed = trim_hls_playlist(body)
    rewritten = rewrite_hls_playlist(
        trimmed,
        stream_url,
        req.server,
        station_id=station_id,
        inject_metadata=inject_metadata,
        metadata=metadata,
    )
    if should_capture_siriusxm_playlist_success():
        capture_cloud_response(req, "siriusxm", summarize_hls_playlist(station_id, rewritten))
    req.send_bytes(rewritten.encode("utf-8"), content_type="application/x-mpegURL")


def handle_siriusxm_proxy_fetch(req: SoundTouchBridgeHandler, token: str = "") -> None:
    if not token:
        req.send_json({"error": "missing_proxy_token"}, 400)
        return
    target = req.server.siriusxm_proxy_urls.get(token, "")
    try:
        validate_siriusxm_proxy_url(target)
    except ValueError:
        req.send_json({"error": "invalid_proxy_url"}, 400)
        return
    if is_siriusxm_hls_key(target):
        maybe_capture_siriusxm_fetch_success(
            req,
            target,
            f"served local hls key path={urlparse(target).path} bytes={len(SIRIUSXM_HLS_AES_KEY)}",
        )
        req.send_bytes(SIRIUSXM_HLS_AES_KEY, content_type="application/octet-stream")
        return
    try:
        body = cached_fetch_siriusxm_url(target, req.server.siriusxm_fetch_cache)
    except Exception as exc:
        capture_cloud_response(req, "siriusxm", describe_siriusxm_fetch_error(target, exc))
        req.send_json({"error": type(exc).__name__, "message": str(exc)}, 502)
        return
    content_type = "application/octet-stream"
    lowered = urlparse(target).path.lower()
    if lowered.endswith(".aac"):
        content_type = "audio/aac"
    elif "key/" in target:
        content_type = "application/octet-stream"
    maybe_capture_siriusxm_fetch_success(
        req, target, f"proxied fetch path={urlparse(target).path} bytes={len(body)}"
    )
    req.send_bytes(body, content_type=content_type)


def handle_siriusxm_metadata_proxy_fetch(req: SoundTouchBridgeHandler, station_id: str, token: str) -> None:
    station_id = resolve_siriusxm_station_alias(req.server.store, station_id)
    target = req.server.siriusxm_proxy_urls.get(token, "")
    try:
        validate_siriusxm_proxy_url(target)
    except ValueError:
        req.send_json({"error": "invalid_proxy_url"}, 400)
        return
    if is_siriusxm_hls_key(target):
        req.send_bytes(SIRIUSXM_HLS_AES_KEY, content_type="application/octet-stream")
        return
    try:
        body = cached_fetch_siriusxm_url(target, req.server.siriusxm_fetch_cache)
    except Exception as exc:
        capture_cloud_response(req, "siriusxm", describe_siriusxm_fetch_error(target, exc))
        req.send_json({"error": type(exc).__name__, "message": str(exc)}, 502)
        return
    metadata: Json = {}
    try:
        channel = req.server.store.get_siriusxm_channel(station_id)
        metadata = req.server.siriusxm.now_playing(station_id, channel)
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        capture_cloud_response(req, "siriusxm", f"id3 metadata lookup failed station={station_id}: {message}")
    injected = inject_id3_metadata(body, metadata)
    req.send_bytes(injected, content_type="audio/aac")


def fetch_siriusxm_url(url: str) -> bytes:
    validate_siriusxm_proxy_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Origin": "https://www.siriusxm.com",
            "Referer": "https://www.siriusxm.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        final_url = getattr(response, "url", url)
        validate_siriusxm_proxy_url(str(final_url))
        body = response.read(MAX_SIRIUSXM_FETCH_BYTES + 1)
        if len(body) > MAX_SIRIUSXM_FETCH_BYTES:
            raise ValueError("SiriusXM response exceeded maximum proxy size")
        return body


def cached_fetch_siriusxm_url(
    url: str,
    cache: dict[str, tuple[float, bytes]],
    now: float | None = None,
    fetcher: Callable[[str], bytes] = fetch_siriusxm_url,
    ttl: float = 8.0,
    force: bool = False,
) -> bytes:
    current = time.time() if now is None else now
    cached = cache.get(url)
    if not force and cached and current - cached[0] < ttl:
        return cached[1]
    body = fetcher(url)
    cache[url] = (current, body)
    if len(cache) > 512:
        cutoff = current - ttl
        for key, (stored_at, _) in list(cache.items()):
            if stored_at < cutoff:
                cache.pop(key, None)
    return body


def validate_siriusxm_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("SiriusXM proxy URL must be an allowed absolute URL")
    if parsed.username or parsed.password:
        raise ValueError("SiriusXM proxy URL must not include credentials")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("SiriusXM proxy URL must include a host")
    if is_blocked_proxy_host(host):
        raise ValueError("SiriusXM proxy URL host is not allowed")
    if host == "api.edge-gateway.siriusxm.com":
        return url
    if host.endswith(".streaming.siriusxm.com") or host == "streaming.siriusxm.com":
        return url
    if host.endswith(".siriusxm.com") or host == "siriusxm.com":
        return url
    if host.endswith(".akamaized.net") and "siriusxm" in host:
        return url
    raise ValueError("SiriusXM proxy URL host is not allowed")


def is_blocked_proxy_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_private


def is_siriusxm_hls_key(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc == "api.edge-gateway.siriusxm.com" and parsed.path.startswith("/playback/key/"):
        return True
    if parsed.netloc.endswith(".akamaized.net") and "siriusxm" in parsed.netloc:
        return "/key/" in parsed.path.lower()
    return False


def should_capture_siriusxm_fetch_success(target: str) -> bool:
    parsed = urlparse(target)
    path = parsed.path.lower()
    if is_siriusxm_hls_key(target):
        return False
    return not path.endswith(".aac")


def should_capture_siriusxm_playlist_success() -> bool:
    return False


def maybe_capture_siriusxm_fetch_success(req: SoundTouchBridgeHandler, target: str, body: str) -> None:
    if should_capture_siriusxm_fetch_success(target):
        capture_cloud_response(req, "siriusxm", body)


def rewrite_hls_playlist(
    body: str,
    playlist_url: str,
    server: SoundTouchBridgeServer,
    station_id: str = "",
    inject_metadata: bool = False,
    metadata: Json | None = None,
) -> str:
    rewritten: list[str] = []
    encrypted = hls_playlist_is_encrypted(body)
    title = hls_metadata_title(metadata or {}) if inject_metadata and encrypted else ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY:"):
            rewritten.append(rewrite_hls_key_line(line, playlist_url, server))
        elif title and stripped.startswith("#EXTINF:"):
            prefix = line.split(",", 1)[0]
            rewritten.append(f"{prefix},{title}")
        elif stripped and not stripped.startswith("#"):
            target = inherit_playlist_auth_query(urljoin(playlist_url, stripped), playlist_url)
            rewritten.append(
                proxy_url(
                    target,
                    server,
                    absolute=False,
                    station_id=station_id,
                    inject_metadata=inject_metadata and not encrypted,
                )
            )
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


def hls_playlist_is_encrypted(body: str) -> bool:
    for line in body.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in stripped:
            return True
    return False


def hls_metadata_title(metadata: Json) -> str:
    track = str(metadata.get("trackName") or "").strip()
    artist = str(metadata.get("artistName") or "").strip()
    if track and artist and artist.lower() != "siriusxm":
        return f"{artist} - {track}".replace("\n", " ").replace("\r", " ")
    return track.replace("\n", " ").replace("\r", " ")


def inherit_playlist_auth_query(target: str, playlist_url: str) -> str:
    target_parts = urlparse(target)
    playlist_parts = urlparse(playlist_url)
    if target_parts.query or not playlist_parts.query:
        return target
    if target_parts.netloc != playlist_parts.netloc:
        return target
    query = urllib.parse.parse_qs(playlist_parts.query)
    auth_query = {
        name: values[-1]
        for name, values in query.items()
        if name in {"token", "gupId", "consumer"} and values
    }
    if not auth_query:
        return target
    return urllib.parse.urlunparse(
        target_parts._replace(query=urllib.parse.urlencode(auth_query))
    )


def trim_hls_playlist(body: str, max_segments: int = 12) -> str:
    header: list[str] = []
    groups: list[list[str]] = []
    pending: list[str] = []
    seen_media = False
    media_sequence = 0

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(stripped.split(":", 1)[1])
            except ValueError:
                media_sequence = 0
            header.append(line)
            continue
        if stripped and not stripped.startswith("#"):
            seen_media = True
            groups.append([*pending, line])
            pending = []
            continue
        if seen_media or is_hls_segment_tag(stripped):
            pending.append(line)
        else:
            header.append(line)

    if len(groups) <= max_segments:
        return body

    skipped = len(groups) - max_segments
    trimmed_header: list[str] = []
    for line in header:
        if line.strip().startswith("#EXT-X-MEDIA-SEQUENCE:"):
            trimmed_header.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence + skipped}")
        else:
            trimmed_header.append(line)
    trimmed: list[str] = [*trimmed_header]
    for group in groups[-max_segments:]:
        trimmed.extend(group)
    return "\n".join(trimmed) + "\n"


def is_hls_segment_tag(line: str) -> bool:
    return line.startswith(
        (
            "#EXTINF:",
            "#EXT-X-BYTERANGE:",
            "#EXT-X-DISCONTINUITY",
            "#EXT-X-PROGRAM-DATE-TIME:",
            "#EXT-X-MAP:",
        )
    )


def rewrite_hls_key_line(line: str, playlist_url: str, server: SoundTouchBridgeServer) -> str:
    def repl(match: re.Match[str]) -> str:
        return f'URI="{proxy_url(urljoin(playlist_url, match.group(1)), server, absolute=False)}"'

    return re.sub(r'URI="([^"]+)"', repl, line)


def proxy_url(
    target: str,
    server: SoundTouchBridgeServer,
    absolute: bool = True,
    station_id: str = "",
    inject_metadata: bool = False,
) -> str:
    validate_siriusxm_proxy_url(target)
    token = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    server.siriusxm_proxy_urls[token] = target
    if inject_metadata and station_id and not is_siriusxm_hls_key(target):
        path = f"/siriusxm/proxy/meta/{urllib.parse.quote(station_id)}/{token}"
    else:
        path = f"/siriusxm/proxy/fetch/{token}"
    if absolute:
        return f"{server.public_base}{path}"
    return path


def inject_id3_metadata(segment: bytes, metadata: Json | None = None) -> bytes:
    tag = build_id3_text_tag(metadata or {})
    return tag + segment if tag else segment


def build_id3_text_tag(metadata: Json) -> bytes:
    frames = b"".join(
        frame
        for frame in (
            id3_text_frame("TIT2", str(metadata.get("trackName") or "")),
            id3_text_frame("TPE1", str(metadata.get("artistName") or "")),
            id3_text_frame("TALB", str(metadata.get("albumName") or "")),
        )
        if frame
    )
    if not frames:
        return b""
    return b"ID3" + bytes([4, 0, 0]) + syncsafe(len(frames)) + frames


def id3_text_frame(frame_id: str, value: str) -> bytes:
    text = value.strip()
    if not text:
        return b""
    payload = b"\x03" + text.encode("utf-8")
    return frame_id.encode("ascii") + syncsafe(len(payload)) + b"\x00\x00" + payload


def syncsafe(value: int) -> bytes:
    return bytes(
        [
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        ]
    )


def summarize_hls_playlist(station_id: str, playlist: str) -> str:
    lines = playlist.splitlines()
    media = sum(1 for line in lines if line and not line.startswith("#"))
    keys = sum(1 for line in lines if line.startswith("#EXT-X-KEY:"))
    preview = "\n".join(lines[:12])
    preview = re.sub(r"/siriusxm/proxy/fetch/[A-Fa-f0-9]+", "/siriusxm/proxy/fetch/[token]", preview)
    return f"proxied playlist station={station_id} lines={len(lines)} keys={keys} media={media}\n{preview}"


def describe_siriusxm_fetch_error(target: str, exc: Exception) -> str:
    parsed = urlparse(target)
    location = f"{parsed.netloc}{parsed.path}"
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read(512).decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        body = re.sub(r"\s+", " ", body)
        return (
            f"proxied fetch error host_path={location} "
            f"http_status={exc.code} reason={exc.reason} body={body[:240]}"
        )
    if isinstance(exc, urllib.error.URLError):
        return f"proxied fetch error host_path={location} url_error={exc.reason}"
    return f"proxied fetch error host_path={location} error={type(exc).__name__}: {exc}"


def handle_siriusxm_needs_auth(req: SoundTouchBridgeHandler, station_id: str) -> None:
    req.send_json(
        {
            "error": "siriusxm_stream_auth_required",
            "station_id": station_id,
            "message": "The preserved SiriusXM preset reached the local adapter, but this MVP still needs authenticated SiriusXM stream URL resolution.",
        },
        501,
    )


def handle_scmudc(req: SoundTouchBridgeHandler, device_id: str) -> None:
    body = req.read_text()
    summary = summarize_scmudc(body)
    if summary:
        req.server.store.add_scmudc_event(device_id, summary, truncate_diagnostic_body(body))
        print(f"[scmudc] {device_id} {summary}")
    req.send_text("")


def maybe_override_siriusxm_preset_press(store: Store, device_id: str, body: str) -> None:
    return


def is_siriusxm_display_experiment(preset: Json) -> bool:
    raw = str(preset.get("raw_content_item", ""))
    return "/experiments/siriusxm/display/playback/station/" in raw


def pressed_preset_slot(body: str) -> int:
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return 0
    payload = data.get("payload") if isinstance(data, dict) else {}
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        events = data.get("events") if isinstance(data, dict) else []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or event.get("eventType") or "")
        event_data = event.get("data") if isinstance(event.get("data"), dict) else event
        button_id = str(event_data.get("buttonId") or event_data.get("button") or "")
        match = re.fullmatch(r"PRESET_([1-6])", button_id)
        if event_type == "preset-pressed" and match:
            return int(match.group(1))
    return 0


def select_siriusxm_preset_content(ip: str, raw_content_item: str, device_id: str, slot: int) -> None:
    try:
        time.sleep(0.25)
        select_content_item(ip, raw_content_item)
        print(f"[preset-override] {device_id} slot={slot} sent /select")
    except Exception as exc:
        print(f"[preset-override] {device_id} slot={slot} failed: {type(exc).__name__}: {exc}")


def summarize_scmudc(body: str) -> str:
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return f"non-json body bytes={len(body)}"
    payload = data.get("payload") if isinstance(data, dict) else {}
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        events = data.get("events") if isinstance(data, dict) else []
    parts: list[str] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or event.get("eventType") or "event")
        event_data = event.get("data") if isinstance(event.get("data"), dict) else event
        now_playing = event_data.get("nowPlaying") if isinstance(event_data.get("nowPlaying"), dict) else {}
        np_content = now_playing.get("contentItem") if isinstance(now_playing.get("contentItem"), dict) else {}
        source = (
            event_data.get("source")
            or event_data.get("sourceName")
            or event_data.get("sourceType")
            or now_playing.get("source")
            or np_content.get("source")
            or ""
        )
        state = (
            event_data.get("state")
            or event_data.get("playStatus")
            or event_data.get("status")
            or event_data.get("system-state")
            or now_playing.get("playStatus")
            or ""
        )
        error = event_data.get("error") or event_data.get("errorCode") or event_data.get("reason") or ""
        item = (
            event_data.get("itemName")
            or event_data.get("name")
            or event_data.get("trackName")
            or textish(np_content.get("itemName"))
            or textish(now_playing.get("track"))
            or textish(now_playing.get("stationName"))
            or ""
        )
        fields = [event_type]
        for value in (source, state, error, item):
            if value:
                fields.append(str(value))
        parts.append(":".join(fields))
    if parts:
        return " | ".join(parts[:8])
    keys = sorted(data.keys()) if isinstance(data, dict) else []
    return f"json keys={','.join(keys)} bytes={len(body)}"


def textish(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def handle_account_full(req: SoundTouchBridgeHandler, account_id: str) -> None:
    body = account_full(req.server.store, account_id)
    capture_cloud_response(req, account_id, body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/xml")


def handle_sources(req: SoundTouchBridgeHandler, account_id: str) -> None:
    body = f'<?xml version="1.0" standalone="yes"?>{sources_xml(req.server.store, account_id)}'
    capture_cloud_response(req, account_id, body)
    req.send_text(body, content_type="application/xml")


def capture_cloud_response(req: SoundTouchBridgeHandler, account_id: str, body: str) -> None:
    path = urlparse(req.path).path
    req.server.store.add_cloud_response(account_id, path, req.client_address[0], truncate_diagnostic_body(body))
    print(f"[cloud-response] account={account_id} path={path} client={req.client_address[0]} bytes={len(body)}")


def redact_cloud_response(body: str) -> str:
    body = re.sub(r'(sourceAccount=")[^"]*(")', r"\1[redacted]\2", body)
    body = re.sub(r"(<username>)[a-fA-F0-9]{16,}(</username>)", r"\1[redacted]\2", body)
    body = re.sub(r'("(?:streamUrl|url)"\s*:\s*")[^"]*(")', r"\1[redacted]\2", body)
    return body


def handle_account_presets(req: SoundTouchBridgeHandler, account_id: str) -> None:
    body = account_presets(req.server.store, account_id)
    if not body:
        req.send_text("", 404)
        return
    req.send_bytes(body, content_type="application/xml")


def handle_device_presets(req: SoundTouchBridgeHandler, account_id: str, device_id: str) -> None:
    body = device_presets(req.server.store, device_id)
    if not body:
        req.send_text("", 404)
        return
    req.send_bytes(body, content_type="application/xml")


def handle_device_add(req: SoundTouchBridgeHandler, account_id: str) -> None:
    body = req.read_text()
    match = re.search(r'deviceid="([^"]+)"', body)
    device_id = match.group(1) if match else ""
    response = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<device deviceid="{device_id}"><createdOn>2020-01-01T00:00:00.000+00:00</createdOn>'
        "<ipaddress></ipaddress><name></name>"
        "<updatedOn>2020-01-01T00:00:00.000+00:00</updatedOn></device>"
    ).encode("utf-8")
    req.send_response(201)
    req.send_header("Content-Type", "application/vnd.bose.streaming-v1.2+xml")
    req.send_header("Content-Length", str(len(response)))
    req.send_header("Credentials", "Bearer soundtouch-bridge-token")
    if device_id:
        req.send_header(
            "Location",
            f"{req.server.public_base}/streaming/account/{account_id}/device/{device_id}",
        )
    req.send_header("METHOD_NAME", "addDevice")
    req.end_headers()
    req.wfile.write(response)


def handle_source_add(req: SoundTouchBridgeHandler, account_id: str) -> None:
    body = req.read_text()
    username = _tag(body, "username")
    source_name = _tag(body, "sourcename") or "Stored Music"
    digest = hashlib.sha1(username.encode("utf-8")).hexdigest()
    source_id = 25000000 + (int(digest[:8], 16) % 1000000)
    username_xml = escape(username)
    source_name_xml = escape(source_name)
    response = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<source id="{source_id}" type="Audio">'
        "<createdOn>2020-01-01T00:00:00.000+00:00</createdOn>"
        '<credential type="token"></credential>'
        f"<name>{username_xml}</name><sourceproviderid>7</sourceproviderid>"
        f"<sourcename>{source_name_xml}</sourcename><sourceSettings/>"
        "<updatedOn>2020-01-01T00:00:00.000+00:00</updatedOn>"
        f"<username>{username_xml}</username></source>"
    ).encode("utf-8")
    req.send_response(201)
    req.send_header("Content-Type", "application/vnd.bose.streaming-v1.2+xml")
    req.send_header("Content-Length", str(len(response)))
    req.send_header("METHOD_NAME", "addSource")
    req.send_header("ETag", f'"{account_id}-{source_id}"')
    req.end_headers()
    req.wfile.write(response)


def _tag(xml: str, name: str) -> str:
    match = re.search(rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>", xml, re.S | re.I)
    return match.group(1).strip() if match else ""


def handle_updates(req: SoundTouchBridgeHandler) -> None:
    req.send_text('<?xml version="1.0" encoding="UTF-8"?><updates/>', content_type="application/xml")


def report_response() -> Json:
    return {"nextReportIn": 1800}


def handle_report(req: SoundTouchBridgeHandler, **_: str) -> None:
    drain_request_body(req)
    req.send_json(report_response())


def handle_empty(req: SoundTouchBridgeHandler, **_: str) -> None:
    drain_request_body(req)
    req.send_text("")


PLAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SoundTouch Bridge Play To Speaker</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1f2933;
      --muted: #5e6b76;
      --line: #d7dde4;
      --panel: #ffffff;
      --bg: #eef2f6;
      --action: #126b5c;
      --accent: #244b7a;
      --bad: #a83232;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 750; }
    a { color: var(--accent); font-weight: 700; text-decoration: none; }
    main {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 20px;
      max-width: 1200px;
      margin: 0 auto;
      padding: 20px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 16px; }
    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
    }
    button {
      border: 1px solid var(--action);
      border-radius: 6px;
      padding: 8px 11px;
      background: var(--action);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
    }
    button.secondary {
      background: #fff;
      color: var(--action);
    }
    .page-note {
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 12px 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
      text-transform: none;
    }
    .check-row input {
      width: auto;
      min-height: 0;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 12px 0;
      align-items: center;
    }
    .source-tabs button.active {
      background: var(--accent);
      border-color: var(--accent);
    }
    .station-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
      gap: 12px;
      align-items: stretch;
    }
    .station-card {
      display: grid;
      grid-template-rows: 170px auto auto;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
      min-width: 0;
    }
    .station-art {
      width: 100%;
      height: 170px;
      display: grid;
      place-items: center;
      border-radius: 6px;
      padding: 20px;
      background: #eef2f6;
      overflow: hidden;
    }
    .station-art img {
      width: 82%;
      height: 82%;
      object-fit: contain;
      display: block;
    }
    .station-title {
      min-height: 44px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .meta, .status { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .status.error { color: var(--bad); }
    .status.ok { color: var(--action); }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      header { align-items: flex-start; flex-direction: column; }
      .station-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <h1>SoundTouch Bridge</h1>
    <nav><a href="/admin">Presets</a></nav>
  </header>
  <main>
    <section>
      <h2>Play To Speaker</h2>
      <p class="page-note">Choose a source and send a station to the selected speaker.</p>
      <label>Speaker
        <select id="speakerSelect"></select>
      </label>
      <label class="check-row">
        <input type="checkbox" id="wakeSpeaker">
        Wake speaker if it is in standby
      </label>
      <div class="toolbar source-tabs" id="sourceTabs">
        <button data-source="SIRIUSXM" class="active">SiriusXM</button>
        <button data-source="TUNEIN" class="secondary">TuneIn</button>
        <button data-source="IHEART" class="secondary">iHeart</button>
      </div>
      <label id="searchLabel">Search
        <input id="stationSearch" placeholder="Search stations">
      </label>
      <div class="toolbar">
        <button id="searchStations">Search</button>
        <button class="secondary" id="loadSirius">Load SiriusXM</button>
      </div>
      <div class="status" id="status"></div>
    </section>
    <section>
      <h2 id="listTitle">SiriusXM Channels</h2>
      <div class="station-grid" id="stationGrid"></div>
    </section>
  </main>
  <script>
    const state = { speakers: [], source: 'SIRIUSXM', stations: { SIRIUSXM: [], TUNEIN: [], IHEART: [] } };
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      });
      const text = await res.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
      if (!res.ok) throw new Error(data.message || data.error || `${res.status} ${res.statusText}`);
      return data;
    }

    function setStatus(text, kind = '') {
      const el = $('status');
      el.textContent = text;
      el.className = `status ${kind}`.trim();
    }

    async function loadSpeakers() {
      const data = await api('/api/speakers');
      state.speakers = data.speakers || [];
      const select = $('speakerSelect');
      select.innerHTML = state.speakers.map((speaker) => {
        const label = `${speaker.name || speaker.device_id} - ${speaker.ip}`;
        return `<option value="${escapeAttr(speaker.device_id)}">${escapeHtml(label)}</option>`;
      }).join('');
      if (!state.speakers.length) {
        select.innerHTML = '<option value="">No speakers registered</option>';
      }
    }

    function setSource(source) {
      state.source = source;
      document.querySelectorAll('#sourceTabs button').forEach((button) => {
        const active = button.dataset.source === source;
        button.className = active ? 'active' : 'secondary';
      });
      $('loadSirius').style.display = source === 'SIRIUSXM' ? 'inline-flex' : 'none';
      $('listTitle').textContent =
        source === 'SIRIUSXM' ? 'SiriusXM Channels' :
        source === 'TUNEIN' ? 'TuneIn Stations' :
        'iHeart Stations';
      renderStations();
    }

    async function loadSirius() {
      const data = await api('/api/siriusxm/catalog');
      state.stations.SIRIUSXM = (data.channels || []).map((channel) => ({
        source: 'SIRIUSXM',
        station_id: channel.station_id,
        name: channel.name || channel.station_id,
        description: channel.number ? `Channel ${channel.number}` : '',
        image_url: channel.image_url || '',
        entity_url: channel.entity_url || '',
      }));
      renderStations();
      setStatus(`Loaded ${state.stations.SIRIUSXM.length} SiriusXM channel(s)`, 'ok');
    }

    async function searchStations() {
      const query = $('stationSearch').value.trim();
      if (!query) {
        setStatus('Enter a search term', 'error');
        return;
      }
      if (state.source === 'SIRIUSXM') {
        await loadSirius();
        renderStations();
        return;
      }
      const path = state.source === 'TUNEIN'
        ? `/api/tunein/search?q=${encodeURIComponent(query)}`
        : `/api/iheart/search?q=${encodeURIComponent(query)}`;
      const data = await api(path);
      state.stations[state.source] = (data.stations || []).map((station) => ({ ...station, source: state.source }));
      renderStations();
      setStatus(`Loaded ${state.stations[state.source].length} station(s)`, 'ok');
    }

    function visibleStations() {
      const query = $('stationSearch').value.trim().toLowerCase();
      return (state.stations[state.source] || []).filter((station) => {
        const haystack = `${station.name || ''} ${station.description || ''} ${station.station_id || ''}`.toLowerCase();
        return !query || haystack.includes(query);
      });
    }

    function renderStations() {
      const grid = $('stationGrid');
      const stations = visibleStations();
      if (!stations.length) {
        grid.innerHTML = '<div class="meta">No stations loaded.</div>';
        return;
      }
      grid.innerHTML = stations.map((station, index) => {
        const image = station.image_url
          ? `<img src="${escapeAttr(station.image_url)}" alt="">`
          : `<span>${escapeHtml((station.name || '?').slice(0, 2).toUpperCase())}</span>`;
        return `
          <article class="station-card">
            <div class="station-art">${image}</div>
            <div>
              <div class="station-title">${escapeHtml(station.name || station.station_id)}</div>
              <div class="meta">${escapeHtml(station.description || station.station_id || '')}</div>
            </div>
            <button data-action="push" data-index="${index}">Try Select</button>
          </article>`;
      }).join('');
      grid.querySelectorAll('[data-action="push"]').forEach((button) => {
        button.onclick = () => pushStation(stations[Number(button.dataset.index)]);
      });
    }

    async function pushStation(station) {
      const deviceId = $('speakerSelect').value;
      if (!deviceId) {
        setStatus('Select a speaker', 'error');
        return;
      }
      const payload = {
        source: station.source,
        station_id: station.station_id || '',
        name: station.name || '',
        image_url: station.image_url || '',
        entity_url: station.entity_url || '',
        stream_url: station.stream_url || '',
        wake: $('wakeSpeaker').checked,
      };
      const data = await api(`/api/speakers/${encodeURIComponent(deviceId)}/play`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setStatus(data.play && data.play.ok ? `Tried ${payload.name} on speaker` : data.play.message, data.play && data.play.ok ? 'ok' : 'error');
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, '&#96;');
    }

    document.querySelectorAll('#sourceTabs button').forEach((button) => {
      button.onclick = () => setSource(button.dataset.source);
    });
    $('stationSearch').oninput = renderStations;
    $('stationSearch').onkeydown = (event) => {
      if (event.key === 'Enter') searchStations().catch((err) => setStatus(err.message, 'error'));
    };
    $('searchStations').onclick = () => searchStations().catch((err) => setStatus(err.message, 'error'));
    $('loadSirius').onclick = () => loadSirius().catch((err) => setStatus(err.message, 'error'));
    loadSpeakers().then(() => setSource('SIRIUSXM')).catch((err) => setStatus(err.message, 'error'));
  </script>
</body>
</html>
"""


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SoundTouch Bridge</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1f2933;
      --muted: #5e6b76;
      --line: #d7dde4;
      --panel: #ffffff;
      --bg: #eef2f6;
      --action: #126b5c;
      --warn: #9f4b14;
      --bad: #a83232;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    h2 { margin: 0 0 10px; font-size: 16px; }
    main {
      display: grid;
      grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
      gap: 20px;
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    .speaker-list {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .speaker-button {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fff;
      color: var(--ink);
      text-align: left;
      cursor: pointer;
    }
    .speaker-button.active {
      border-color: var(--action);
      outline: 2px solid rgba(18, 107, 92, .16);
    }
    .meta { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin: 12px 0;
    }
    button {
      border: 1px solid var(--action);
      border-radius: 6px;
      padding: 8px 11px;
      background: var(--action);
      color: #fff;
      cursor: pointer;
      font-weight: 650;
    }
    button.secondary {
      background: #fff;
      color: var(--action);
    }
    button.danger {
      border-color: var(--bad);
      background: #fff;
      color: var(--bad);
    }
    input, select {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--ink);
    }
    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .add-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: end;
    }
    .preset-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(280px, 1fr));
      gap: 12px;
    }
    .preset-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 10px;
      background: #fff;
    }
    .preset-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .slot {
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: #243b53;
      color: #fff;
      font-weight: 800;
      flex: 0 0 auto;
    }
    .fields {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .wide { grid-column: 1 / -1; }
    .status {
      min-height: 22px;
      color: var(--muted);
      font-size: 13px;
    }
    .status.error { color: var(--bad); }
    .status.ok { color: var(--action); }
    .status.warn { color: var(--warn); }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      header { align-items: flex-start; flex-direction: column; }
      .preset-grid { grid-template-columns: 1fr; }
      .fields { grid-template-columns: 1fr; }
      .add-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>SoundTouch Bridge</h1>
    <div class="meta"><a href="/play">Play</a> <span id="serverMeta"></span></div>
  </header>
  <main>
    <section>
      <h2>Speakers</h2>
      <div class="add-row">
        <label>Speaker IP
          <input id="speakerIp" inputmode="decimal" placeholder="192.168.1.50">
        </label>
        <button id="addSpeaker">Add</button>
      </div>
      <div class="toolbar">
        <button class="secondary" id="refreshSpeakers">Refresh</button>
      </div>
      <div class="speaker-list" id="speakerList"></div>
      <hr>
      <h2>SiriusXM</h2>
      <div class="meta" id="siriusStatus">Not checked</div>
      <div class="toolbar">
        <button class="secondary" id="refreshSiriusStatus">Status</button>
        <button class="secondary" id="loginSirius">Login</button>
        <button class="secondary" id="loadSiriusCatalog">Load Channels</button>
      </div>
      <label class="wide">Channel Search
        <input id="siriusChannelSearch" placeholder="Search SiriusXM channels">
      </label>
      <div class="speaker-list" id="siriusChannelList"></div>
      <hr>
      <h2>TuneIn</h2>
      <div class="toolbar">
        <button class="secondary" id="searchTuneInStations">Search Stations</button>
      </div>
      <label class="wide">Station Search
        <input id="tuneinStationSearch" placeholder="Search TuneIn stations">
      </label>
      <div class="speaker-list" id="tuneinStationList"></div>
      <hr>
      <h2>iHeart</h2>
      <div class="toolbar">
        <button class="secondary" id="searchIHeartStations">Search Stations</button>
      </div>
      <label class="wide">Station Search
        <input id="iheartStationSearch" placeholder="Search iHeart stations">
      </label>
      <div class="speaker-list" id="iheartStationList"></div>
    </section>
    <section>
      <h2 id="selectedTitle">Presets</h2>
      <div class="toolbar">
        <button class="secondary" id="importPresets">Import From Speaker</button>
        <button class="secondary" id="reloadPresets">Reload</button>
        <button class="secondary" id="migrateSpeaker">Migrate</button>
      </div>
      <div class="status" id="status"></div>
      <div class="preset-grid" id="presetGrid"></div>
    </section>
  </main>
  <script>
    const state = { speakers: [], selected: null, presets: [], sirius: null, siriusChannels: [], tuneinStations: [], iheartStations: [], cardNotices: {} };
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      });
      const text = await res.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
      if (!res.ok) throw new Error(data.message || data.error || `${res.status} ${res.statusText}`);
      return data;
    }

    function setStatus(text, kind = '') {
      const el = $('status');
      el.textContent = text;
      el.className = `status ${kind}`.trim();
    }

    function cardNoticeKey(deviceId, slot) {
      return `${deviceId}:${slot}`;
    }

    function setCardNotice(deviceId, slot, text, kind = '') {
      state.cardNotices[cardNoticeKey(deviceId, slot)] = { text, kind };
    }

    function clearCardNotice(deviceId, slot) {
      delete state.cardNotices[cardNoticeKey(deviceId, slot)];
    }

    function applyCardNotice(card) {
      const speaker = currentSpeaker();
      if (!speaker) return false;
      const notice = state.cardNotices[cardNoticeKey(speaker.device_id, Number(card.dataset.slot))];
      if (!notice) return false;
      const status = card.querySelector('[data-role="card-status"]');
      status.textContent = notice.text;
      status.className = `status ${notice.kind}`.trim();
      return true;
    }

    async function loadSpeakers() {
      const data = await api('/api/speakers');
      state.speakers = data.speakers || [];
      if (!state.selected && state.speakers.length) state.selected = state.speakers[0].device_id;
      renderSpeakers();
      if (state.selected) await loadPresets();
      await loadSiriusStatus();
    }

    async function loadSiriusStatus() {
      const data = await api('/api/siriusxm/session');
      state.sirius = data.session || {};
      renderSiriusStatus();
    }

    function renderSiriusStatus() {
      const session = state.sirius || {};
      const username = session.username ? ` - ${session.username}` : '';
      const error = session.last_error ? ` - ${session.last_error}` : '';
      const el = $('siriusStatus');
      if (!session.configured) {
        el.textContent = 'Missing /etc/soundtouch-bridge/siriusxm.env';
        el.className = 'meta status warn';
      } else if (session.session_authenticated) {
        el.textContent = `Ready${username}${error}`;
        el.className = 'meta status ok';
      } else {
        el.textContent = `Configured; playback will login automatically${username}${error}`;
        el.className = 'meta status';
      }
    }

    function renderSpeakers() {
      const list = $('speakerList');
      list.innerHTML = '';
      if (!state.speakers.length) {
        list.innerHTML = '<div class="meta">No speakers registered.</div>';
        $('selectedTitle').textContent = 'Presets';
        $('presetGrid').innerHTML = '';
        return;
      }
      for (const sp of state.speakers) {
        const btn = document.createElement('button');
        btn.className = `speaker-button ${sp.device_id === state.selected ? 'active' : ''}`.trim();
        btn.innerHTML = `<strong>${escapeHtml(sp.name || sp.device_id)}</strong><div class="meta">${escapeHtml(sp.ip)} - ${escapeHtml(sp.device_id)}</div>`;
        btn.onclick = async () => {
          state.selected = sp.device_id;
          renderSpeakers();
          await loadPresets();
        };
        list.appendChild(btn);
      }
    }

    async function loadPresets() {
      const speaker = currentSpeaker();
      if (!speaker) return;
      $('selectedTitle').textContent = `${speaker.name || speaker.device_id} Presets`;
      const data = await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/presets`);
      state.presets = data.presets || [];
      renderPresets();
    }

    function renderPresets() {
      const grid = $('presetGrid');
      grid.innerHTML = '';
      for (const preset of state.presets) {
        const card = document.createElement('div');
        card.className = 'preset-card';
        card.dataset.slot = preset.slot;
        card.innerHTML = `
          <div class="preset-head">
            <div class="slot">${preset.slot}</div>
            <div class="meta">${escapeHtml(preset.source || 'EMPTY')}</div>
          </div>
          <div class="fields">
            <label>Source
              <select data-field="source">
                <option value="TUNEIN">TuneIn</option>
                <option value="LOCAL_INTERNET_RADIO">Direct Stream</option>
                <option value="SIRIUSXM">SiriusXM</option>
                <option value="OPAQUE">Preserved</option>
              </select>
            </label>
            <label>Name
              <input data-field="name" value="${escapeAttr(preset.name || '')}">
            </label>
            <label class="wide tunein">TuneIn Station ID
              <input data-field="station_id" value="${escapeAttr(preset.station_id || '')}">
            </label>
            <label class="wide tunein tunein-picker-row">TuneIn Station
              <select data-role="tunein-picker"></select>
            </label>
            <label class="wide sirius">SiriusXM Channel
              <select data-role="sirius-picker"></select>
            </label>
            <label class="wide stream">Stream URL
              <input data-field="stream_url" value="${escapeAttr(preset.stream_url || '')}">
            </label>
            <label class="wide stream iheart-picker-row">iHeart Station
              <select data-role="iheart-picker"></select>
            </label>
            <label class="wide">Image URL
              <input data-field="image_url" value="${escapeAttr(preset.image_url || '')}">
            </label>
            <input data-field="entity_url" type="hidden" value="">
            <textarea data-field="raw_content_item" hidden>${escapeHtml(preset.raw_content_item || '')}</textarea>
          </div>
          <div class="toolbar">
            <button data-action="save">Save</button>
            <button class="secondary" data-action="pick-tunein">Use Station</button>
            <button class="secondary" data-action="pick-iheart">Use iHeart</button>
            <button class="secondary" data-action="pick-sirius">Use Channel</button>
            <button class="secondary" data-action="refresh-sirius">Refresh SiriusXM</button>
            <button class="danger" data-action="clear">Clear</button>
          </div>
          <div class="toolbar copy-row">
            <label>Copy source slot
              <select data-role="copy-source"></select>
            </label>
            <button class="secondary" data-action="copy">Copy Here</button>
          </div>
          <div class="status" data-role="card-status"></div>
        `;
        grid.appendChild(card);
        card.querySelector('[data-field="source"]').value =
          preset.source === 'LOCAL_INTERNET_RADIO' ? 'LOCAL_INTERNET_RADIO' :
          preset.source === 'SIRIUSXM' ? 'SIRIUSXM' :
          preset.source === 'OPAQUE' ? 'OPAQUE' : 'TUNEIN';
        populateCopyOptions(card);
        populateTuneInPicker(card);
        populateIHeartPicker(card);
        populateSiriusPicker(card);
        syncSourceFields(card);
        applyCardNotice(card);
        card.querySelector('[data-field="source"]').onchange = () => {
          const speaker = currentSpeaker();
          if (speaker) clearCardNotice(speaker.device_id, Number(card.dataset.slot));
          syncSourceFields(card);
        };
        card.querySelector('[data-action="save"]').onclick = () => savePreset(card);
        card.querySelector('[data-action="pick-tunein"]').onclick = () => pickTuneInStation(card);
        card.querySelector('[data-action="pick-iheart"]').onclick = () => pickIHeartStation(card);
        card.querySelector('[data-action="pick-sirius"]').onclick = () => pickSiriusChannel(card);
        card.querySelector('[data-action="refresh-sirius"]').onclick = () => refreshSiriusPreset(card);
        card.querySelector('[data-action="clear"]').onclick = () => clearPreset(card);
        card.querySelector('[data-action="copy"]').onclick = () => copyPreset(card);
      }
    }

    function syncSourceFields(card) {
      const source = card.querySelector('[data-field="source"]').value;
      const raw = card.querySelector('[data-field="raw_content_item"]').value;
      card.querySelectorAll('.tunein').forEach((el) => { el.style.display = source === 'TUNEIN' ? 'grid' : 'none'; });
      card.querySelector('.sirius').style.display = source === 'SIRIUSXM' ? 'grid' : 'none';
      card.querySelector('.stream').style.display = source === 'LOCAL_INTERNET_RADIO' ? 'grid' : 'none';
      card.querySelector('[data-action="pick-tunein"]').style.display = source === 'TUNEIN' ? 'inline-flex' : 'none';
      card.querySelector('[data-action="pick-iheart"]').style.display = source === 'LOCAL_INTERNET_RADIO' ? 'inline-flex' : 'none';
      card.querySelector('[data-action="pick-sirius"]').style.display = source === 'SIRIUSXM' ? 'inline-flex' : 'none';
      card.querySelector('[data-action="refresh-sirius"]').style.display = source === 'SIRIUSXM' ? 'inline-flex' : 'none';
      const msg = card.querySelector('[data-role="card-status"]');
      if (applyCardNotice(card)) return;
      if (source === 'OPAQUE' && raw.includes('source="IHEART"')) {
        msg.textContent = 'Preserved iHeart preset.';
        msg.className = 'status ok';
      } else if (source === 'OPAQUE') {
        msg.textContent = 'Preserved imported preset. Copy or leave unchanged unless you know this service still works.';
        msg.className = 'status warn';
      } else if (source === 'SIRIUSXM' && raw.includes('/experiments/siriusxm/display/playback/station/')) {
        msg.textContent = 'SiriusXM display metadata experiment active.';
        msg.className = 'status warn';
      } else if (source === 'SIRIUSXM' && raw.includes('SIRIUSXM_EVEREST')) {
        msg.textContent = 'SiriusXM preset stored.';
        msg.className = 'status ok';
      } else if (source === 'SIRIUSXM') {
        msg.textContent = state.siriusChannels.length
          ? 'Choose a channel, use it, then save the preset.'
          : 'Load SiriusXM channels, then choose one and save the preset.';
        msg.className = 'status';
      } else if (source === 'TUNEIN') {
        msg.textContent = state.tuneinStations.length
          ? 'Choose a station, use it, then save the preset.'
          : 'Search TuneIn stations, then choose one and save the preset.';
        msg.className = 'status';
      } else if (source === 'LOCAL_INTERNET_RADIO') {
        msg.textContent = state.iheartStations.length
          ? 'Choose an iHeart station, use it, then save the direct stream preset.'
          : 'Search iHeart stations or enter a direct stream URL.';
        msg.className = 'status';
      } else {
        msg.textContent = '';
        msg.className = 'status';
      }
    }

    async function loadSiriusCatalog() {
      const data = await api('/api/siriusxm/catalog');
      state.siriusChannels = (data.channels || []).sort(compareSiriusChannels);
      if (data.session) state.sirius = data.session;
      renderSiriusStatus();
      renderSiriusCatalog();
      document.querySelectorAll('.preset-card').forEach((card) => {
        populateSiriusPicker(card);
        syncSourceFields(card);
      });
      setStatus(`Loaded ${state.siriusChannels.length} SiriusXM channel(s)`, 'ok');
    }

    function compareSiriusChannels(a, b) {
      const an = Number(a.number || 0);
      const bn = Number(b.number || 0);
      if (an && bn && an !== bn) return an - bn;
      return String(a.name || a.station_id).localeCompare(String(b.name || b.station_id));
    }

    function renderSiriusCatalog() {
      const list = $('siriusChannelList');
      const query = $('siriusChannelSearch').value.trim().toLowerCase();
      const channels = state.siriusChannels.filter((channel) => {
        const haystack = `${channel.number || ''} ${channel.name || ''} ${channel.station_id || ''}`.toLowerCase();
        return !query || haystack.includes(query);
      }).slice(0, 60);
      if (!state.siriusChannels.length) {
        list.innerHTML = '<div class="meta">Load channels to browse SiriusXM.</div>';
        return;
      }
      if (!channels.length) {
        list.innerHTML = '<div class="meta">No matching channels.</div>';
        return;
      }
      list.innerHTML = channels.map((channel) => {
        const number = channel.number ? `${escapeHtml(channel.number)} - ` : '';
        return `<div class="speaker-button"><strong>${number}${escapeHtml(channel.name || channel.station_id)}</strong><div class="meta">${escapeHtml(channel.station_id || '')}</div></div>`;
      }).join('');
    }

    async function searchTuneInStations() {
      const query = $('tuneinStationSearch').value.trim();
      if (!query) {
        setStatus('Enter a TuneIn station search first', 'warn');
        return;
      }
      const data = await api(`/api/tunein/search?q=${encodeURIComponent(query)}`);
      state.tuneinStations = data.stations || [];
      renderTuneInStations();
      document.querySelectorAll('.preset-card').forEach((card) => {
        populateTuneInPicker(card);
        syncSourceFields(card);
      });
      setStatus(`Loaded ${state.tuneinStations.length} TuneIn station(s)`, 'ok');
    }

    async function searchIHeartStations() {
      const query = $('iheartStationSearch').value.trim();
      if (!query) {
        setStatus('Enter an iHeart station search first', 'warn');
        return;
      }
      const data = await api(`/api/iheart/search?q=${encodeURIComponent(query)}`);
      state.iheartStations = data.stations || [];
      renderIHeartStations();
      document.querySelectorAll('.preset-card').forEach((card) => {
        populateIHeartPicker(card);
        syncSourceFields(card);
      });
      setStatus(`Loaded ${state.iheartStations.length} iHeart station(s)`, 'ok');
    }

    function renderTuneInStations() {
      const list = $('tuneinStationList');
      const stations = state.tuneinStations.slice(0, 60);
      if (!stations.length) {
        list.innerHTML = '<div class="meta">Search to browse TuneIn stations.</div>';
        return;
      }
      list.innerHTML = stations.map((station) => {
        const detail = station.description ? `${station.station_id || ''} - ${station.description}` : station.station_id || '';
        return `<div class="speaker-button"><strong>${escapeHtml(station.name || station.station_id)}</strong><div class="meta">${escapeHtml(detail)}</div></div>`;
      }).join('');
    }

    function renderIHeartStations() {
      const list = $('iheartStationList');
      const stations = state.iheartStations.slice(0, 60);
      if (!stations.length) {
        list.innerHTML = '<div class="meta">Search to browse iHeart stations.</div>';
        return;
      }
      list.innerHTML = stations.map((station) => {
        const detail = station.description ? `${station.station_id || ''} - ${station.description}` : station.station_id || '';
        return `<div class="speaker-button"><strong>${escapeHtml(station.name || station.station_id)}</strong><div class="meta">${escapeHtml(detail)}</div></div>`;
      }).join('');
    }

    function populateTuneInPicker(card) {
      const picker = card.querySelector('[data-role="tunein-picker"]');
      if (!picker) return;
      const current = card.querySelector('[data-field="station_id"]').value.trim();
      picker.innerHTML = '<option value="">Select station</option>';
      for (const station of state.tuneinStations) {
        const option = document.createElement('option');
        option.value = station.station_id || '';
        option.textContent = station.description
          ? `${station.name || station.station_id} - ${station.description}`
          : `${station.name || station.station_id}`;
        option.dataset.name = station.name || '';
        option.dataset.imageUrl = station.image_url || '';
        picker.appendChild(option);
      }
      if (current && [...picker.options].some((option) => option.value === current)) {
        picker.value = current;
      }
    }

    function populateIHeartPicker(card) {
      const picker = card.querySelector('[data-role="iheart-picker"]');
      if (!picker) return;
      picker.innerHTML = '<option value="">Select station</option>';
      for (const station of state.iheartStations) {
        const option = document.createElement('option');
        option.value = station.station_id || '';
        option.textContent = station.description
          ? `${station.name || station.station_id} - ${station.description}`
          : `${station.name || station.station_id}`;
        option.dataset.name = station.name || '';
        option.dataset.imageUrl = station.image_url || '';
        picker.appendChild(option);
      }
    }

    function populateSiriusPicker(card) {
      const picker = card.querySelector('[data-role="sirius-picker"]');
      if (!picker) return;
      const current = card.querySelector('[data-field="station_id"]').value.trim();
      picker.innerHTML = '<option value="">Select channel</option>';
      for (const channel of state.siriusChannels) {
        const option = document.createElement('option');
        option.value = channel.station_id || '';
        option.textContent = `${channel.number ? `${channel.number} - ` : ''}${channel.name || channel.station_id}`;
        option.dataset.name = channel.name || '';
        option.dataset.entityUrl = channel.entity_url || '';
        option.dataset.imageUrl = channel.image_url || '';
        picker.appendChild(option);
      }
      if (current && [...picker.options].some((option) => option.value === current)) {
        picker.value = current;
      }
    }

    function pickSiriusChannel(card) {
      const picker = card.querySelector('[data-role="sirius-picker"]');
      const option = picker.selectedOptions[0];
      const status = card.querySelector('[data-role="card-status"]');
      if (!option || !option.value) {
        status.textContent = 'Select a SiriusXM channel first';
        status.className = 'status error';
        return;
      }
      card.querySelector('[data-field="source"]').value = 'SIRIUSXM';
      card.querySelector('[data-field="station_id"]').value = option.value;
      card.querySelector('[data-field="name"]').value = option.dataset.name || option.textContent;
      card.querySelector('[data-field="image_url"]').value = option.dataset.imageUrl || '';
      card.querySelector('[data-field="entity_url"]').value = option.dataset.entityUrl || '';
      card.querySelector('[data-field="stream_url"]').value = '';
      card.querySelector('[data-field="raw_content_item"]').value = '';
      syncSourceFields(card);
      status.textContent = 'Channel ready; save this preset to assign it.';
      status.className = 'status ok';
    }

    function pickTuneInStation(card) {
      const picker = card.querySelector('[data-role="tunein-picker"]');
      const option = picker.selectedOptions[0];
      const status = card.querySelector('[data-role="card-status"]');
      if (!option || !option.value) {
        status.textContent = 'Select a TuneIn station first';
        status.className = 'status error';
        return;
      }
      card.querySelector('[data-field="source"]').value = 'TUNEIN';
      card.querySelector('[data-field="station_id"]').value = option.value;
      card.querySelector('[data-field="name"]').value = option.dataset.name || option.textContent;
      card.querySelector('[data-field="image_url"]').value = option.dataset.imageUrl || '';
      card.querySelector('[data-field="entity_url"]').value = '';
      card.querySelector('[data-field="stream_url"]').value = '';
      card.querySelector('[data-field="raw_content_item"]').value = '';
      syncSourceFields(card);
      status.textContent = 'Station ready; save this preset to assign it.';
      status.className = 'status ok';
    }

    async function pickIHeartStation(card) {
      const picker = card.querySelector('[data-role="iheart-picker"]');
      const option = picker.selectedOptions[0];
      const status = card.querySelector('[data-role="card-status"]');
      if (!option || !option.value) {
        status.textContent = 'Select an iHeart station first';
        status.className = 'status error';
        return;
      }
      status.textContent = 'Resolving iHeart stream...';
      status.className = 'status';
      try {
        const params = new URLSearchParams({
          name: option.dataset.name || option.textContent,
          image: option.dataset.imageUrl || '',
        });
        const data = await api(`/api/iheart/stations/${encodeURIComponent(option.value)}/stream?${params.toString()}`);
        card.querySelector('[data-field="source"]').value = 'LOCAL_INTERNET_RADIO';
        card.querySelector('[data-field="station_id"]').value = '';
        card.querySelector('[data-field="name"]').value = option.dataset.name || option.textContent;
        card.querySelector('[data-field="image_url"]').value = option.dataset.imageUrl || '';
        card.querySelector('[data-field="entity_url"]').value = '';
        card.querySelector('[data-field="stream_url"]').value = data.stream_url || '';
        card.querySelector('[data-field="raw_content_item"]').value = '';
        syncSourceFields(card);
        status.textContent = 'iHeart stream ready; save this preset to assign it.';
        status.className = 'status ok';
      } catch (err) {
        status.textContent = err.message;
        status.className = 'status error';
      }
    }

    function populateCopyOptions(card) {
      const select = card.querySelector('[data-role="copy-source"]');
      const target = Number(card.dataset.slot);
      select.innerHTML = '';
      for (const preset of state.presets) {
        if (Number(preset.slot) === target) continue;
        const label = preset.source === 'EMPTY' ? `Slot ${preset.slot} (empty)` : `Slot ${preset.slot}: ${preset.name || preset.source}`;
        const option = document.createElement('option');
        option.value = preset.slot;
        option.textContent = label;
        option.disabled = preset.source === 'EMPTY';
        select.appendChild(option);
      }
    }

    async function savePreset(card) {
      const speaker = currentSpeaker();
      const slot = Number(card.dataset.slot);
      const payload = Object.fromEntries([...card.querySelectorAll('[data-field]')].map((el) => [el.dataset.field, el.value.trim()]));
      const status = card.querySelector('[data-role="card-status"]');
      try {
        status.textContent = 'Saving...';
        status.className = 'status';
        const result = await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/presets/${slot}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        });
        let text = 'Saved';
        let kind = 'ok';
        if (result.speaker_store && result.speaker_store.attempted && result.speaker_store.ok === false) {
          text = `Saved; speaker store failed: ${result.speaker_store.message || 'unknown error'}`;
          kind = 'warn';
        } else if (result.speaker_store && result.speaker_store.ok) {
          text = 'Saved and stored on speaker';
        }
        setCardNotice(speaker.device_id, slot, text, kind);
        setStatus(`Slot ${slot}: ${text}`, kind);
        await loadPresets();
      } catch (err) {
        status.textContent = err.message;
        status.className = 'status error';
      }
    }

    async function clearPreset(card) {
      const speaker = currentSpeaker();
      const slot = Number(card.dataset.slot);
      await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/presets/${slot}`, { method: 'DELETE' });
      setStatus(`Slot ${slot} cleared`, 'ok');
      await loadPresets();
    }

    async function refreshSiriusPreset(card) {
      const slot = Number(card.dataset.slot);
      const preset = state.presets.find((item) => Number(item.slot) === slot) || {};
      const stationId = preset.station_id || card.querySelector('[data-field="station_id"]').value.trim();
      const status = card.querySelector('[data-role="card-status"]');
      if (!stationId) {
        status.textContent = 'Missing SiriusXM station id';
        status.className = 'status error';
        return;
      }
      try {
        await api(`/api/siriusxm/channels/${encodeURIComponent(stationId)}/refresh`, { method: 'POST', body: '{}' });
        status.textContent = 'SiriusXM stream refreshed';
        status.className = 'status ok';
        await loadSiriusStatus();
      } catch (err) {
        status.textContent = err.message;
        status.className = 'status error';
      }
    }

    async function copyPreset(card) {
      const speaker = currentSpeaker();
      const slot = Number(card.dataset.slot);
      const sourceSlot = Number(card.querySelector('[data-role="copy-source"]').value);
      if (!sourceSlot) return;
      await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/presets/${slot}/copy`, {
        method: 'POST',
        body: JSON.stringify({ source_slot: sourceSlot }),
      });
      setStatus(`Copied slot ${sourceSlot} to slot ${slot}`, 'ok');
      await loadPresets();
    }

    function currentSpeaker() {
      return state.speakers.find((sp) => sp.device_id === state.selected);
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, '&#96;');
    }

    $('refreshSpeakers').onclick = () => loadSpeakers().catch((err) => setStatus(err.message, 'error'));
    $('reloadPresets').onclick = () => loadPresets().catch((err) => setStatus(err.message, 'error'));
    $('refreshSiriusStatus').onclick = () => loadSiriusStatus().catch((err) => setStatus(err.message, 'error'));
    $('loadSiriusCatalog').onclick = () => loadSiriusCatalog().catch((err) => setStatus(err.message, 'error'));
    $('siriusChannelSearch').oninput = renderSiriusCatalog;
    $('searchTuneInStations').onclick = () => searchTuneInStations().catch((err) => setStatus(err.message, 'error'));
    $('tuneinStationSearch').onkeydown = (event) => {
      if (event.key === 'Enter') searchTuneInStations().catch((err) => setStatus(err.message, 'error'));
    };
    $('searchIHeartStations').onclick = () => searchIHeartStations().catch((err) => setStatus(err.message, 'error'));
    $('iheartStationSearch').onkeydown = (event) => {
      if (event.key === 'Enter') searchIHeartStations().catch((err) => setStatus(err.message, 'error'));
    };
    $('loginSirius').onclick = async () => {
      try {
        const data = await api('/api/siriusxm/session/login', { method: 'POST', body: '{}' });
        state.sirius = data.session || {};
        renderSiriusStatus();
        setStatus('SiriusXM login succeeded', 'ok');
      } catch (err) {
        setStatus(err.message, 'error');
        await loadSiriusStatus().catch(() => {});
      }
    };
    $('addSpeaker').onclick = async () => {
      const ip = $('speakerIp').value.trim();
      if (!ip) return;
      const data = await api('/api/speakers', { method: 'POST', body: JSON.stringify({ ip }) });
      state.selected = data.speaker.device_id;
      $('speakerIp').value = '';
      setStatus('Speaker added', 'ok');
      await loadSpeakers();
    };
    $('importPresets').onclick = async () => {
      const speaker = currentSpeaker();
      if (!speaker) return;
      const data = await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/import-presets`, { method: 'POST' });
      setStatus(`Imported ${data.imported} preset(s)`, 'ok');
      await loadPresets();
    };
    $('migrateSpeaker').onclick = async () => {
      const speaker = currentSpeaker();
      if (!speaker) return;
      await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/migrate`, { method: 'POST', body: '{}' });
      setStatus('Migration command sent; speaker will reboot', 'ok');
      await loadSpeakers();
    };

    $('serverMeta').textContent = window.location.origin;
    loadSpeakers().catch((err) => setStatus(err.message, 'error'));
  </script>
</body>
</html>
"""


def guess_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SoundTouch Bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(Path.home() / ".local/share/soundtouch-bridge/state.sqlite3"))
    parser.add_argument("--public-base", default="")
    args = parser.parse_args(argv)

    public_base = args.public_base or f"http://{guess_lan_ip()}:{args.port}"
    store = Store(args.db)
    server = SoundTouchBridgeServer((args.host, args.port), store, public_base)
    print(f"soundtouch-bridge listening on {args.host}:{args.port}")
    print(f"speaker cloud base: {public_base}")
    print(f"sqlite state: {args.db}")
    server.serve_forever()


if __name__ == "__main__":
    main()


