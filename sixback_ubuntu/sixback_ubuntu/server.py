from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .cloud import (
    account_full,
    account_presets,
    bmx_services,
    bmx_services_availability,
    device_presets,
    sources_xml,
    tunein_station,
    tunein_token,
)
from .db import Store
from .speaker import import_presets, migrate_speaker, probe_speaker


Json = dict[str, Any]


class SixBackHandler(BaseHTTPRequestHandler):
    server: "SixBackServer"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., None]]] = []
        self._register_routes()

    def route(self, method: str, pattern: str, handler: Callable[..., None]) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def _register_routes(self) -> None:
        self.route("GET", r"/", handle_root)
        self.route("GET", r"/healthz", handle_healthz)
        self.route("GET", r"/api/speakers", handle_list_speakers)
        self.route("POST", r"/api/speakers", handle_add_speaker)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/import-presets", handle_import_presets)
        self.route("POST", r"/api/speakers/(?P<device_id>[^/]+)/migrate", handle_migrate)
        self.route("GET", r"/bmx/registry/v1/services", handle_bmx_services)
        self.route("GET", r"/bmx/registry/v1/servicesAvailability", handle_bmx_services_availability)
        self.route("POST", r"/bmx/tunein/v1/token", handle_tunein_token)
        self.route("GET", r"/bmx/tunein/v1/playback/station/(?P<station_id>[^/]+)", handle_tunein_station)
        self.route("POST", r"/bmx/tunein/v1/report", handle_empty)
        self.route("POST", r"/v1/scmudc/(?P<device_id>[^/]+)", handle_empty)
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
    req.send_json(
        {
            "name": "sixback-ubuntu",
            "version": __version__,
            "public_base": req.server.public_base,
            "admin": {
                "list_speakers": "GET /api/speakers",
                "add_speaker": 'POST /api/speakers {"ip":"192.168.1.50"}',
                "import_presets": "POST /api/speakers/{device_id}/import-presets",
                "migrate": "POST /api/speakers/{device_id}/migrate",
            },
        }
    )


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


def handle_bmx_services(req: SixBackHandler) -> None:
    req.send_bytes(bmx_services(req.server.public_base), content_type="application/json")


def handle_bmx_services_availability(req: SixBackHandler) -> None:
    req.send_bytes(bmx_services_availability(), content_type="application/json")


def handle_tunein_token(req: SixBackHandler) -> None:
    req.send_bytes(tunein_token(), content_type="application/json")


def handle_tunein_station(req: SixBackHandler, station_id: str) -> None:
    req.send_bytes(tunein_station(station_id, req.server.public_base), content_type="application/json")


def handle_account_full(req: SixBackHandler, account_id: str) -> None:
    req.send_bytes(account_full(req.server.store, account_id), content_type="application/xml")


def handle_sources(req: SixBackHandler, account_id: str) -> None:
    req.send_text(f'<?xml version="1.0" standalone="yes"?>{sources_xml()}', content_type="application/xml")


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
