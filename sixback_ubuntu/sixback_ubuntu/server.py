from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
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
from .cloud import (
    account_full,
    account_presets,
    bmx_services,
    bmx_services_availability,
    device_presets,
    siriusxm_availability,
    siriusxm_now_playing,
    siriusxm_station,
    siriusxm_token,
    sourceproviders_xml,
    sources_xml,
    tunein_station,
    tunein_token,
)
from .db import Store
from .speaker import import_presets, migrate_speaker, probe_speaker
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


class SixBackHandler(BaseHTTPRequestHandler):
    server: "SixBackServer"

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
                except Exception as exc:
                    self.send_json({"error": type(exc).__name__, "message": str(exc)}, 500)
                return
        self.send_json({"error": "not_found", "path": path}, 404)

    def read_json(self) -> Json:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

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


RouteHandler = Callable[[SixBackHandler], None]


class SixBackServer(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], store: Store, public_base: str):
        super().__init__(addr, SixBackHandler)
        self.store = store
        self.public_base = public_base.rstrip("/")
        self.siriusxm = SiriusXmSession.from_env(os.environ.get("SIXBACK_SIRIUSXM_ENV_FILE", DEFAULT_ENV_FILE))
        self.siriusxm_proxy_urls: dict[str, str] = {}
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., None]]] = []
        self._register_routes()

    def route(self, method: str, pattern: str, handler: Callable[..., None]) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def _register_routes(self) -> None:
        self.route("GET", r"/", handle_root)
        self.route("GET", r"/admin", handle_admin)
        self.route("GET", r"/healthz", handle_healthz)
        self.route("GET", r"/api/speakers", handle_list_speakers)
        self.route("POST", r"/api/speakers", handle_add_speaker)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/import-presets", handle_import_presets)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/migrate", handle_migrate)
        self.route("GET", r"/api/speakers/(?P<device_id>[^/]+)/events", handle_speaker_events)
        self.route("GET", r"/api/accounts/(?P<account_id>[^/]+)/cloud-responses", handle_cloud_responses)
        self.route("GET", r"/api/speakers/(?P<device_id>[^/]+)/presets", handle_get_presets)
        self.route("PUT", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])", handle_put_preset)
        self.route("DELETE", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])", handle_delete_preset)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/presets/(?P<slot>[1-6])/copy", handle_copy_preset)
        self.route("GET", r"/api/siriusxm/channels", handle_siriusxm_channels_list)
        self.route("GET", r"/api/siriusxm/session", handle_siriusxm_session)
        self.route("POST", r"/api/siriusxm/session/login", handle_siriusxm_session_login)
        self.route("GET", r"/api/siriusxm/channels/(?P<station_id>[^/]+)", handle_siriusxm_channel_get)
        self.route("PUT", r"/api/siriusxm/channels/(?P<station_id>[^/]+)", handle_siriusxm_channel_put)
        self.route("POST", r"/api/siriusxm/channels/(?P<station_id>[^/]+)/refresh", handle_siriusxm_channel_refresh)
        self.route("GET", r"/bmx/registry/v1/services", handle_bmx_services)
        self.route("GET", r"/bmx/registry/v1/servicesAvailability", handle_bmx_services_availability)
        self.route("GET", r"/streaming/sourceproviders", handle_sourceproviders)
        self.route("POST", r"/bmx/tunein/v1/token", handle_tunein_token)
        self.route("GET", r"/bmx/tunein/v1/playback/station/(?P<station_id>[^/]+)", handle_tunein_station)
        self.route("POST", r"/bmx/tunein/v1/report", handle_empty)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/token", handle_siriusxm_token)
        self.route("POST", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/token", handle_siriusxm_token)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/availability", handle_siriusxm_availability)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/playback/station/(?P<station_id>[^/]+)", handle_siriusxm_station)
        self.route("GET", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/v1/now-playing/station/(?P<station_id>[^/]+)", handle_siriusxm_now_playing)
        self.route("POST", r"/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/v1/report", handle_empty)
        self.route("GET", r"/siriusxm/proxy/(?P<station_id>[^/]+)/playlist\.m3u8", handle_siriusxm_proxy_playlist)
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


def handle_root(req: SixBackHandler) -> None:
    req.send_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>SixBack Ubuntu</title></head>'
        '<body><h1>SixBack Ubuntu</h1><p>Admin UI: <a href="/admin">/admin</a></p></body></html>',
        content_type="text/html; charset=utf-8",
    )


def handle_admin(req: SixBackHandler) -> None:
    req.send_bytes(ADMIN_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")


def handle_healthz(req: SixBackHandler) -> None:
    req.send_json({"ok": True})


def handle_list_speakers(req: SixBackHandler) -> None:
    req.send_json({"speakers": req.server.store.list_speakers()})


def handle_add_speaker(req: SixBackHandler) -> None:
    body = req.read_json()
    ip = str(body.get("ip", "")).strip()
    if not ip:
        req.send_json({"error": "missing ip"}, 400)
        return
    speaker = probe_speaker(ip)
    req.server.store.upsert_speaker(speaker)
    req.send_json({"speaker": speaker}, HTTPStatus.CREATED)


def handle_import_presets(req: SixBackHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    presets = import_presets(speaker["ip"])
    req.server.store.replace_presets(device_id, presets)
    req.send_json({"device_id": device_id, "imported": len(presets), "presets": presets})


def handle_speaker_events(req: SixBackHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    req.send_json({"device_id": device_id, "events": req.server.store.recent_scmudc_events(device_id)})


def handle_cloud_responses(req: SixBackHandler, account_id: str) -> None:
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


def handle_get_presets(req: SixBackHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    req.send_json({"device_id": device_id, "presets": req.server.store.preset_slots_for_speaker(device_id)})


def handle_put_preset(req: SixBackHandler, device_id: str, slot: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    try:
        preset = normalize_admin_preset(body, int(slot))
    except ValueError as exc:
        req.send_json({"error": "invalid_preset", "message": str(exc)}, 400)
        return
    if preset["source"] == "SIRIUSXM" and not is_preserved_siriusxm(preset):
        req.send_json(
            {
                "error": "unsupported_source",
                "message": "SiriusXM presets need authenticated SiriusXM/Bose adapter support, which this MVP does not implement. Imported opaque SiriusXM presets may be preserved, but new SiriusXM presets cannot be created yet.",
            },
            501,
        )
        return
    saved = req.server.store.set_preset(device_id, {"device_id": device_id, **preset})
    req.send_json({"device_id": device_id, "preset": saved})


def handle_copy_preset(req: SixBackHandler, device_id: str, slot: str) -> None:
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


def handle_delete_preset(req: SixBackHandler, device_id: str, slot: str) -> None:
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
    if source not in {"TUNEIN", "LOCAL_INTERNET_RADIO", "SIRIUSXM"}:
        raise ValueError("source must be TUNEIN, LOCAL_INTERNET_RADIO, or SIRIUSXM")
    if not name:
        raise ValueError("name is required")
    if source == "TUNEIN" and not station_id:
        raise ValueError("station_id is required for TuneIn presets")
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
        "raw_content_item": raw_content_item if source == "SIRIUSXM" else "",
    }


def is_preserved_siriusxm(preset: Json) -> bool:
    raw = str(preset.get("raw_content_item", ""))
    return "SIRIUSXM_EVEREST" in raw and "<ContentItem" in raw


def handle_migrate(req: SixBackHandler, device_id: str) -> None:
    speaker = req.server.store.get_speaker(device_id)
    if not speaker:
        req.send_json({"error": "unknown speaker"}, 404)
        return
    body = req.read_json()
    base_url = str(body.get("base_url") or req.server.public_base).rstrip("/")
    transcript = migrate_speaker(speaker["ip"], base_url)
    req.server.store.set_migrated(device_id, base_url)
    req.send_json({"device_id": device_id, "base_url": base_url, "transcript": transcript})


def handle_siriusxm_channels_list(req: SixBackHandler) -> None:
    req.send_json({"channels": req.server.store.list_siriusxm_channels()})


def handle_siriusxm_session(req: SixBackHandler) -> None:
    req.send_json({"session": req.server.siriusxm.status()})


def handle_siriusxm_session_login(req: SixBackHandler) -> None:
    try:
        req.server.siriusxm.login()
    except Exception as exc:
        message = sanitize_siriusxm_error(str(exc), req.server.siriusxm.credentials)
        req.send_json({"error": "siriusxm_login_failed", "message": message}, 502)
        return
    req.send_json({"session": req.server.siriusxm.status()})


def handle_siriusxm_channel_get(req: SixBackHandler, station_id: str) -> None:
    req.send_json({"channel": req.server.store.get_siriusxm_channel(station_id)})


def handle_siriusxm_channel_put(req: SixBackHandler, station_id: str) -> None:
    body = req.read_json()
    try:
        channel = normalize_siriusxm_channel(body)
    except ValueError as exc:
        req.send_json({"error": "invalid_siriusxm_channel", "message": str(exc)}, 400)
        return
    saved = req.server.store.upsert_siriusxm_channel(station_id, channel)
    req.send_json({"channel": saved})


def handle_siriusxm_channel_refresh(req: SixBackHandler, station_id: str) -> None:
    try:
        stream_url = resolve_siriusxm_stream_url(req.server.store, req.server.siriusxm, station_id)
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


def normalize_siriusxm_channel(body: Json) -> Json:
    name = str(body.get("name", "")).strip()
    entity_url = str(body.get("entity_url", "")).strip()
    stream_url = str(body.get("stream_url", "")).strip()
    if entity_url and not entity_url.startswith("https://www.siriusxm.com/player/"):
        raise ValueError("entity_url should be the SiriusXM web player URL")
    if stream_url:
        if "siriusxm.com/player/" in stream_url:
            raise ValueError("stream_url must be a direct playable audio URL, not the SiriusXM web player URL")
        if not (stream_url.startswith("http://") or stream_url.startswith("https://")):
            raise ValueError("stream_url must start with http:// or https://")
    return {"name": name, "entity_url": entity_url, "stream_url": stream_url}


def resolve_siriusxm_stream_url(store: Store, session: SiriusXmSession, station_id: str) -> str:
    channel = store.get_siriusxm_channel(station_id)
    if session.credentials.configured:
        try:
            stream_url = session.refresh_stream_url(station_id, channel)
        except Exception as exc:
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
    stream_url = str(channel.get("stream_url", ""))
    if stream_url:
        return stream_url
    raise SiriusXmNotConfigured("SiriusXM credentials are not configured")


def should_retry_siriusxm_fetch(session: SiriusXmSession, exc: Exception) -> bool:
    return session.credentials.configured and should_refresh_stream("configured", exc)


def sanitize_siriusxm_error(message: str, credentials: SiriusXmCredentials) -> str:
    sanitized = re.sub(r"https://[^\s\"']+", "[redacted-url]", message)
    for secret in (credentials.username, credentials.password):
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    sanitized = re.sub(r"(token|gupId|password|username)=([^&\s]+)", r"\1=[redacted]", sanitized, flags=re.I)
    return sanitized[:500]


def handle_bmx_services(req: SixBackHandler) -> None:
    req.send_bytes(bmx_services(req.server.public_base), content_type="application/json")


def handle_bmx_services_availability(req: SixBackHandler) -> None:
    req.send_bytes(bmx_services_availability(), content_type="application/json")


def handle_sourceproviders(req: SixBackHandler) -> None:
    req.send_bytes(sourceproviders_xml(), content_type="application/vnd.bose.streaming-v1.2+xml")


def handle_tunein_token(req: SixBackHandler) -> None:
    req.send_bytes(tunein_token(), content_type="application/json")


def handle_tunein_station(req: SixBackHandler, station_id: str) -> None:
    req.send_bytes(tunein_station(station_id, req.server.public_base), content_type="application/json")


def handle_siriusxm_token(req: SixBackHandler) -> None:
    length = int(req.headers.get("Content-Length", "0") or "0")
    if length:
        req.rfile.read(length)
    body = siriusxm_token()
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_availability(req: SixBackHandler) -> None:
    body = siriusxm_availability()
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_station(req: SixBackHandler, station_id: str) -> None:
    body = siriusxm_station(req.server.store, station_id, req.server.public_base)
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_now_playing(req: SixBackHandler, station_id: str) -> None:
    body = siriusxm_now_playing(req.server.store, station_id)
    capture_cloud_response(req, "siriusxm", body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/json")


def handle_siriusxm_proxy_playlist(req: SixBackHandler, station_id: str) -> None:
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
                    "message": "Create /etc/sixback-ubuntu/siriusxm.env with SIRIUSXM_USERNAME and SIRIUSXM_PASSWORD.",
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
        body = fetch_siriusxm_url(stream_url).decode("utf-8", "replace")
    except Exception as exc:
        if should_retry_siriusxm_fetch(req.server.siriusxm, exc):
            try:
                stream_url = resolve_siriusxm_stream_url(req.server.store, req.server.siriusxm, station_id)
                body = fetch_siriusxm_url(stream_url).decode("utf-8", "replace")
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
    trimmed = trim_hls_playlist(body)
    rewritten = rewrite_hls_playlist(trimmed, stream_url, req.server)
    capture_cloud_response(req, "siriusxm", summarize_hls_playlist(station_id, rewritten))
    req.send_bytes(rewritten.encode("utf-8"), content_type="application/x-mpegURL")


def handle_siriusxm_proxy_fetch(req: SixBackHandler, token: str = "") -> None:
    query = parse_qs(urlparse(req.path).query)
    target = req.server.siriusxm_proxy_urls.get(token, "") if token else (query.get("url") or [""])[0]
    if not target.startswith("https://"):
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
        body = fetch_siriusxm_url(target)
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


def fetch_siriusxm_url(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Origin": "https://www.siriusxm.com",
            "Referer": "https://www.siriusxm.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return response.read()


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


def maybe_capture_siriusxm_fetch_success(req: SixBackHandler, target: str, body: str) -> None:
    if should_capture_siriusxm_fetch_success(target):
        capture_cloud_response(req, "siriusxm", body)


def rewrite_hls_playlist(body: str, playlist_url: str, server: SixBackServer) -> str:
    rewritten: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY:"):
            rewritten.append(rewrite_hls_key_line(line, playlist_url, server))
        elif stripped and not stripped.startswith("#"):
            target = inherit_playlist_auth_query(urljoin(playlist_url, stripped), playlist_url)
            rewritten.append(proxy_url(target, server, absolute=False))
        else:
            rewritten.append(line)
    return "\n".join(rewritten) + "\n"


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


def trim_hls_playlist(body: str, max_segments: int = 6) -> str:
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


def rewrite_hls_key_line(line: str, playlist_url: str, server: SixBackServer) -> str:
    def repl(match: re.Match[str]) -> str:
        return f'URI="{proxy_url(urljoin(playlist_url, match.group(1)), server, absolute=False)}"'

    return re.sub(r'URI="([^"]+)"', repl, line)


def proxy_url(target: str, server: SixBackServer, absolute: bool = True) -> str:
    token = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
    server.siriusxm_proxy_urls[token] = target
    path = f"/siriusxm/proxy/fetch/{token}"
    if absolute:
        return f"{server.public_base}{path}"
    return path


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


def handle_siriusxm_needs_auth(req: SixBackHandler, station_id: str) -> None:
    req.send_json(
        {
            "error": "siriusxm_stream_auth_required",
            "station_id": station_id,
            "message": "The preserved SiriusXM preset reached the local adapter, but this MVP still needs authenticated SiriusXM stream URL resolution.",
        },
        501,
    )


def handle_scmudc(req: SixBackHandler, device_id: str) -> None:
    length = int(req.headers.get("Content-Length", "0") or "0")
    body = req.rfile.read(length).decode("utf-8", "replace") if length else ""
    summary = summarize_scmudc(body)
    if summary:
        req.server.store.add_scmudc_event(device_id, summary, body)
        print(f"[scmudc] {device_id} {summary}")
    req.send_text("")


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


def handle_account_full(req: SixBackHandler, account_id: str) -> None:
    body = account_full(req.server.store, account_id)
    capture_cloud_response(req, account_id, body.decode("utf-8", "replace"))
    req.send_bytes(body, content_type="application/xml")


def handle_sources(req: SixBackHandler, account_id: str) -> None:
    body = f'<?xml version="1.0" standalone="yes"?>{sources_xml(req.server.store, account_id)}'
    capture_cloud_response(req, account_id, body)
    req.send_text(body, content_type="application/xml")


def capture_cloud_response(req: SixBackHandler, account_id: str, body: str) -> None:
    path = urlparse(req.path).path
    req.server.store.add_cloud_response(account_id, path, req.client_address[0], body)
    print(f"[cloud-response] account={account_id} path={path} client={req.client_address[0]} bytes={len(body)}")


def redact_cloud_response(body: str) -> str:
    body = re.sub(r'(sourceAccount=")[^"]*(")', r"\1[redacted]\2", body)
    body = re.sub(r"(<username>)[a-fA-F0-9]{16,}(</username>)", r"\1[redacted]\2", body)
    body = re.sub(r'("(?:streamUrl|url)"\s*:\s*")[^"]*(")', r"\1[redacted]\2", body)
    return body


def handle_account_presets(req: SixBackHandler, account_id: str) -> None:
    body = account_presets(req.server.store, account_id)
    if not body:
        req.send_text("", 404)
        return
    req.send_bytes(body, content_type="application/xml")


def handle_device_presets(req: SixBackHandler, account_id: str, device_id: str) -> None:
    body = device_presets(req.server.store, device_id)
    if not body:
        req.send_text("", 404)
        return
    req.send_bytes(body, content_type="application/xml")


def handle_device_add(req: SixBackHandler, account_id: str) -> None:
    length = int(req.headers.get("Content-Length", "0") or "0")
    body = req.rfile.read(length).decode("utf-8", "replace") if length else ""
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
    req.send_header("Credentials", "Bearer sixback-ubuntu-token")
    if device_id:
        req.send_header(
            "Location",
            f"{req.server.public_base}/streaming/account/{account_id}/device/{device_id}",
        )
    req.send_header("METHOD_NAME", "addDevice")
    req.end_headers()
    req.wfile.write(response)


def handle_source_add(req: SixBackHandler, account_id: str) -> None:
    length = int(req.headers.get("Content-Length", "0") or "0")
    body = req.rfile.read(length).decode("utf-8", "replace") if length else ""
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


def handle_updates(req: SixBackHandler) -> None:
    req.send_text('<?xml version="1.0" encoding="UTF-8"?><updates/>', content_type="application/xml")


def handle_empty(req: SixBackHandler, **_: str) -> None:
    length = int(req.headers.get("Content-Length", "0") or "0")
    if length:
        req.rfile.read(length)
    req.send_text("")


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SixBack Ubuntu</title>
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
    <h1>SixBack Ubuntu</h1>
    <div class="meta" id="serverMeta"></div>
  </header>
  <main>
    <section>
      <h2>Speakers</h2>
      <div class="add-row">
        <label>Speaker IP
          <input id="speakerIp" inputmode="decimal" placeholder="192.168.10.50">
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
      </div>
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
    const state = { speakers: [], selected: null, presets: [], sirius: null };
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
      const configured = session.configured ? 'configured' : 'not configured';
      const loggedIn = session.session_authenticated ? 'authenticated' : 'not authenticated';
      const username = session.username ? ` - ${session.username}` : '';
      const error = session.last_error ? ` - ${session.last_error}` : '';
      $('siriusStatus').textContent = `${configured}, ${loggedIn}${username}${error}`;
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
              </select>
            </label>
            <label>Name
              <input data-field="name" value="${escapeAttr(preset.name || '')}">
            </label>
            <label class="wide tunein">TuneIn Station ID
              <input data-field="station_id" value="${escapeAttr(preset.station_id || '')}">
            </label>
            <label class="wide stream">Stream URL
              <input data-field="stream_url" value="${escapeAttr(preset.stream_url || '')}">
            </label>
            <label class="wide">Image URL
              <input data-field="image_url" value="${escapeAttr(preset.image_url || '')}">
            </label>
            <textarea data-field="raw_content_item" hidden>${escapeHtml(preset.raw_content_item || '')}</textarea>
          </div>
          <div class="toolbar">
            <button data-action="save">Save</button>
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
          preset.source === 'SIRIUSXM' ? 'SIRIUSXM' : 'TUNEIN';
        populateCopyOptions(card);
        syncSourceFields(card);
        card.querySelector('[data-field="source"]').onchange = () => syncSourceFields(card);
        card.querySelector('[data-action="save"]').onclick = () => savePreset(card);
        card.querySelector('[data-action="refresh-sirius"]').onclick = () => refreshSiriusPreset(card);
        card.querySelector('[data-action="clear"]').onclick = () => clearPreset(card);
        card.querySelector('[data-action="copy"]').onclick = () => copyPreset(card);
      }
    }

    function syncSourceFields(card) {
      const source = card.querySelector('[data-field="source"]').value;
      const raw = card.querySelector('[data-field="raw_content_item"]').value;
      card.querySelector('.tunein').style.display = source === 'TUNEIN' ? 'grid' : 'none';
      card.querySelector('.stream').style.display = source === 'LOCAL_INTERNET_RADIO' ? 'grid' : 'none';
      card.querySelector('[data-action="refresh-sirius"]').style.display = source === 'SIRIUSXM' ? 'inline-flex' : 'none';
      const msg = card.querySelector('[data-role="card-status"]');
      if (source === 'SIRIUSXM' && raw.includes('SIRIUSXM_EVEREST')) {
        msg.textContent = 'Imported SiriusXM preset preserved from the speaker. You can copy it to another slot.';
        msg.className = 'status ok';
      } else if (source === 'SIRIUSXM') {
        msg.textContent = 'New SiriusXM presets require authenticated adapter support and cannot be created by this MVP.';
        msg.className = 'status warn';
      } else {
        msg.textContent = '';
        msg.className = 'status';
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
        await api(`/api/speakers/${encodeURIComponent(speaker.device_id)}/presets/${slot}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        });
        status.textContent = 'Saved';
        status.className = 'status ok';
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
    parser = argparse.ArgumentParser(description="SixBack Ubuntu MVP")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(Path.home() / ".local/share/sixback-ubuntu/state.sqlite3"))
    parser.add_argument("--public-base", default="")
    args = parser.parse_args(argv)

    public_base = args.public_base or f"http://{guess_lan_ip()}:{args.port}"
    store = Store(args.db)
    server = SixBackServer((args.host, args.port), store, public_base)
    print(f"sixback-ubuntu listening on {args.host}:{args.port}")
    print(f"speaker cloud base: {public_base}")
    print(f"sqlite state: {args.db}")
    server.serve_forever()


if __name__ == "__main__":
    main()

