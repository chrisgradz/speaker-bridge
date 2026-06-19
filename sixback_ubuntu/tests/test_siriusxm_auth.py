from __future__ import annotations

import os
import tempfile
import unittest
import urllib.error
import json
from io import BytesIO
from http.cookiejar import Cookie
from types import SimpleNamespace

from sixback_ubuntu.sixback_ubuntu.db import Store
from sixback_ubuntu.sixback_ubuntu.cloud import siriusxm_now_playing, siriusxm_station
from sixback_ubuntu.sixback_ubuntu.siriusxm import (
    SiriusXmCredentials,
    SiriusXmSession,
    extract_entity_id,
    extract_now_playing,
    extract_stream_url,
    load_credentials,
    redact_secret,
    should_refresh_stream,
)
from sixback_ubuntu.sixback_ubuntu.server import (
    handle_siriusxm_now_playing_debug,
    resolve_siriusxm_stream_url,
    sanitize_siriusxm_error,
)


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
