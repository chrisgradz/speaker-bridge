from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
import json
from io import BytesIO
from http.cookiejar import Cookie
from types import SimpleNamespace
from unittest.mock import patch

from soundtouch_bridge.db import Store
from soundtouch_bridge.cloud import (
    siriusxm_now_playing,
    siriusxm_station_display_experiment,
    siriusxm_station,
    tunein_station,
)
from soundtouch_bridge.siriusxm import (
    DEFAULT_ENV_FILE,
    SiriusXmCredentials,
    SiriusXmSession,
    extract_entity_id,
    extract_now_playing,
    extract_stream_url,
    extract_public_channel_guide,
    load_credentials,
    redact_secret,
    should_refresh_stream,
)
from soundtouch_bridge.server import (
    ADMIN_HTML,
    PLAY_HTML,
    SoundTouchBridgeServer,
    build_play_content_item,
    build_siriusxm_display_experiment_content_item,
    build_siriusxm_content_item,
    rewrite_siriusxm_preset_content_item,
    handle_siriusxm_now_playing_debug,
    handle_tunein_station,
    iheart_playlist_body,
    iheart_playlist_url,
    iheart_proxy_stream_url,
    iheart_station_descriptor,
    iheart_station_descriptor_url,
    normalize_iheart_search_station,
    normalize_siriusxm_catalog_channel,
    maybe_override_siriusxm_preset_press,
    normalize_tunein_search_station,
    resolve_iheart_stream_url,
    prepare_admin_preset,
    pressed_preset_slot,
    remember_siriusxm_station_alias,
    resolve_siriusxm_stream_url,
    resolve_siriusxm_station_alias,
    sanitize_siriusxm_error,
    search_iheart_stations,
    search_tunein_stations,
    speaker_now_playing_snapshot,
    siriusxm_metadata_proxy_debug_payload,
    tunein_icy_debug_payload,
    push_station_to_speaker,
)
from soundtouch_bridge.speaker import preset_to_xml, store_preset_xml


class SiriusXmAuthTests(unittest.TestCase):
    def test_load_credentials_from_env_file_and_redacts_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "siriusxm.env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "SIRIUSXM_USERNAME='listener@example.com'\n"
                    'SIRIUSXM_PASSWORD="secret password"\n'
                )

            creds = load_credentials(path, environ={})

        self.assertEqual(creds.username, "listener@example.com")
        self.assertEqual(creds.password, "secret password")
        self.assertEqual(redact_secret(creds.username), "l***@example.com")
        self.assertEqual(redact_secret(creds.password), "[set]")

    def test_extract_stream_url_finds_hls_url_in_nested_payload(self) -> None:
        payload = {
            "result": {
                "sets": [
                    {
                        "type": "hls",
                        "url": "https://live-akc-prod-device.streaming.siriusxm.com/v1/session/playlist.m3u8?token=abc",
                    }
                ]
            }
        }

        self.assertEqual(
            extract_stream_url(payload),
            "https://live-akc-prod-device.streaming.siriusxm.com/v1/session/playlist.m3u8?token=abc",
        )

    def test_extract_entity_id_from_player_url(self) -> None:
        self.assertEqual(
            extract_entity_id(
                "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e"
            ),
            "65f04311-3581-256c-97b9-279838d6ff5e",
        )

    def test_extract_now_playing_uses_latest_non_interstitial_item(self) -> None:
        payload = {
            "channelName": "1st Wave",
            "items": [
                {
                    "name": "Station Liner",
                    "artistName": "1st Wave",
                    "isInterstitial": True,
                    "timestamp": "2026-06-19T15:00:00Z",
                },
                {
                    "name": "Just Like Heaven",
                    "artistName": "The Cure",
                    "albumName": "Kiss Me, Kiss Me, Kiss Me",
                    "images": {"tile": {"aspect_1x1": "https://img.example/cure.jpg"}},
                    "isInterstitial": False,
                    "timestamp": "2026-06-19T15:01:00Z",
                },
            ],
        }

        metadata = extract_now_playing(payload, "firstwave", "1st Wave")

        self.assertEqual(metadata["trackName"], "Just Like Heaven")
        self.assertEqual(metadata["artistName"], "The Cure")
        self.assertEqual(metadata["albumName"], "Kiss Me, Kiss Me, Kiss Me")
        self.assertEqual(metadata["channelName"], "1st Wave")
        self.assertEqual(metadata["imageUrl"], "https://img.example/cure.jpg")

    def test_extract_now_playing_accepts_http_artwork_urls(self) -> None:
        payload = {
            "channelName": "1st Wave",
            "items": [
                {
                    "name": "Just Like Heaven",
                    "artistName": "The Cure",
                    "images": {"tile": "http://img.example/cure.png"},
                }
            ],
        }

        metadata = extract_now_playing(payload, "firstwave", "1st Wave")

        self.assertEqual(metadata["imageUrl"], "http://img.example/cure.png")

    def test_extract_now_playing_finds_nested_track_items(self) -> None:
        payload = {
            "ModuleListResponse": {
                "moduleList": {
                    "modules": [
                        {
                            "moduleResponse": {
                                "liveChannelData": {
                                    "items": [
                                        {
                                            "trackName": "Blue Monday",
                                            "artistName": "New Order",
                                            "cutFlags": ["SONG"],
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            }
        }

        metadata = extract_now_playing(payload, "firstwave", "1st Wave")

        self.assertEqual(metadata["trackName"], "Blue Monday")
        self.assertEqual(metadata["artistName"], "New Order")

    def test_refresh_decision_covers_missing_and_auth_errors(self) -> None:
        self.assertTrue(should_refresh_stream(""))
        self.assertFalse(should_refresh_stream("https://example.test/live.m3u8"))
        unauthorized = urllib.error.HTTPError(
            "https://example.test/live.m3u8",
            401,
            "Unauthorized",
            hdrs={},
            fp=BytesIO(b""),
        )
        server_error = urllib.error.HTTPError(
            "https://example.test/live.m3u8",
            500,
            "Server Error",
            hdrs={},
            fp=BytesIO(b""),
        )
        try:
            self.assertTrue(
                should_refresh_stream(
                    "https://example.test/live.m3u8",
                    unauthorized,
                )
            )
            self.assertFalse(
                should_refresh_stream(
                    "https://example.test/live.m3u8",
                    server_error,
                )
            )
        finally:
            unauthorized.close()
            server_error.close()

    def test_store_keeps_refresh_metadata_with_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel(
                "firstwave",
                {
                    "name": "1st Wave",
                    "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/entity-id",
                    "stream_url": "",
                },
            )

            saved = store.update_siriusxm_stream_status(
                "firstwave",
                stream_url="https://live.example.test/playlist.m3u8",
                stream_expires_at="2026-06-19T05:00:00Z",
                last_refresh_error="",
            )
            store.conn.close()

        self.assertEqual(saved["stream_url"], "https://live.example.test/playlist.m3u8")
        self.assertEqual(saved["stream_expires_at"], "2026-06-19T05:00:00Z")
        self.assertEqual(saved["last_refresh_error"], "")

    def test_session_status_does_not_expose_password(self) -> None:
        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=lambda request: b"{}",
        )

        status = session.status()

        self.assertEqual(status["configured"], True)
        self.assertEqual(status["username"], "l***@example.com")
        self.assertNotIn("secret", repr(status))

    def test_session_get_channels_extracts_nested_channel_dicts(self) -> None:
        def opener(request):
            self.assertIn("get/discovery/channel-listing", request.full_url)
            return json.dumps(
                {
                    "ModuleListResponse": {
                        "moduleList": {
                            "modules": [
                                {
                                    "moduleResponse": {
                                        "channelListing": {
                                            "channels": [
                                                {
                                                    "channelId": "firstwave",
                                                    "channelGuid": "65f04311-3581-256c-97b9-279838d6ff5e",
                                                    "channelName": "1st Wave",
                                                    "channelNumber": 33,
                                                }
                                            ]
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            ).encode("utf-8")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.cookie_jar.set_cookie(make_cookie("JSESSIONID", "session-123"))

        channels = session.get_channels()

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channelId"], "firstwave")
        self.assertEqual(session.status()["known_channels"], 1)

    def test_public_channel_guide_extracts_channels_from_embedded_page(self) -> None:
        html = (
            '<script>{"contentId":"firstwave","displayName":"1st Wave",'
            '"streamingChannelNumber":33,'
            '"colorLogo":"/content/dam/sxm-com/channel-logos/Music/x-Rock/1st-Wave/1stWave-4C.svg"}</script>'
            '<script>{"contentId":"classicvinyl","displayName":"Classic Vinyl",'
            '"xmChannelNumber":26}</script>'
        )

        channels = extract_public_channel_guide(html)

        self.assertEqual(
            channels[0],
            {
                "channelId": "firstwave",
                "channelName": "1st Wave",
                "channelNumber": 33,
                "images": {"logo": {"url": "/content/dam/sxm-com/channel-logos/Music/x-Rock/1st-Wave/1stWave-4C.svg"}},
            },
        )
        self.assertEqual(channels[1]["channelId"], "classicvinyl")
        self.assertEqual(channels[1]["channelNumber"], 26)

    def test_session_get_channels_falls_back_to_public_channel_guide(self) -> None:
        requests = []

        def opener(request):
            requests.append(request.full_url)
            if "get/discovery/channel-listing" in request.full_url:
                return b'{"ModuleListResponse":{"moduleList":{"modules":[{"moduleResponse":{"message":"no channel objects"}}]}}}'
            if request.full_url == "https://www.siriusxm.com/channels":
                return (
                    b'<script>{"contentId":"firstwave","displayName":"1st Wave",'
                    b'"streamingChannelNumber":33}</script>'
                )
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.cookie_jar.set_cookie(make_cookie("JSESSIONID", "session-123"))

        channels = session.get_channels()

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channelId"], "firstwave")
        self.assertTrue(any(url == "https://www.siriusxm.com/channels" for url in requests))

    def test_session_refresh_uses_k2_resolver_and_variant_playlist(self) -> None:
        requests = []

        def opener(request):
            requests.append(request)
            url = request.full_url
            if "modify/authentication" in url or "resume" in url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "tune/now-playing-live" in url:
                return json.dumps(
                    {
                        "ModuleListResponse": {
                            "moduleList": {
                                "modules": [
                                    {
                                        "moduleResponse": {
                                            "liveChannelData": {
                                                "hlsAudioInfos": [
                                                    {
                                                        "size": "LARGE",
                                                        "url": "%Live_Primary_HLS%/firstwave/master.m3u8",
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                ).encode("utf-8")
            if "master.m3u8" in url:
                return b"#EXTM3U\nfirstwave_256k.m3u8\n"
            raise AssertionError(f"unexpected URL {url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.cookie_jar.set_cookie(make_cookie("SXMAKTOKEN", "token=abc,expires=tomorrow"))
        session.cookie_jar.set_cookie(make_cookie("SXMDATA", "%7B%22gupId%22%3A%22gup-123%22%7D"))

        stream_url = session.refresh_stream_url("firstwave", {})

        self.assertEqual(
            stream_url,
            "https://siriusxm-priprodlive.akamaized.net/firstwave/firstwave_256k.m3u8?token=abc&gupId=gup-123&consumer=k2",
        )
        self.assertTrue(any("modify/authentication" in req.full_url for req in requests))
        self.assertTrue(any("tune/now-playing-live" in req.full_url for req in requests))

    def test_login_rejects_failed_k2_status(self) -> None:
        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=lambda request: b'{"ModuleListResponse":{"status":0,"messages":[{"message":"bad login"}]}}',
        )

        with self.assertRaisesRegex(Exception, "bad login"):
            session.login()

    def test_session_now_playing_fetches_and_caches_metadata(self) -> None:
        requests = []

        def opener(request):
            requests.append(request.full_url)
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "tune/now-playing-live" in request.full_url:
                return json.dumps(
                    {
                        "channelName": "1st Wave",
                        "items": [
                            {
                                "name": "Just Like Heaven",
                                "artistName": "The Cure",
                                "isInterstitial": False,
                            }
                        ],
                    }
                ).encode("utf-8")
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )

        first = session.now_playing("firstwave", {"name": "1st Wave"})
        second = session.now_playing("firstwave", {"name": "1st Wave"})

        self.assertEqual(first["trackName"], "Just Like Heaven")
        self.assertEqual(second["artistName"], "The Cure")
        self.assertEqual(sum("tune/now-playing-live" in url for url in requests), 1)

    def test_session_now_playing_prefers_edge_live_update_for_entity_channels(self) -> None:
        requests = []

        def opener(request):
            requests.append(request)
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "liveUpdate" in request.full_url:
                self.assertEqual(request.get_method(), "POST")
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["channelId"], "65f04311-3581-256c-97b9-279838d6ff5e")
                return json.dumps(
                    {
                        "channelName": "1st Wave",
                        "items": [
                            {
                                "name": "Station Liner",
                                "artistName": "1st Wave",
                                "isInterstitial": True,
                            },
                            {
                                "name": "Just Like Heaven",
                                "artistName": "The Cure",
                                "albumName": "Kiss Me, Kiss Me, Kiss Me",
                                "isInterstitial": False,
                            },
                        ],
                    }
                ).encode("utf-8")
            if "tune/now-playing-live" in request.full_url:
                raise AssertionError("edge liveUpdate metadata should be used before K2 fallback")
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.edge_access_token = "edge-access"

        metadata = session.now_playing(
            "firstwave",
            {
                "name": "1st Wave",
                "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e",
            },
        )

        self.assertEqual(metadata["trackName"], "Just Like Heaven")
        self.assertEqual(metadata["artistName"], "The Cure")
        self.assertTrue(any("liveUpdate" in request.full_url for request in requests))

    def test_session_now_playing_uses_catalog_guid_for_edge_live_update(self) -> None:
        requests = []

        def opener(request):
            requests.append(request)
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "liveUpdate" in request.full_url:
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["channelId"], "big80s-guid")
                return json.dumps(
                    {
                        "channelName": "80s on 8",
                        "items": [
                            {
                                "name": "Everybody Wants To Rule The World",
                                "artistName": "Tears For Fears",
                            }
                        ],
                    }
                ).encode("utf-8")
            if "tune/now-playing-live" in request.full_url:
                raise AssertionError("catalog GUID should allow edge metadata before K2 fallback")
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.edge_access_token = "edge-access"
        session.channels = [
            {
                "channelId": "big80s",
                "channelGuid": "big80s-guid",
                "channelName": "80s on 8",
            }
        ]

        metadata = session.now_playing("big80s", {"name": "80s on 8"})

        self.assertEqual(metadata["trackName"], "Everybody Wants To Rule The World")
        self.assertEqual(metadata["artistName"], "Tears For Fears")
        self.assertEqual(session.last_now_playing_debug["big80s"]["entity_id"], "big80s-guid")

    def test_edge_live_update_uses_authenticated_edge_access_token(self) -> None:
        requests = []

        def opener(request):
            requests.append(request)
            headers = {name.lower(): value for name, value in request.header_items()}
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
            if request.full_url.endswith("/device/v2/devices"):
                self.assertEqual(body["devicePlatform"], "web-desktop")
                self.assertEqual(body["tenant"], "sxm")
                self.assertEqual(body["grantVersion"], "v2")
                return b'{"grant":"device-grant"}'
            if request.full_url.endswith("/session/v1/sessions/anonymous"):
                self.assertEqual(headers["authorization"], "Bearer device-grant")
                return b'{"accessToken":"anonymous-access"}'
            if request.full_url.endswith("/identity/v1/identities/authenticate/password"):
                self.assertEqual(headers["authorization"], "Bearer anonymous-access")
                self.assertEqual(body["handle"], "listener@example.com")
                self.assertEqual(body["password"], "secret password")
                return b'{"grant":"identity-grant"}'
            if request.full_url.endswith("/session/v1/sessions/authenticated"):
                self.assertEqual(headers["authorization"], "Bearer identity-grant")
                return b'{"accessToken":"edge-access","accessTokenExpiresAt":"2099-01-01T00:00:00Z"}'
            if "liveUpdate" in request.full_url:
                self.assertEqual(headers["authorization"], "Bearer edge-access")
                return json.dumps(
                    {
                        "channelName": "1st Wave",
                        "items": [
                            {
                                "name": "Just Like Heaven",
                                "artistName": "The Cure",
                            }
                        ],
                    }
                ).encode("utf-8")
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )

        metadata = session.now_playing(
            "firstwave",
            {
                "name": "1st Wave",
                "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e",
            },
        )

        self.assertEqual(metadata["trackName"], "Just Like Heaven")
        self.assertEqual(metadata["artistName"], "The Cure")
        self.assertTrue(any(request.full_url.endswith("/device/v2/devices") for request in requests))
        self.assertTrue(any("liveUpdate" in request.full_url for request in requests))

    def test_session_now_playing_falls_back_to_k2_when_edge_metadata_fails(self) -> None:
        requests = []

        def opener(request):
            requests.append(request.full_url)
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "liveUpdate" in request.full_url:
                raise urllib.error.URLError("edge unavailable")
            if "tune/now-playing-live" in request.full_url:
                return json.dumps(
                    {
                        "channelName": "1st Wave",
                        "items": [
                            {
                                "name": "Blue Monday",
                                "artistName": "New Order",
                                "isInterstitial": False,
                            }
                        ],
                    }
                ).encode("utf-8")
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.edge_access_token = "edge-access"

        metadata = session.now_playing(
            "firstwave",
            {
                "name": "1st Wave",
                "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e",
            },
        )

        self.assertEqual(metadata["trackName"], "Blue Monday")
        self.assertEqual(metadata["artistName"], "New Order")
        self.assertTrue(any("liveUpdate" in url for url in requests))
        self.assertTrue(any("tune/now-playing-live" in url for url in requests))

    def test_session_now_playing_records_debug_sources_and_allows_forced_probe(self) -> None:
        requests = []

        def opener(request):
            requests.append(request.full_url)
            if "modify/authentication" in request.full_url or "resume" in request.full_url:
                return b'{"ModuleListResponse":{"status":1}}'
            if "liveUpdate" in request.full_url:
                raise urllib.error.URLError("edge unavailable")
            if "tune/now-playing-live" in request.full_url:
                return json.dumps(
                    {
                        "channelName": "1st Wave",
                        "items": [
                            {
                                "name": "Blue Monday",
                                "artistName": "New Order",
                                "isInterstitial": False,
                            }
                        ],
                    }
                ).encode("utf-8")
            raise AssertionError(f"unexpected URL {request.full_url}")

        session = SiriusXmSession(
            SiriusXmCredentials("listener@example.com", "secret password"),
            opener=opener,
        )
        session.edge_access_token = "edge-access"
        channel = {
            "name": "1st Wave",
            "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e",
        }

        session.now_playing("firstwave", channel)
        session.now_playing("firstwave", channel)
        session.now_playing("firstwave", channel, force=True)

        debug = session.last_now_playing_debug["firstwave"]
        self.assertEqual(debug["station_id"], "firstwave")
        self.assertEqual(debug["entity_id"], "65f04311-3581-256c-97b9-279838d6ff5e")
        self.assertEqual(debug["sources"][0]["source"], "edge_live_update")
        self.assertEqual(debug["sources"][0]["result"], "error")
        self.assertEqual(debug["sources"][1]["source"], "k2_now_playing")
        self.assertEqual(debug["sources"][1]["result"], "specific_track")
        self.assertEqual(sum("tune/now-playing-live" in url for url in requests), 2)

    def test_debug_handler_forces_now_playing_probe_and_returns_source_summary(self) -> None:
        class FakeSession:
            credentials = SiriusXmCredentials("listener@example.com", "secret password")

            def __init__(self) -> None:
                self.force = None
                self.channel = None
                self.last_now_playing_debug = {}

            def now_playing(self, station_id, channel, force=False):
                self.force = force
                self.channel = channel
                self.last_now_playing_debug[station_id] = {
                    "station_id": station_id,
                    "sources": [{"source": "edge_live_update", "result": "station_only"}],
                }
                return {"trackName": "1st Wave", "artistName": "SiriusXM"}

            def status(self):
                return {"configured": True}

        class FakeRequest:
            def __init__(self, store, session) -> None:
                self.server = SimpleNamespace(store=store, siriusxm=session)
                self.status = None
                self.body = None

            def send_json(self, body, status=200):
                self.body = body
                self.status = status

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel(
                "firstwave",
                {
                    "name": "1st Wave",
                    "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/entity-id",
                    "stream_url": "https://live.example.test/playlist.m3u8?token=secret",
                },
            )
            session = FakeSession()
            req = FakeRequest(store, session)

            try:
                handle_siriusxm_now_playing_debug(req, "firstwave")
            finally:
                store.conn.close()

        self.assertEqual(req.status, 200)
        self.assertTrue(session.force)
        self.assertEqual(session.channel["name"], "1st Wave")
        self.assertEqual(req.body["metadata"]["trackName"], "1st Wave")
        self.assertEqual(req.body["debug"]["sources"][0]["source"], "edge_live_update")
        self.assertNotIn("stream_url", req.body["channel"])

    def test_server_reuses_cached_authenticated_stream_url(self) -> None:
        class FakeSession:
            credentials = SiriusXmCredentials("listener@example.com", "secret password")

            def refresh_stream_url(self, station_id, channel):
                raise AssertionError("playback should not refresh a cached stream URL")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel(
                "firstwave",
                {
                    "name": "1st Wave",
                    "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/entity-id",
                    "stream_url": "https://live.example.test/cached.m3u8?token=abc&consumer=k2",
                },
            )

            try:
                resolved = resolve_siriusxm_stream_url(store, FakeSession(), "firstwave")
            finally:
                store.conn.close()

        self.assertEqual(resolved, "https://live.example.test/cached.m3u8?token=abc&consumer=k2")

    def test_server_forced_refresh_replaces_cached_stream_url(self) -> None:
        class FakeSession:
            credentials = SiriusXmCredentials("listener@example.com", "secret password")

            def refresh_stream_url(self, station_id, channel):
                self.station_id = station_id
                self.channel = channel
                return "https://live.example.test/fresh.m3u8"

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel(
                "firstwave",
                {
                    "name": "1st Wave",
                    "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/entity-id",
                    "stream_url": "https://expired.example.test/from-har.m3u8",
                },
            )

            try:
                resolved = resolve_siriusxm_stream_url(store, FakeSession(), "firstwave", force=True)
                saved = store.get_siriusxm_channel("firstwave")
            finally:
                store.conn.close()

        self.assertEqual(resolved, "https://live.example.test/fresh.m3u8")
        self.assertEqual(saved["stream_url"], "https://live.example.test/fresh.m3u8")

    def test_siriusxm_station_routes_to_proxy_before_stream_url_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel(
                "firstwave",
                {
                    "name": "1st Wave",
                    "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/entity-id",
                    "stream_url": "",
                },
            )

            body = siriusxm_station(store, "firstwave", "http://ubuntu.example:8000").decode("utf-8")
            store.conn.close()

        self.assertIn("http://ubuntu.example:8000/siriusxm/proxy/firstwave/playlist.m3u8", body)
        self.assertNotIn("/siriusxm/needs-auth/firstwave", body)

    def test_normalize_siriusxm_catalog_channel_for_admin_picker(self) -> None:
        normalized = normalize_siriusxm_catalog_channel(
            {
                "channelId": "firstwave",
                "channelGuid": "65f04311-3581-256c-97b9-279838d6ff5e",
                "channelName": "1st Wave",
                "channelNumber": 33,
                "images": {"tile": {"aspect_1x1": {"preferredImage": {"url": "chan.png"}}}},
            }
        )

        self.assertEqual(normalized["station_id"], "firstwave")
        self.assertEqual(normalized["name"], "1st Wave")
        self.assertEqual(normalized["number"], "33")
        self.assertEqual(
            normalized["entity_url"],
            "https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e",
        )
        self.assertEqual(normalized["image_url"], "http://pri.art.prod.streaming.siriusxm.com/chan.png")

    def test_normalize_siriusxm_catalog_channel_prefers_slug_over_numeric_channel_id(self) -> None:
        normalized = normalize_siriusxm_catalog_channel(
            {
                "channelId": "9450",
                "urlKey": "poprocks",
                "channelName": "PopRocks",
                "channelNumber": 6,
            }
        )

        self.assertEqual(normalized["station_id"], "poprocks")
        self.assertEqual(normalized["name"], "PopRocks")
        self.assertEqual(normalized["number"], "6")

    def test_normalize_siriusxm_catalog_channel_derives_slug_when_only_numeric_id_exists(self) -> None:
        normalized = normalize_siriusxm_catalog_channel(
            {
                "channelId": "9450",
                "channelName": "PopRocks",
                "channelNumber": 6,
            }
        )

        self.assertEqual(normalized["station_id"], "poprocks")
        self.assertEqual(normalized["name"], "PopRocks")
        self.assertEqual(normalized["number"], "6")

    def test_normalize_siriusxm_catalog_channel_uses_public_logo_urls(self) -> None:
        normalized = normalize_siriusxm_catalog_channel(
            {
                "channelId": "firstwave",
                "channelName": "1st Wave",
                "channelNumber": 33,
                "images": {"logo": {"url": "/content/dam/sxm-com/channel-logos/Music/1stWave.svg"}},
            }
        )

        self.assertEqual(
            normalized["image_url"],
            "https://www.siriusxm.com/content/dam/sxm-com/channel-logos/Music/1stWave.svg",
        )

    def test_prepare_admin_preset_generates_siriusxm_content_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            raw = (
                '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
                'location="/playback/station/firstwave?preset_play=True" '
                'sourceAccount="source-account-123" isPresetable="true">'
                "<itemName>1st Wave</itemName></ContentItem>"
            )
            try:
                store.set_preset(
                    "speaker-1",
                    {
                        "device_id": "speaker-1",
                        "slot": 1,
                        "source": "SIRIUSXM",
                        "name": "1st Wave",
                        "station_id": "firstwave",
                        "raw_content_item": raw,
                    },
                )
                store.upsert_siriusxm_channel(
                    "classicvinyl",
                    {
                        "name": "Classic Vinyl",
                        "entity_url": "",
                        "stream_url": "https://stream.example/classic.m3u8",
                    },
                )

                preset = prepare_admin_preset(
                    store,
                    "speaker-1",
                    {
                        "source": "SIRIUSXM",
                        "name": "Classic Vinyl",
                        "station_id": "classicvinyl",
                        "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/classic-guid",
                        "image_url": "https://img.example/classic.png",
                    },
                    2,
                )
                channel = store.get_siriusxm_channel("classicvinyl")
            finally:
                store.conn.close()

        self.assertIn('source="SIRIUSXM_EVEREST"', preset["raw_content_item"])
        self.assertIn('location="/playback/station/classicvinyl?preset_play=True"', preset["raw_content_item"])
        self.assertIn('sourceAccount="source-account-123"', preset["raw_content_item"])
        self.assertIn("<itemName>Classic Vinyl</itemName>", preset["raw_content_item"])
        self.assertIn("<containerArt>https://img.example/classic.png</containerArt>", preset["raw_content_item"])
        self.assertEqual(channel["name"], "Classic Vinyl")
        self.assertEqual(
            channel["entity_url"],
            "https://www.siriusxm.com/player/channel-linear/entity/classic-guid",
        )
        self.assertEqual(channel["stream_url"], "https://stream.example/classic.m3u8")

    def test_display_experiment_content_item_points_to_experiment_resolver(self) -> None:
        raw = build_siriusxm_display_experiment_content_item(
            "big80s?preset_play=True",
            "80s on 8",
            "https://img.example/80s.png",
            "source-account-123",
        )

        self.assertIn('source="SIRIUSXM_EVEREST"', raw)
        self.assertIn('location="/experiments/siriusxm/display/playback/station/big80s?preset_play=True"', raw)
        self.assertIn('sourceAccount="source-account-123"', raw)
        self.assertIn("<itemName>80s on 8</itemName>", raw)

    def test_rewrite_siriusxm_preset_content_item_can_toggle_display_experiment(self) -> None:
        preset = {
            "source": "SIRIUSXM",
            "station_id": "big80s?preset_play=True",
            "name": "80s on 8",
            "image_url": "https://img.example/80s.png",
            "raw_content_item": build_siriusxm_content_item(
                "big80s",
                "80s on 8",
                "https://img.example/80s.png",
                "source-account-123",
            ),
        }

        experiment = rewrite_siriusxm_preset_content_item(preset, experiment=True)
        normal = rewrite_siriusxm_preset_content_item(experiment, experiment=False)

        self.assertIn("/experiments/siriusxm/display/playback/station/big80s", experiment["raw_content_item"])
        self.assertIn('sourceAccount="source-account-123"', experiment["raw_content_item"])
        self.assertIn('location="/playback/station/big80s?preset_play=True"', normal["raw_content_item"])

    def test_generated_siriusxm_preset_renders_as_bose_xml(self) -> None:
        preset = {
            "slot": 2,
            "source": "SIRIUSXM",
            "name": "Classic Vinyl",
            "station_id": "classicvinyl",
            "raw_content_item": (
                '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
                'location="/playback/station/classicvinyl?preset_play=True" '
                'sourceAccount="source-account-123" isPresetable="true">'
                "<itemName>Classic Vinyl</itemName></ContentItem>"
            ),
        }

        xml = preset_to_xml(preset)

        self.assertIn("<sourcename>SIRIUSXM_EVEREST</sourcename>", xml)
        self.assertIn("<username>source-account-123</username>", xml)
        self.assertIn('location="/playback/station/classicvinyl?preset_play=True"', xml)

    def test_store_preset_xml_wraps_content_item_for_speaker_store(self) -> None:
        raw = (
            '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
            'location="/playback/station/big80s?preset_play=True" '
            'sourceAccount="source-account-123" isPresetable="true">'
            "<itemName>80s on 8</itemName></ContentItem>"
        )

        xml = store_preset_xml(1, raw)

        self.assertEqual(xml, f'<preset id="1">{raw}</preset>')

    def test_generated_tunein_preset_renders_content_item_for_speaker_store(self) -> None:
        preset = {
            "slot": 1,
            "source": "TUNEIN",
            "name": "The Answer Chicago",
            "station_id": "s17947",
            "image_url": "http://example.test/logo.png",
        }

        xml = preset_to_xml(preset)

        self.assertIn('<ContentItem source="TUNEIN" type="stationurl"', xml)
        self.assertIn('location="/v1/playback/station/s17947"', xml)
        self.assertIn("<itemName>The Answer Chicago</itemName>", xml)

    def test_direct_stream_preset_renders_content_item_for_speaker_store(self) -> None:
        preset = {
            "slot": 5,
            "source": "LOCAL_INTERNET_RADIO",
            "name": "Big 95.5",
            "stream_url": "https://stream.revma.ihrhls.com/zc8731",
            "image_url": "https://i.iheart.com/logo.png",
        }

        xml = preset_to_xml(preset)

        self.assertIn('<ContentItem source="LOCAL_INTERNET_RADIO" type="url"', xml)
        self.assertIn('location="https://stream.revma.ihrhls.com/zc8731"', xml)
        self.assertIn("<itemName>Big 95.5</itemName>", xml)

    def test_build_play_content_item_renders_tunein_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                raw = build_play_content_item(
                    store,
                    "speaker-1",
                    "http://ubuntu.example:8000",
                    {
                        "source": "TUNEIN",
                        "station_id": "s17947",
                        "name": "The Answer Chicago",
                        "image_url": "https://img.example/tunein.png",
                    },
                )
            finally:
                store.conn.close()

        self.assertIn('<ContentItem source="TUNEIN" type="stationurl"', raw)
        self.assertIn('location="/v1/playback/station/s17947"', raw)
        self.assertIn("<itemName>The Answer Chicago</itemName>", raw)
        self.assertIn("<containerArt>https://img.example/tunein.png</containerArt>", raw)

    def test_build_play_content_item_renders_iheart_selection_as_local_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                raw = build_play_content_item(
                    store,
                    "speaker-1",
                    "http://ubuntu.example:8000",
                    {
                        "source": "IHEART",
                        "station_id": "5305",
                        "name": "WGN AM 720",
                        "image_url": "https://img.example/wgn.png",
                    },
                )
            finally:
                store.conn.close()

        self.assertIn('<ContentItem source="LOCAL_INTERNET_RADIO" type="url"', raw)
        self.assertIn("/iheart/stations/5305/station.json", raw)
        self.assertIn("name=WGN+AM+720", raw)
        self.assertIn("<itemName>WGN AM 720</itemName>", raw)

    def test_build_play_content_item_renders_siriusxm_selection_as_native_provider_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.set_preset(
                    "speaker-1",
                    {
                        "slot": 3,
                        "source": "SIRIUSXM",
                        "name": "1st Wave",
                        "station_id": "firstwave",
                        "raw_content_item": build_siriusxm_content_item(
                            "firstwave",
                            "1st Wave",
                            "",
                            "source-account-123",
                        ),
                    },
                )
                raw = build_play_content_item(
                    store,
                    "speaker-1",
                    "http://ubuntu.example:8000",
                    {
                        "source": "SIRIUSXM",
                        "station_id": "big80s",
                        "name": "80s on 8",
                        "entity_url": "https://www.siriusxm.com/player/channel-linear/entity/example",
                        "image_url": "https://img.example/80s.png",
                    },
                )
                channels = store.list_siriusxm_channels()
            finally:
                store.conn.close()

        self.assertIn('<ContentItem source="SIRIUSXM_EVEREST" type="stationurl"', raw)
        self.assertIn('location="/playback/station/big80s?preset_play=True"', raw)
        self.assertIn('sourceAccount="source-account-123"', raw)
        self.assertNotIn("siriusxm/proxy/big80s/playlist.m3u8", raw)
        self.assertEqual(channels, [])

    def test_push_station_to_speaker_selects_content_item(self) -> None:
        speaker = {"device_id": "speaker-1", "ip": "192.168.1.50"}
        raw = '<ContentItem source="TUNEIN" type="stationurl" location="/v1/playback/station/s17947"><itemName>Station</itemName></ContentItem>'

        with patch("soundtouch_bridge.server.select_content_item") as select, patch(
            "soundtouch_bridge.server.now_playing_xml",
            return_value=(
                '<nowPlaying source="TUNEIN"><ContentItem source="TUNEIN" '
                'location="/v1/playback/station/s17947"><itemName>Station</itemName>'
                "</ContentItem><playStatus>PLAY_STATE</playStatus>"
                "<streamType>RADIO_STREAMING</streamType></nowPlaying>"
            ),
        ):
            result = push_station_to_speaker(speaker, raw)

        select.assert_called_once_with("192.168.1.50", raw)
        self.assertEqual(result["attempted"], True)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["message"], "sent to speaker")
        self.assertEqual(result["now_playing"]["source"], "TUNEIN")
        self.assertEqual(result["now_playing"]["location"], "/v1/playback/station/s17947")
        self.assertEqual(result["now_playing"]["item_name"], "Station")

    def test_push_station_to_speaker_does_not_wake_standby_by_default(self) -> None:
        speaker = {"device_id": "speaker-1", "ip": "192.168.1.50"}
        raw = '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" location="/playback/station/big80s?preset_play=True"><itemName>80s on 8</itemName></ContentItem>'

        with patch(
            "soundtouch_bridge.server.now_playing_xml",
            return_value='<nowPlaying source="STANDBY"><playStatus></playStatus></nowPlaying>',
        ), patch("soundtouch_bridge.server.press_speaker_key") as press_key, patch(
            "soundtouch_bridge.server.select_content_item"
        ) as select:
            result = push_station_to_speaker(speaker, raw)

        press_key.assert_not_called()
        select.assert_called_once_with("192.168.1.50", raw)
        self.assertEqual(result["woke_from_standby"], False)
        self.assertEqual(result["before"]["source"], "STANDBY")

    def test_push_station_to_speaker_wakes_standby_when_requested(self) -> None:
        speaker = {"device_id": "speaker-1", "ip": "192.168.1.50"}
        raw = '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" location="/playback/station/big80s?preset_play=True"><itemName>80s on 8</itemName></ContentItem>'
        responses = [
            '<nowPlaying source="STANDBY"><playStatus></playStatus></nowPlaying>',
            '<nowPlaying source="STANDBY"><playStatus></playStatus></nowPlaying>',
            (
                '<nowPlaying source="SIRIUSXM_EVEREST"><ContentItem source="SIRIUSXM_EVEREST" '
                'location="/playback/station/big80s?preset_play=True"><itemName>80s on 8</itemName>'
                "</ContentItem><playStatus>PLAY_STATE</playStatus>"
                "<streamType>RADIO_STREAMING</streamType></nowPlaying>"
            ),
        ]

        with patch("soundtouch_bridge.server.now_playing_xml", side_effect=responses), patch(
            "soundtouch_bridge.server.press_speaker_key"
        ) as press_key, patch("soundtouch_bridge.server.select_content_item") as select, patch(
            "soundtouch_bridge.server.time.sleep"
        ) as sleep:
            result = push_station_to_speaker(speaker, raw, wake=True)

        press_key.assert_called_once_with("192.168.1.50", "POWER")
        select.assert_called_once_with("192.168.1.50", raw)
        self.assertEqual(sleep.call_args_list[0].args[0], 1.2)
        self.assertEqual(sleep.call_args_list[1].args[0], 0.5)
        self.assertEqual(result["woke_from_standby"], True)
        self.assertEqual(result["now_playing"]["source"], "SIRIUSXM_EVEREST")
        self.assertEqual(result["now_playing"]["location"], "/playback/station/big80s?preset_play=True")

    def test_now_playing_snapshot_reads_root_source_when_content_item_is_empty(self) -> None:
        with patch(
            "soundtouch_bridge.server.now_playing_xml",
            return_value='<nowPlaying source="STANDBY"><playStatus></playStatus></nowPlaying>',
        ):
            snapshot = speaker_now_playing_snapshot("192.168.1.50")

        self.assertEqual(snapshot["source"], "STANDBY")
        self.assertEqual(snapshot["location"], "")

    def test_tunein_station_returns_audio_stream_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.set_preset(
                    "speaker-1",
                    {
                        "device_id": "speaker-1",
                        "slot": 1,
                        "source": "TUNEIN",
                        "name": "The Answer Chicago",
                        "station_id": "s17947",
                        "image_url": "http://example.test/logo.png",
                    },
                )
                with patch(
                    "soundtouch_bridge.cloud._resolve_tunein",
                    return_value={"url": "https://stream.example.test/live.mp3", "media_type": "mp3"},
                ):
                    body = tunein_station(store, "s17947", "http://ubuntu.example:8000")
            finally:
                store.conn.close()

        payload = json.loads(body)
        self.assertEqual(payload["name"], "The Answer Chicago")
        self.assertIn("audio", payload)
        self.assertEqual(payload["audio"]["streamUrl"], "https://stream.example.test/live.mp3")
        self.assertEqual(payload["nowPlaying"]["stationName"]["text"], "The Answer Chicago")

    def test_tunein_icy_debug_payload_inspects_resolved_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.set_preset(
                    "speaker-1",
                    {
                        "device_id": "speaker-1",
                        "slot": 5,
                        "source": "TUNEIN",
                        "name": "Metadata Station",
                        "station_id": "s123",
                    },
                )
                with patch(
                    "soundtouch_bridge.cloud._resolve_tunein",
                    return_value={"url": "https://stream.example.test/live.mp3", "media_type": "mp3"},
                ):
                    payload = tunein_icy_debug_payload(
                        store,
                        "s123",
                        "http://ubuntu.example:8000",
                        inspector=lambda url: {
                            "stream_url": url,
                            "icy_metadata_supported": True,
                            "metadata": {"artist": "Artist", "title": "Song"},
                        },
                    )
            finally:
                store.conn.close()

        self.assertEqual(payload["station_id"], "s123")
        self.assertEqual(payload["name"], "Metadata Station")
        self.assertEqual(payload["stream_url"], "https://stream.example.test/live.mp3")
        self.assertTrue(payload["icy"]["icy_metadata_supported"])
        self.assertEqual(payload["icy"]["metadata"]["title"], "Song")

    def test_normalize_tunein_search_station_extracts_station_fields(self) -> None:
        station = normalize_tunein_search_station(
            {
                "type": "audio",
                "text": "The Answer Chicago",
                "subtext": "AM 560",
                "guide_id": "s17947",
                "image": "http://cdn.example/s17947.png",
            }
        )

        self.assertEqual(
            station,
            {
                "station_id": "s17947",
                "name": "The Answer Chicago",
                "description": "AM 560",
                "image_url": "http://cdn.example/s17947.png",
            },
        )

    def test_search_tunein_stations_uses_radiotime_search_endpoint(self) -> None:
        response = {
            "body": [
                {
                    "type": "audio",
                    "text": "The Answer Chicago",
                    "guide_id": "s17947",
                    "image": "http://cdn.example/s17947.png",
                },
                {"type": "link", "text": "Browse", "guide_id": "c1"},
            ]
        }

        with patch("soundtouch_bridge.server.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")

            stations = search_tunein_stations("answer chicago")

        requested_url = urlopen.call_args.args[0]
        self.assertIn("Search.ashx", requested_url)
        self.assertIn("query=answer+chicago", requested_url)
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["station_id"], "s17947")
        self.assertEqual(stations[0]["name"], "The Answer Chicago")

    def test_normalize_iheart_search_station_extracts_station_fields(self) -> None:
        station = normalize_iheart_search_station(
            {
                "id": 8731,
                "name": "Big 95.5",
                "description": "Chicago's New Country",
                "logo": "https://i.iheart.com/v3/re/assets.brands/63fd2da6eb854409caefdcd3",
            }
        )

        self.assertEqual(
            station,
            {
                "station_id": "8731",
                "name": "Big 95.5",
                "description": "Chicago's New Country",
                "image_url": "https://i.iheart.com/v3/re/assets.brands/63fd2da6eb854409caefdcd3",
            },
        )

    def test_search_iheart_stations_uses_catalog_endpoint(self) -> None:
        response = {
            "stations": [
                {
                    "id": 8731,
                    "name": "Big 95.5",
                    "description": "Chicago's New Country",
                    "logo": "https://i.iheart.com/logo.png",
                }
            ]
        }

        with patch("soundtouch_bridge.server.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")

            stations = search_iheart_stations("big 95.5")

        requested_url = urlopen.call_args.args[0]
        self.assertIn("api.iheart.com/api/v1/catalog/searchAll", requested_url)
        self.assertIn("keywords=big+95.5", requested_url)
        self.assertEqual(stations[0]["station_id"], "8731")
        self.assertEqual(stations[0]["name"], "Big 95.5")

    def test_resolve_iheart_stream_url_prefers_secure_shoutcast_stream(self) -> None:
        response = {
            "hits": [
                {
                    "streams": {
                        "secure_hls_stream": "https://stream.example/hls.m3u8",
                        "secure_shoutcast_stream": "https://stream.example/live",
                    }
                }
            ]
        }

        with patch("soundtouch_bridge.server.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode("utf-8")

            stream_url = resolve_iheart_stream_url("8731")

        self.assertEqual(stream_url, "https://stream.example/live")

    def test_iheart_proxy_stream_url_uses_local_server(self) -> None:
        self.assertEqual(
            iheart_proxy_stream_url("http://ubuntu.example:8000", "8731"),
            "http://ubuntu.example:8000/iheart/proxy/8731/stream.aac",
        )

    def test_iheart_station_descriptor_points_to_local_playlist(self) -> None:
        descriptor = iheart_station_descriptor(
            "http://ubuntu.example:8000",
            "8731",
            "Big 95.5",
            "https://i.iheart.com/logo.png",
        )

        self.assertEqual(descriptor["name"], "Big 95.5")
        self.assertEqual(descriptor["streamType"], "liveRadio")
        self.assertEqual(descriptor["imageUrl"], "https://i.iheart.com/logo.png")
        self.assertEqual(descriptor["audio"]["hasPlaylist"], True)
        self.assertEqual(descriptor["audio"]["isRealtime"], True)
        self.assertEqual(descriptor["audio"]["streamUrl"], "http://ubuntu.example:8000/iheart/proxy/8731/playlist.m3u")

    def test_iheart_playlist_is_simple_url_only_m3u(self) -> None:
        self.assertEqual(
            iheart_playlist_body("http://ubuntu.example:8000", "8731"),
            "http://ubuntu.example:8000/iheart/proxy/8731/stream.aac\n",
        )

    def test_iheart_descriptor_url_is_used_as_preset_location(self) -> None:
        self.assertEqual(
            iheart_station_descriptor_url("http://ubuntu.example:8000", "8731"),
            "http://ubuntu.example:8000/iheart/stations/8731/station.json",
        )
        self.assertEqual(
            iheart_playlist_url("http://ubuntu.example:8000", "8731"),
            "http://ubuntu.example:8000/iheart/proxy/8731/playlist.m3u",
        )

    def test_siriusxm_metadata_proxy_debug_identifies_hls_not_icy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_siriusxm_channel("firstwave", {"name": "1st Wave"})
                payload = siriusxm_metadata_proxy_debug_payload(
                    store,
                    "firstwave",
                    "http://ubuntu.example:8000",
                    {"artistName": "The Cure", "trackName": "Just Like Heaven"},
                )
            finally:
                store.conn.close()

        self.assertEqual(payload["station_id"], "firstwave")
        self.assertEqual(payload["transport"], "hls")
        self.assertFalse(payload["icy_metadata_injection_feasible"])
        self.assertIn("ICY metadata is for continuous streams", payload["reason"])
        self.assertEqual(payload["metadata"]["trackName"], "Just Like Heaven")
        self.assertEqual(payload["stream_url"], "http://ubuntu.example:8000/siriusxm/proxy/firstwave/playlist.m3u8")

    def test_siriusxm_preset_overwrite_creates_old_station_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                remember_siriusxm_station_alias(
                    store,
                    {
                        "source": "SIRIUSXM",
                        "station_id": "firstwave?preset_play=True",
                        "raw_content_item": (
                            '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
                            'location="/playback/station/firstwave?preset_play=True">'
                            "<itemName>1st Wave</itemName></ContentItem>"
                        ),
                    },
                    {
                        "source": "SIRIUSXM",
                        "station_id": "classicvinyl",
                        "raw_content_item": (
                            '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
                            'location="/playback/station/classicvinyl?preset_play=True">'
                            "<itemName>Classic Vinyl</itemName></ContentItem>"
                        ),
                    },
                )

                resolved = resolve_siriusxm_station_alias(store, "firstwave")
            finally:
                store.conn.close()

        self.assertEqual(resolved, "classicvinyl")

    def test_tunein_preset_overwrite_creates_cross_source_siriusxm_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                remember_siriusxm_station_alias(
                    store,
                    {
                        "source": "TUNEIN",
                        "station_id": "s17947",
                        "raw_content_item": (
                            '<ContentItem source="TUNEIN" type="stationurl" '
                            'location="/v1/playback/station/s17947">'
                            "<itemName>The Answer Chicago</itemName></ContentItem>"
                        ),
                    },
                    {
                        "source": "SIRIUSXM",
                        "station_id": "big80s",
                        "raw_content_item": (
                            '<ContentItem source="SIRIUSXM_EVEREST" type="stationurl" '
                            'location="/playback/station/big80s?preset_play=True">'
                            "<itemName>80s on 8</itemName></ContentItem>"
                        ),
                    },
                )

                target = store.resolve_station_alias_target("TUNEIN", "s17947")
            finally:
                store.conn.close()

        self.assertEqual(target, {"source": "SIRIUSXM", "station_id": "big80s"})

    def test_saving_real_tunein_preset_clears_stale_cross_source_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_station_alias("TUNEIN", "s17947", "big80s", "SIRIUSXM")

                remember_siriusxm_station_alias(
                    store,
                    {"source": "TUNEIN", "station_id": "s297678"},
                    {"source": "TUNEIN", "station_id": "s17947"},
                )

                target = store.resolve_station_alias_target("TUNEIN", "s17947")
            finally:
                store.conn.close()

        self.assertEqual(target, {"source": "TUNEIN", "station_id": "s17947"})

    def test_tunein_station_ignores_stale_cross_source_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_station_alias("TUNEIN", "s17947", "big80s", "SIRIUSXM")
                with patch(
                    "soundtouch_bridge.cloud._resolve_tunein",
                    return_value={"url": "https://stream.example.test/live.mp3", "media_type": "mp3"},
                ):
                    body = tunein_station(store, "s17947", "http://ubuntu.example:8000")
            finally:
                store.conn.close()

        payload = json.loads(body)
        self.assertEqual(payload["name"], "s17947")
        self.assertEqual(payload["audio"]["streamUrl"], "https://stream.example.test/live.mp3")
        self.assertNotIn("SIRIUSXM", json.dumps(payload))

    def test_tunein_handler_ignores_stale_cross_source_alias(self) -> None:
        class FakeRequest:
            def __init__(self, store: Store) -> None:
                self.server = SimpleNamespace(store=store, public_base="http://ubuntu.example:8000")
                self.body = b""
                self.content_type = ""

            def send_bytes(self, body: bytes, content_type: str) -> None:
                self.body = body
                self.content_type = content_type

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_station_alias("TUNEIN", "s17947", "big80s", "SIRIUSXM")
                req = FakeRequest(store)
                with patch(
                    "soundtouch_bridge.cloud._resolve_tunein",
                    return_value={"url": "https://stream.example.test/live.mp3", "media_type": "mp3"},
                ):
                    handle_tunein_station(req, "s17947")
            finally:
                store.conn.close()

        payload = json.loads(req.body)
        self.assertEqual(req.content_type, "application/json")
        self.assertEqual(payload["audio"]["streamUrl"], "https://stream.example.test/live.mp3")
        self.assertNotIn("SIRIUSXM", json.dumps(payload))

    def test_pressed_preset_slot_reads_scmudc_preset_events(self) -> None:
        body = json.dumps(
            {
                "payload": {
                    "events": [
                        {
                            "type": "preset-pressed",
                            "data": {"buttonId": "PRESET_1", "origin": "ir-remote"},
                        }
                    ]
                }
            }
        )

        self.assertEqual(pressed_preset_slot(body), 1)

    def test_pressed_preset_slot_ignores_non_preset_events(self) -> None:
        body = json.dumps({"payload": {"events": [{"type": "power-pressed", "data": {"buttonId": "POWER"}}]}})

        self.assertEqual(pressed_preset_slot(body), 0)

    def test_preset_press_does_not_send_select_override(self) -> None:
        body = json.dumps(
            {
                "payload": {
                    "events": [
                        {
                            "type": "preset-pressed",
                            "data": {"buttonId": "PRESET_1"},
                        }
                    ]
                }
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_speaker(
                    {
                        "device_id": "speaker-1",
                        "ip": "192.168.10.22",
                        "name": "STS1",
                        "model": "",
                        "firmware": "",
                        "account_id": "",
                        "cloud_url": "",
                        "migrated": 1,
                    }
                )
                store.set_preset(
                    "speaker-1",
                    {
                        "device_id": "speaker-1",
                        "slot": 1,
                        "source": "SIRIUSXM",
                        "name": "80s on 8",
                        "station_id": "big80s?preset_play=True",
                        "raw_content_item": build_siriusxm_content_item("big80s", "80s on 8"),
                    },
                )

                with patch("soundtouch_bridge.server.threading.Thread") as thread:
                    maybe_override_siriusxm_preset_press(store, "speaker-1", body)
            finally:
                store.conn.close()

        thread.assert_not_called()

    def test_admin_ui_exposes_siriusxm_channel_picker(self) -> None:
        self.assertIn("SoundTouch Bridge", ADMIN_HTML)
        self.assertIn('href="/play"', ADMIN_HTML)
        self.assertIn("Missing /etc/soundtouch-bridge/siriusxm.env", ADMIN_HTML)
        self.assertIn("siriusChannelSearch", ADMIN_HTML)
        self.assertIn("loadSiriusCatalog", ADMIN_HTML)
        self.assertIn("data-action=\"pick-sirius\"", ADMIN_HTML)
        self.assertIn("Use Channel", ADMIN_HTML)

    def test_siriusxm_default_env_file_uses_soundtouch_bridge_path(self) -> None:
        self.assertEqual(DEFAULT_ENV_FILE, "/etc/soundtouch-bridge/siriusxm.env")

    def test_server_prefers_soundtouch_bridge_env_file_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            env_path = os.path.join(tmp, "new.env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("SIRIUSXM_USERNAME='new@example.com'\nSIRIUSXM_PASSWORD='new-pass'\n")

            with patch.dict(
                os.environ,
                {"SOUNDTOUCH_BRIDGE_SIRIUSXM_ENV_FILE": env_path},
            ):
                server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                self.assertEqual(server.siriusxm.credentials.username, "new@example.com")
                self.assertEqual(server.siriusxm.credentials.password, "new-pass")
            finally:
                server.server_close()
                store.conn.close()

    def test_admin_ui_exposes_tunein_station_picker(self) -> None:
        self.assertIn("tuneinStationSearch", ADMIN_HTML)
        self.assertIn("searchTuneInStations", ADMIN_HTML)
        self.assertIn("data-action=\"pick-tunein\"", ADMIN_HTML)
        self.assertIn("Use Station", ADMIN_HTML)

    def test_admin_ui_exposes_iheart_station_picker(self) -> None:
        self.assertIn("iheartStationSearch", ADMIN_HTML)
        self.assertIn("searchIHeartStations", ADMIN_HTML)
        self.assertIn("data-action=\"pick-iheart\"", ADMIN_HTML)
        self.assertIn("Use iHeart", ADMIN_HTML)
        self.assertIn("Preserved iHeart preset.", ADMIN_HTML)

    def test_play_page_exposes_isolated_experiment_station_browser(self) -> None:
        self.assertIn("Experimental Play Lab", PLAY_HTML)
        self.assertIn("Does not write presets or aliases", PLAY_HTML)
        self.assertIn("sourceTabs", PLAY_HTML)
        self.assertIn("station-grid", PLAY_HTML)
        self.assertIn("data-source=\"SIRIUSXM\"", PLAY_HTML)
        self.assertIn("pushStation", PLAY_HTML)
        self.assertIn("wakeSpeaker", PLAY_HTML)
        self.assertIn("/api/experiments/play/speakers/", PLAY_HTML)
        self.assertNotIn("playSlot", PLAY_HTML)
        self.assertIn(">Try Select</button>", PLAY_HTML)

    def test_tunein_search_route_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                matched = any(
                    method == "GET" and pattern.fullmatch("/api/tunein/search")
                    for method, pattern, _handler in server.routes
                )
            finally:
                server.server_close()
                store.conn.close()

        self.assertTrue(matched)

    def test_iheart_routes_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                paths = [
                    "/api/iheart/search",
                    "/api/iheart/stations/8731/stream",
                    "/iheart/proxy/8731/stream",
                    "/iheart/proxy/8731/stream.aac",
                    "/iheart/proxy/8731/playlist.m3u",
                    "/iheart/stations/8731/station.json",
                ]
                matched = {
                    path: any(method == "GET" and pattern.fullmatch(path) for method, pattern, _handler in server.routes)
                    for path in paths
                }
            finally:
                server.server_close()
                store.conn.close()

        self.assertEqual(
            matched,
            {
                "/api/iheart/search": True,
                "/api/iheart/stations/8731/stream": True,
                "/iheart/proxy/8731/stream": True,
                "/iheart/proxy/8731/stream.aac": True,
                "/iheart/proxy/8731/playlist.m3u": True,
                "/iheart/stations/8731/station.json": True,
            },
        )

    def test_play_routes_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                paths = ["/play", "/api/experiments/play/speakers/000C8A8DAF9E/select"]
                matched = {
                    path: any(
                        method == ("POST" if path.startswith("/api/") else "GET")
                        and pattern.fullmatch(path)
                        for method, pattern, _handler in server.routes
                    )
                    for path in paths
                }
            finally:
                server.server_close()
                store.conn.close()

        self.assertEqual(matched, {"/play": True, "/api/experiments/play/speakers/000C8A8DAF9E/select": True})

    def test_siriusxm_descriptor_route_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                path = "/siriusxm/stations/big80s/station.json"
                matched = any(method == "GET" and pattern.fullmatch(path) for method, pattern, _handler in server.routes)
            finally:
                server.server_close()
                store.conn.close()

        self.assertTrue(matched)

    def test_admin_ui_persists_save_confirmation_after_reload(self) -> None:
        self.assertIn("cardNotices", ADMIN_HTML)
        self.assertIn("setCardNotice(speaker.device_id, slot, text, kind)", ADMIN_HTML)
        self.assertIn("Saved and stored on speaker", ADMIN_HTML)
        self.assertIn("SiriusXM preset stored.", ADMIN_HTML)
        self.assertIn("SiriusXM display metadata experiment active.", ADMIN_HTML)
        self.assertNotIn("Imported SiriusXM preset preserved from the speaker", ADMIN_HTML)

    def test_siriusxm_station_uses_longer_buffer_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            store.upsert_siriusxm_channel("firstwave", {"name": "1st Wave"})

            payload = json.loads(siriusxm_station(store, "firstwave", "http://ubuntu.example:8000"))
            store.conn.close()

        self.assertEqual(payload["audio"]["maxTimeout"], 180)
        self.assertEqual(payload["audio"]["streams"][0]["bufferingTimeout"], 120)
        self.assertEqual(payload["audio"]["streams"][0]["connectingTimeout"], 20)

    def test_siriusxm_station_can_include_now_playing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_siriusxm_channel("firstwave", {"name": "1st Wave"})

                payload = json.loads(
                    siriusxm_station(
                        store,
                        "firstwave",
                        "http://ubuntu.example:8000",
                        {
                            "stationName": "1st Wave",
                            "trackName": "Just Like Heaven",
                            "artistName": "The Cure",
                            "albumName": "Kiss Me, Kiss Me, Kiss Me",
                            "imageUrl": "https://img.example/cure.jpg",
                        },
                    )
                )
            finally:
                store.conn.close()

        self.assertEqual(payload["nowPlaying"]["track"]["text"], "Just Like Heaven")
        self.assertEqual(payload["nowPlaying"]["artist"]["text"], "The Cure")
        self.assertEqual(payload["nowPlaying"]["album"]["text"], "Kiss Me, Kiss Me, Kiss Me")
        self.assertEqual(payload["nowPlaying"]["stationName"]["text"], "1st Wave")
        self.assertEqual(payload["nowPlaying"]["art"]["text"], "https://img.example/cure.jpg")

    def test_siriusxm_display_experiment_repeats_metadata_in_iheart_like_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            try:
                store.upsert_siriusxm_channel("firstwave", {"name": "1st Wave"})

                payload = json.loads(
                    siriusxm_station_display_experiment(
                        store,
                        "firstwave",
                        "http://ubuntu.example:8000",
                        {
                            "stationName": "1st Wave",
                            "trackName": "Just Like Heaven",
                            "artistName": "The Cure",
                            "albumName": "Kiss Me, Kiss Me, Kiss Me",
                            "imageUrl": "https://img.example/cure.jpg",
                        },
                    )
                )
            finally:
                store.conn.close()

        self.assertEqual(payload["_meta"]["resolver"], "soundtouch-bridge-siriusxm-display-experiment")
        self.assertEqual(payload["stationName"]["text"], "1st Wave")
        self.assertEqual(payload["track"]["text"], "Just Like Heaven")
        self.assertEqual(payload["artist"]["text"], "The Cure")
        self.assertEqual(payload["nowPlaying"]["source"], "SIRIUSXM_EVEREST")
        self.assertEqual(payload["nowPlaying"]["track"]["text"], "Just Like Heaven")
        self.assertEqual(payload["contentItem"]["source"], "SIRIUSXM_EVEREST")
        self.assertEqual(
            payload["audio"]["streamUrl"],
            "http://ubuntu.example:8000/siriusxm/proxy/firstwave/metadata-playlist.m3u8",
        )
        self.assertEqual(payload["_meta"]["hlsTimedMetadata"], "id3-prepend")

    def test_siriusxm_display_experiment_route_accepts_adapter_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))
            server = SoundTouchBridgeServer(("127.0.0.1", 0), store, "http://ubuntu.example:8000")
            try:
                path = "/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/experiments/siriusxm/display/playback/station/big80s"
                matched = any(method == "GET" and pattern.fullmatch(path) for method, pattern, _handler in server.routes)
            finally:
                server.server_close()
                store.conn.close()

        self.assertTrue(matched)

    def test_siriusxm_now_playing_payload_uses_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))

            body = siriusxm_now_playing(
                store,
                "firstwave",
                {
                    "stationName": "1st Wave",
                    "channelName": "1st Wave",
                    "trackName": "Just Like Heaven",
                    "artistName": "The Cure",
                    "albumName": "Kiss Me, Kiss Me, Kiss Me",
                    "imageUrl": "https://img.example/cure.jpg",
                },
            ).decode("utf-8")
            store.conn.close()

        payload = json.loads(body)
        self.assertEqual(payload["trackName"], "Just Like Heaven")
        self.assertEqual(payload["artistName"], "The Cure")
        self.assertEqual(payload["albumName"], "Kiss Me, Kiss Me, Kiss Me")
        self.assertEqual(payload["containerArt"], "https://img.example/cure.jpg")

    def test_siriusxm_now_playing_payload_includes_bose_nested_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "state.sqlite3"))

            body = siriusxm_now_playing(
                store,
                "firstwave",
                {
                    "stationName": "1st Wave",
                    "trackName": "Just Like Heaven",
                    "artistName": "The Cure",
                    "albumName": "Kiss Me, Kiss Me, Kiss Me",
                    "imageUrl": "https://img.example/cure.jpg",
                },
            ).decode("utf-8")
            store.conn.close()

        payload = json.loads(body)
        self.assertEqual(payload["track"]["text"], "Just Like Heaven")
        self.assertEqual(payload["artist"]["text"], "The Cure")
        self.assertEqual(payload["album"]["text"], "Kiss Me, Kiss Me, Kiss Me")
        self.assertEqual(payload["art"]["artImageStatus"], "IMAGE_PRESENT")
        self.assertEqual(payload["art"]["text"], "https://img.example/cure.jpg")

    def test_siriusxm_error_sanitizer_removes_secrets_and_queries(self) -> None:
        message = (
            "HTTP 403 https://live.example.test/playlist.m3u8?token=abc "
            "for listener@example.com using secret password"
        )

        redacted = sanitize_siriusxm_error(
            message,
            SiriusXmCredentials("listener@example.com", "secret password"),
        )

        self.assertNotIn("token=abc", redacted)
        self.assertNotIn("listener@example.com", redacted)
        self.assertNotIn("secret password", redacted)
        self.assertIn("[redacted-url]", redacted)


def make_cookie(name: str, value: str) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain="player.siriusxm.com",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


if __name__ == "__main__":
    unittest.main()


