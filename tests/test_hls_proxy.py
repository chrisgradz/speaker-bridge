from __future__ import annotations

import unittest

from soundtouch_bridge.server import (
    SIRIUSXM_HLS_AES_KEY,
    build_id3_text_tag,
    cached_fetch_siriusxm_url,
    handle_siriusxm_proxy_fetch,
    inject_id3_metadata,
    is_siriusxm_hls_key,
    normalize_siriusxm_channel,
    report_response,
    rewrite_hls_playlist,
    should_capture_siriusxm_playlist_success,
    should_capture_siriusxm_fetch_success,
    summarize_hls_playlist,
    trim_hls_playlist,
    validate_siriusxm_proxy_url,
)
from soundtouch_bridge.icy import MAX_ICY_METAINT, inspect_icy_stream, parse_icy_metadata_block


class FakeServer:
    public_base = "http://ubuntu.example:8000"

    def __init__(self) -> None:
        self.siriusxm_proxy_urls: dict[str, str] = {}


class FakeProxyRequest:
    def __init__(self, path: str) -> None:
        self.path = path
        self.server = FakeServer()
        self.sent_json = {}
        self.sent_status = 0

    def send_json(self, body, status: int = 200) -> None:
        self.sent_json = body
        self.sent_status = status


class HlsProxyTests(unittest.TestCase):
    def test_trim_hls_playlist_keeps_small_live_window(self) -> None:
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:10",
                "#EXT-X-MEDIA-SEQUENCE:100",
                "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"",
                *[
                    line
                    for index in range(10)
                    for line in (
                        f"#EXT-X-PROGRAM-DATE-TIME:2026-06-19T00:00:{index:02d}.000+00:00",
                        "#EXTINF:9.75,",
                        f"segment-{index}.aac",
                    )
                ],
            ]
        )

        trimmed = trim_hls_playlist(body, max_segments=3)

        self.assertIn("#EXT-X-MEDIA-SEQUENCE:107", trimmed)
        self.assertNotIn("segment-6.aac", trimmed)
        self.assertIn("segment-7.aac", trimmed)
        self.assertIn("segment-8.aac", trimmed)
        self.assertIn("segment-9.aac", trimmed)
        summary = summarize_hls_playlist("firstwave", trimmed)
        self.assertIn("keys=1", summary)
        self.assertIn("media=3", summary)
        lines = trimmed.splitlines()
        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXTINF:"):
                self.assertFalse(lines[index + 1].startswith("#"))

    def test_trim_hls_playlist_default_keeps_larger_live_window(self) -> None:
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:10",
                "#EXT-X-MEDIA-SEQUENCE:200",
                *[
                    line
                    for index in range(15)
                    for line in (
                        "#EXTINF:9.75,",
                        f"segment-{index}.aac",
                    )
                ],
            ]
        )

        trimmed = trim_hls_playlist(body)

        self.assertIn("#EXT-X-MEDIA-SEQUENCE:203", trimmed)
        self.assertNotIn("segment-2.aac", trimmed)
        self.assertIn("segment-3.aac", trimmed)
        self.assertIn("segment-14.aac", trimmed)
        self.assertEqual(summarize_hls_playlist("firstwave", trimmed).count("media=12"), 1)

    def test_rewrite_hls_playlist_uses_root_relative_token_urls(self) -> None:
        server = FakeServer()
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-KEY:METHOD=AES-128,URI=\"https://siriusxm-priprodlive.akamaized.net/key\"",
                "#EXTINF:9.75,",
                "audio/segment.aac",
            ]
        )

        rewritten = rewrite_hls_playlist(body, "https://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8", server)

        self.assertNotIn("https://siriusxm-priprodlive.akamaized.net/key", rewritten)
        self.assertNotIn("audio/segment.aac", rewritten)
        self.assertNotIn("http://ubuntu.example:8000", rewritten)
        self.assertEqual(rewritten.count("/siriusxm/proxy/fetch/"), 2)
        self.assertEqual(
            sorted(server.siriusxm_proxy_urls.values()),
            [
                "https://siriusxm-priprodlive.akamaized.net/key",
                "https://siriusxm-priprodlive.akamaized.net/live/audio/segment.aac",
            ],
        )

    def test_rewrite_hls_playlist_carries_playlist_auth_query_to_relative_media(self) -> None:
        server = FakeServer()
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:9.75,",
                "audio/segment.aac",
                "#EXTINF:9.75,",
                "audio/already.aac?token=own",
            ]
        )

        rewrite_hls_playlist(
            body,
            "https://siriusxm-priprodlive.akamaized.net/firstwave/live.m3u8?token=abc&gupId=gup-123&consumer=k2",
            server,
        )

        self.assertEqual(
            sorted(server.siriusxm_proxy_urls.values()),
            [
                "https://siriusxm-priprodlive.akamaized.net/firstwave/audio/already.aac?token=own",
                "https://siriusxm-priprodlive.akamaized.net/firstwave/audio/segment.aac?token=abc&gupId=gup-123&consumer=k2",
            ],
        )

    def test_rewrite_hls_playlist_can_route_unencrypted_media_through_metadata_proxy(self) -> None:
        server = FakeServer()
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:9.75,",
                "audio/segment.aac",
            ]
        )

        rewritten = rewrite_hls_playlist(
            body,
            "https://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8",
            server,
            station_id="big80s",
            inject_metadata=True,
        )

        self.assertNotIn("/siriusxm/proxy/fetch/", rewritten)
        self.assertIn("/siriusxm/proxy/meta/big80s/", rewritten)
        self.assertEqual(len(server.siriusxm_proxy_urls), 1)

    def test_rewrite_hls_playlist_keeps_encrypted_media_on_plain_proxy(self) -> None:
        server = FakeServer()
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"",
                "#EXTINF:9.75,",
                "audio/segment.aac",
            ]
        )

        rewritten = rewrite_hls_playlist(
            body,
            "https://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8",
            server,
            station_id="big80s",
            inject_metadata=True,
            metadata={"trackName": "Mickey", "artistName": "Toni Basil"},
        )

        self.assertIn("/siriusxm/proxy/fetch/", rewritten)
        self.assertNotIn("/siriusxm/proxy/meta/big80s/", rewritten)
        self.assertIn("#EXTINF:9.75,Toni Basil - Mickey", rewritten)
        self.assertEqual(len(server.siriusxm_proxy_urls), 2)

    def test_build_id3_text_tag_contains_title_artist_and_album_frames(self) -> None:
        tag = build_id3_text_tag(
            {
                "trackName": "Just Like Heaven",
                "artistName": "The Cure",
                "albumName": "Kiss Me, Kiss Me, Kiss Me",
            }
        )

        self.assertTrue(tag.startswith(b"ID3\x04\x00\x00"))
        self.assertIn(b"TIT2", tag)
        self.assertIn(b"Just Like Heaven", tag)
        self.assertIn(b"TPE1", tag)
        self.assertIn(b"The Cure", tag)
        self.assertIn(b"TALB", tag)

    def test_inject_id3_metadata_prepends_tag_before_aac_segment(self) -> None:
        segment = b"\xff\xf1aac-data"

        injected = inject_id3_metadata(segment, {"trackName": "Mickey", "artistName": "Toni Basil"})

        self.assertTrue(injected.startswith(b"ID3"))
        self.assertTrue(injected.endswith(segment))

    def test_siriusxm_key_detection(self) -> None:
        self.assertEqual(len(SIRIUSXM_HLS_AES_KEY), 16)
        self.assertTrue(
            is_siriusxm_hls_key(
                "https://api.edge-gateway.siriusxm.com/playback/key/v1/00000000-0000-0000-0000-000000000000"
            )
        )
        self.assertTrue(
            is_siriusxm_hls_key(
                "https://siriusxm-priprodlive.akamaized.net/AAC_Data/firstwave/HLS_firstwave_256k_v3/key/1"
            )
        )
        self.assertFalse(is_siriusxm_hls_key("https://api.edge-gateway.siriusxm.com/other/key"))
        self.assertFalse(is_siriusxm_hls_key("https://example.test/playback/key/v1/foo"))

    def test_siriusxm_proxy_url_validation_allows_known_stream_hosts(self) -> None:
        self.assertEqual(
            validate_siriusxm_proxy_url("https://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8"),
            "https://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8",
        )
        self.assertEqual(
            validate_siriusxm_proxy_url(
                "https://live-akc-prod-device.streaming.siriusxm.com/v1/session/sec-1/AAC_Data/firstwave/segment.aac"
            ),
            "https://live-akc-prod-device.streaming.siriusxm.com/v1/session/sec-1/AAC_Data/firstwave/segment.aac",
        )

    def test_siriusxm_proxy_url_validation_rejects_arbitrary_and_local_urls(self) -> None:
        for url in (
            "http://siriusxm-priprodlive.akamaized.net/live/playlist.m3u8",
            "https://example.test/live/playlist.m3u8",
            "https://127.0.0.1/private.m3u8",
            "https://localhost/private.m3u8",
            "https://user:pass@siriusxm-priprodlive.akamaized.net/live/playlist.m3u8",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_siriusxm_proxy_url(url)

    def test_normalize_siriusxm_channel_rejects_arbitrary_stream_urls(self) -> None:
        with self.assertRaises(ValueError):
            normalize_siriusxm_channel({"name": "bad", "stream_url": "http://127.0.0.1:9999/private.m3u8"})
        with self.assertRaises(ValueError):
            normalize_siriusxm_channel({"name": "bad", "stream_url": "https://example.invalid/audio.m3u8"})

    def test_normalize_siriusxm_channel_accepts_siriusxm_stream_urls(self) -> None:
        channel = normalize_siriusxm_channel(
            {
                "name": "1st Wave",
                "stream_url": "https://siriusxm-priprodlive.akamaized.net/AAC_Data/firstwave/live.m3u8",
            }
        )

        self.assertEqual(
            channel["stream_url"],
            "https://siriusxm-priprodlive.akamaized.net/AAC_Data/firstwave/live.m3u8",
        )

    def test_siriusxm_proxy_fetch_rejects_query_url_fallback(self) -> None:
        req = FakeProxyRequest("/siriusxm/proxy/fetch?url=https://siriusxm-priprodlive.akamaized.net/live.m3u8")

        handle_siriusxm_proxy_fetch(req)

        self.assertEqual(req.sent_status, 400)
        self.assertEqual(req.sent_json, {"error": "missing_proxy_token"})

    def test_siriusxm_success_capture_policy_skips_noisy_stream_fetches(self) -> None:
        self.assertFalse(should_capture_siriusxm_playlist_success())
        self.assertFalse(
            should_capture_siriusxm_fetch_success(
                "https://api.edge-gateway.siriusxm.com/playback/key/v1/00000000-0000-0000-0000-000000000000"
            )
        )
        self.assertFalse(
            should_capture_siriusxm_fetch_success(
                "https://live-akc-prod-device.streaming.siriusxm.com/v1/session/sec-1/AAC_Data/firstwave/segment.aac"
            )
        )
        self.assertTrue(should_capture_siriusxm_fetch_success("https://example.test/other-resource"))

    def test_report_response_slows_periodic_reports(self) -> None:
        self.assertEqual(report_response(), {"nextReportIn": 1800})

    def test_cached_fetch_reuses_short_lived_upstream_response(self) -> None:
        calls = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            return f"body-{len(calls)}".encode("utf-8")

        cache: dict[str, tuple[float, bytes]] = {}

        first = cached_fetch_siriusxm_url("https://example.test/live.m3u8", cache, now=100.0, fetcher=fetcher)
        second = cached_fetch_siriusxm_url("https://example.test/live.m3u8", cache, now=104.0, fetcher=fetcher)
        third = cached_fetch_siriusxm_url("https://example.test/live.m3u8", cache, now=111.0, fetcher=fetcher)

        self.assertEqual(first, b"body-1")
        self.assertEqual(second, b"body-1")
        self.assertEqual(third, b"body-2")
        self.assertEqual(calls, ["https://example.test/live.m3u8", "https://example.test/live.m3u8"])

    def test_parse_icy_metadata_block_extracts_artist_and_title(self) -> None:
        metadata = parse_icy_metadata_block(b"StreamTitle='The Cure - Just Like Heaven';StreamUrl='';")

        self.assertEqual(metadata["stream_title"], "The Cure - Just Like Heaven")
        self.assertEqual(metadata["artist"], "The Cure")
        self.assertEqual(metadata["title"], "Just Like Heaven")

    def test_inspect_icy_stream_reads_first_metadata_packet(self) -> None:
        class FakeHeaders:
            def get(self, name: str, default: str = "") -> str:
                return {"icy-metaint": "4", "content-type": "audio/mpeg"}.get(name.lower(), default)

        class FakeResponse:
            headers = FakeHeaders()
            status = 200

            def __init__(self) -> None:
                raw_metadata = b"StreamTitle='Berlin - The Metro';"
                padded = raw_metadata + (b"\0" * (16 - len(raw_metadata) % 16))
                self.body = b"DATA" + bytes([len(padded) // 16]) + padded

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    size = len(self.body)
                chunk = self.body[:size]
                self.body = self.body[size:]
                return chunk

        def opener(request, timeout=8):
            return FakeResponse()

        result = inspect_icy_stream("https://stream.example.test/live.mp3", opener=opener)

        self.assertTrue(result["icy_metadata_supported"])
        self.assertEqual(result["metadata"]["artist"], "Berlin")
        self.assertEqual(result["metadata"]["title"], "The Metro")
        self.assertEqual(result["content_type"], "audio/mpeg")

    def test_inspect_icy_stream_skips_empty_metadata_packets(self) -> None:
        class FakeHeaders:
            def get(self, name: str, default: str = "") -> str:
                return {"icy-metaint": "4", "content-type": "audio/mpeg"}.get(name.lower(), default)

        class FakeResponse:
            headers = FakeHeaders()
            status = 200

            def __init__(self) -> None:
                raw_metadata = b"StreamTitle='Flying Lizards - Money';"
                padded = raw_metadata + (b"\0" * (16 - len(raw_metadata) % 16))
                self.body = b"DATA" + b"\0" + b"MORE" + bytes([len(padded) // 16]) + padded

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    size = len(self.body)
                chunk = self.body[:size]
                self.body = self.body[size:]
                return chunk

        result = inspect_icy_stream("https://stream.example.test/live.mp3", opener=lambda request, timeout=8: FakeResponse())

        self.assertEqual(result["metadata_packets_checked"], 2)
        self.assertEqual(result["metadata"]["artist"], "Flying Lizards")
        self.assertEqual(result["metadata"]["title"], "Money")

    def test_inspect_icy_stream_rejects_huge_metadata_interval_before_reading(self) -> None:
        class FakeHeaders:
            def get(self, name: str, default: str = "") -> str:
                return {"icy-metaint": str(MAX_ICY_METAINT + 1), "content-type": "audio/mpeg"}.get(
                    name.lower(), default
                )

        class FakeResponse:
            headers = FakeHeaders()
            status = 200

            def __init__(self) -> None:
                self.read_sizes: list[int] = []
                self.closed = False

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return b""

            def close(self) -> None:
                self.closed = True

        response = FakeResponse()

        result = inspect_icy_stream("https://stream.example.test/live.mp3", opener=lambda request, timeout=8: response)

        self.assertEqual(result["error"], "unsupported_metadata_interval")
        self.assertEqual(result["icy_metaint"], MAX_ICY_METAINT + 1)
        self.assertEqual(result["max_icy_metaint"], MAX_ICY_METAINT)
        self.assertFalse(result["icy_metadata_supported"])
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)

    def test_inspect_icy_stream_rejects_oversized_metadata_block(self) -> None:
        class FakeHeaders:
            def get(self, name: str, default: str = "") -> str:
                return {"icy-metaint": "4", "content-type": "audio/mpeg"}.get(name.lower(), default)

        class FakeResponse:
            headers = FakeHeaders()
            status = 200

            def __init__(self) -> None:
                self.body = b"DATA" + bytes([255])
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                chunk = self.body[:size]
                self.body = self.body[size:]
                return chunk

        response = FakeResponse()

        result = inspect_icy_stream(
            "https://stream.example.test/live.mp3",
            opener=lambda request, timeout=8: response,
            max_metadata_block=1024,
        )

        self.assertEqual(result["error"], "unsupported_metadata_block")
        self.assertEqual(result["metadata_length"], 4080)
        self.assertEqual(result["max_metadata_length"], 1024)
        self.assertEqual(response.read_sizes, [4, 1])


if __name__ == "__main__":
    unittest.main()

