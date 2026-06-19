from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Callable


DEFAULT_ENV_FILE = "/etc/sixback-ubuntu/siriusxm.env"
K2_REST = "https://player.siriusxm.com/rest/v2/experience/modules/{method}"
EDGE_LIVE_UPDATE = "https://api.edge-gateway.siriusxm.com/playback/play/v1/liveUpdate"
LIVE_PRIMARY_HLS = "https://siriusxm-priprodlive.akamaized.net"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


class SiriusXmError(RuntimeError):
    pass


class SiriusXmNotConfigured(SiriusXmError):
    pass


@dataclass(frozen=True)
class SiriusXmCredentials:
    username: str
    password: str

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


def load_credentials(path: str = DEFAULT_ENV_FILE, environ: dict[str, str] | None = None) -> SiriusXmCredentials:
    values = parse_env_file(path)
    env = os.environ if environ is None else environ
    username = env.get("SIRIUSXM_USERNAME") or values.get("SIRIUSXM_USERNAME", "")
    password = env.get("SIRIUSXM_PASSWORD") or values.get("SIRIUSXM_PASSWORD", "")
    return SiriusXmCredentials(username=username.strip(), password=password.strip())


def parse_env_file(path: str) -> dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[name] = value
    return values


def redact_secret(value: str) -> str:
    if not value:
        return ""
    if "@" in value:
        local, domain = value.split("@", 1)
        first = local[:1] or "*"
        return f"{first}***@{domain}"
    return "[set]"


def should_refresh_stream(stream_url: str, exc: Exception | None = None) -> bool:
    if not stream_url:
        return True
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)


def extract_entity_id(entity_url: str) -> str:
    match = re.search(r"/entity/([^/?#]+)", entity_url)
    return match.group(1) if match else ""


def extract_stream_url(payload: Any) -> str:
    for value in walk_values(payload):
        if not isinstance(value, str):
            continue
        candidate = value.replace("%Live_Primary_HLS%", LIVE_PRIMARY_HLS)
        if candidate.startswith("https://") and ".m3u8" in candidate:
            return candidate
    return ""


def extract_now_playing(payload: Any, station_id: str, station_name: str = "") -> dict[str, str]:
    item = latest_track_item(payload)
    track = str(item.get("name") or item.get("trackName") or station_name or station_id)
    artist = str(item.get("artistName") or item.get("artist") or "")
    album = str(item.get("albumName") or item.get("album") or "")
    channel = str(
        first_string(payload, "channelName")
        or first_string(payload, "stationName")
        or station_name
        or station_id
    )
    image = extract_image_url(item) or extract_image_url(payload)
    return {
        "stationId": station_id,
        "stationName": channel,
        "channelName": channel,
        "trackName": track,
        "artistName": artist or "SiriusXM",
        "albumName": album,
        "imageUrl": image,
        "containerArt": image,
    }


def latest_track_item(payload: Any) -> dict[str, Any]:
    items = []
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items.extend(item for item in payload["items"] if isinstance(item, dict))
    for value in walk_dicts(payload):
        if isinstance(value, dict) and ("artistName" in value or "trackName" in value):
            items.append(value)
    for item in reversed(items):
        if item.get("isInterstitial") is True:
            continue
        flags = item.get("cutFlags", [])
        if isinstance(flags, list) and "INTERSTITIAL" in flags:
            continue
        if item.get("name") or item.get("trackName"):
            return item
    return {}


def walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_dicts(child))
    return found


def first_string(payload: Any, key: str) -> str:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        for child in payload.values():
            found = first_string(child, key)
            if found:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = first_string(child, key)
            if found:
                return found
    return ""


def extract_image_url(payload: Any) -> str:
    for value in walk_values(payload):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            lowered = value.lower()
            if any(fragment in lowered for fragment in (".jpg", ".jpeg", ".png", "image")):
                return value
    return ""


def walk_values(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(walk_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_values(child))
    else:
        found.append(value)
    return found


def is_specific_track_metadata(metadata: dict[str, str], station_id: str, station_name: str = "") -> bool:
    track = (metadata.get("trackName") or "").strip().lower()
    artist = (metadata.get("artistName") or "").strip().lower()
    station_values = {
        station_id.strip().lower(),
        station_name.strip().lower(),
        (metadata.get("stationName") or "").strip().lower(),
        (metadata.get("channelName") or "").strip().lower(),
    }
    station_values.discard("")
    return bool(track and artist and artist != "siriusxm" and track not in station_values)


def iso_z(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SiriusXmSession:
    def __init__(
        self,
        credentials: SiriusXmCredentials,
        opener: Callable[[urllib.request.Request], bytes] | None = None,
    ):
        self.credentials = credentials
        self.cookie_jar = CookieJar()
        self._opener = opener or self._default_open
        self.last_login_at = ""
        self.last_error = ""
        self.channels: list[dict[str, Any]] = []
        self.now_playing_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self.now_playing_ttl = 20.0

    @classmethod
    def from_env(cls, path: str = DEFAULT_ENV_FILE) -> "SiriusXmSession":
        return cls(load_credentials(path))

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.credentials.configured,
            "username": redact_secret(self.credentials.username),
            "logged_in": self.is_logged_in(),
            "session_authenticated": self.is_session_authenticated(),
            "last_login_at": self.last_login_at,
            "last_error": self.last_error,
            "known_channels": len(self.channels),
        }

    def login(self) -> None:
        if not self.credentials.configured:
            self.last_error = "SiriusXM credentials are not configured"
            raise SiriusXmNotConfigured(self.last_error)
        self._ensure_k2_ok(self._post_k2("modify/authentication", self._login_payload(), authenticate=False))
        self._ensure_k2_ok(self._post_k2("resume?OAtrial=false", self._resume_payload(), authenticate=False))
        self.last_login_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.last_error = ""

    def refresh_stream_url(self, station_id: str, channel: dict[str, Any] | None = None) -> str:
        if not self.is_session_authenticated():
            self.login()
        channel_info = self.resolve_channel(station_id, channel or {})
        data = self._get_k2("tune/now-playing-live", self._live_params(channel_info, station_id))
        url = extract_stream_url(data)
        if not url:
            raise SiriusXmError("SiriusXM did not return an HLS playlist URL")
        if "%Live_Primary_HLS%" in url:
            url = url.replace("%Live_Primary_HLS%", LIVE_PRIMARY_HLS)
        variant = self._resolve_playlist_variant(url)
        return self._with_stream_auth(variant) if variant else self._with_stream_auth(url)

    def now_playing(self, station_id: str, channel: dict[str, Any] | None = None) -> dict[str, str]:
        cached = self.now_playing_cache.get(station_id)
        now = time.time()
        if cached and now - cached[0] < self.now_playing_ttl:
            return cached[1]
        if not self.is_session_authenticated():
            self.login()
        channel_data = channel or {}
        station_name = str(channel_data.get("name", ""))
        entity_id = extract_entity_id(str(channel_data.get("entity_url", "")))
        if entity_id:
            try:
                data = self._edge_live_update(entity_id)
                metadata = extract_now_playing(data, station_id, station_name)
                if is_specific_track_metadata(metadata, station_id, station_name):
                    self.now_playing_cache[station_id] = (now, metadata)
                    return metadata
            except Exception:
                pass
        channel_info = self.resolve_channel(station_id, channel_data)
        data = self._get_k2("tune/now-playing-live", self._live_params(channel_info, station_id))
        metadata = extract_now_playing(data, station_id, station_name)
        self.now_playing_cache[station_id] = (now, metadata)
        return metadata

    def _edge_live_update(self, entity_id: str) -> Any:
        return json.loads(self._opener(self._edge_live_update_request(entity_id)).decode("utf-8", "replace"))

    def _edge_live_update_request(self, entity_id: str) -> urllib.request.Request:
        now = dt.datetime.now(dt.UTC)
        payload = {
            "channelId": entity_id,
            "startTimestamp": iso_z(now - dt.timedelta(minutes=8)),
            "endTimestamp": iso_z(now + dt.timedelta(minutes=2)),
        }
        return urllib.request.Request(
            EDGE_LIVE_UPDATE,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://www.siriusxm.com",
                "Referer": "https://www.siriusxm.com/",
            },
            method="POST",
        )

    def _live_params(self, channel_info: dict[str, str], station_id: str) -> dict[str, str]:
        return {
            "assetGUID": channel_info.get("guid") or channel_info.get("channelGuid") or station_id,
            "ccRequestType": "AUDIO_VIDEO",
            "channelId": channel_info.get("channel_id") or station_id,
            "hls_output_mode": "custom",
            "marker_mode": "all_separate_cue_points",
            "result-template": "web",
            "time": str(int(round(time.time() * 1000.0))),
            "timestamp": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    def resolve_channel(self, station_id: str, channel: dict[str, Any]) -> dict[str, str]:
        entity_id = extract_entity_id(str(channel.get("entity_url", "")))
        candidates = [station_id.lower(), entity_id.lower()]
        for item in self.get_channels():
            values = {
                str(item.get("channelId", "")).lower(),
                str(item.get("channelGuid", "")).lower(),
                str(item.get("assetGUID", "")).lower(),
                str(item.get("urlKey", "")).lower(),
                str(item.get("key", "")).lower(),
                str(item.get("name", "")).replace(" ", "").lower(),
                str(item.get("channelName", "")).replace(" ", "").lower(),
                str(item.get("channelNumber", "")).lower(),
            }
            if any(candidate and candidate in values for candidate in candidates):
                return {
                    "guid": str(item.get("channelGuid") or item.get("assetGUID") or item.get("guid") or station_id),
                    "channel_id": str(item.get("channelId") or item.get("channelGuid") or station_id),
                }
        return {"guid": entity_id or station_id, "channel_id": station_id}

    def get_channels(self) -> list[dict[str, Any]]:
        if self.channels:
            return self.channels
        try:
            data = self._post_k2("get/discovery/channel-listing", self._channel_listing_payload())
        except Exception:
            return []
        channels: list[dict[str, Any]] = []
        for value in walk_values(data):
            if isinstance(value, dict) and (
                "channelId" in value or "channelGuid" in value or "channelNumber" in value
            ):
                channels.append(value)
        self.channels = channels
        return channels

    def is_logged_in(self) -> bool:
        names = {cookie.name for cookie in self.cookie_jar}
        return "SXMAUTH" in names or "SXMAUTHNEW" in names

    def is_session_authenticated(self) -> bool:
        names = {cookie.name for cookie in self.cookie_jar}
        return bool({"AWSELB", "JSESSIONID"} & names) or self.is_logged_in()

    def _resolve_playlist_variant(self, url: str) -> str:
        playlist = self._request_text(self._with_stream_auth(url))
        for line in playlist.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and ".m3u8" in stripped:
                return urllib.parse.urljoin(url, stripped)
        return ""

    def _with_stream_auth(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        existing = {name for name, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
        params = {}
        token = self._sxmak_token()
        gup_id = self._gup_id()
        if token and "token" not in existing:
            params["token"] = token
        if gup_id and "gupId" not in existing:
            params["gupId"] = gup_id
        if "consumer" not in existing:
            params["consumer"] = "k2"
        if not params:
            return url
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{separator}{urllib.parse.urlencode(params)}"

    def _sxmak_token(self) -> str:
        for cookie in self.cookie_jar:
            if cookie.name == "SXMAKTOKEN":
                return cookie.value.split("=", 1)[-1].split(",", 1)[0]
        return ""

    def _gup_id(self) -> str:
        for cookie in self.cookie_jar:
            if cookie.name == "SXMDATA":
                try:
                    return str(json.loads(urllib.parse.unquote(cookie.value)).get("gupId", ""))
                except json.JSONDecodeError:
                    return ""
        return ""

    def _get_k2(self, method: str, params: dict[str, str]) -> Any:
        url = K2_REST.format(method=method)
        return self._request_json(f"{url}?{urllib.parse.urlencode(params)}")

    def _post_k2(self, method: str, payload: dict[str, Any], authenticate: bool = True) -> Any:
        if authenticate and not self.is_session_authenticated():
            self.login()
        request = self._json_request(K2_REST.format(method=method), payload)
        return json.loads(self._opener(request).decode("utf-8", "replace"))

    def _ensure_k2_ok(self, payload: Any) -> None:
        response = payload.get("ModuleListResponse", {}) if isinstance(payload, dict) else {}
        if response.get("status") == 1:
            return
        messages = response.get("messages", [])
        message = ""
        if isinstance(messages, list) and messages:
            message = str(messages[0].get("message", "")) if isinstance(messages[0], dict) else str(messages[0])
        self.last_error = message or "SiriusXM login failed"
        raise SiriusXmError(self.last_error)

    def _request_json(self, url: str) -> Any:
        return json.loads(self._request_text(url))

    def _request_text(self, url: str) -> str:
        request = urllib.request.Request(url, headers=self._headers())
        return self._opener(request).decode("utf-8", "replace")

    def _json_request(self, url: str, payload: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json;charset=UTF-8"},
            method="POST",
        )

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://player.siriusxm.com",
            "Referer": "https://player.siriusxm.com/",
        }

    def _default_open(self, request: urllib.request.Request) -> bytes:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        try:
            with opener.open(request, timeout=15) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            self.last_error = f"SiriusXM HTTP {exc.code} for {urllib.parse.urlparse(exc.url).path}"
            raise
        except urllib.error.URLError as exc:
            self.last_error = f"SiriusXM URL error: {exc.reason}"
            raise

    def _login_payload(self) -> dict[str, Any]:
        return {
            "moduleList": {
                "modules": [
                    {
                        "moduleRequest": {
                            "resultTemplate": "web",
                            "deviceInfo": device_info(),
                            "standardAuth": {
                                "username": self.credentials.username,
                                "password": self.credentials.password,
                            },
                        }
                    }
                ]
            }
        }

    def _resume_payload(self) -> dict[str, Any]:
        return {
            "moduleList": {
                "modules": [
                    {
                        "moduleRequest": {
                            "resultTemplate": "web",
                            "deviceInfo": device_info(),
                        }
                    }
                ]
            }
        }

    def _channel_listing_payload(self) -> dict[str, Any]:
        return {
            "moduleList": {
                "modules": [
                    {
                        "moduleArea": "Discovery",
                        "moduleType": "ChannelListing",
                        "moduleRequest": {
                            "resultTemplate": "web",
                            "deviceInfo": device_info(),
                            "lineupId": "",
                        },
                    }
                ]
            }
        }


def device_info() -> dict[str, str]:
    return {
        "osVersion": "Mac",
        "platform": "Web",
        "clientDeviceType": "web",
        "sxmAppVersion": "3.1802.10011.0",
        "browser": "Safari",
        "browserVersion": "17.0",
        "appRegion": "US",
        "deviceModel": "K2WebClient",
        "player": "html5",
        "clientDeviceId": "null",
    }
