from __future__ import annotations

import unittest

from sixback_ubuntu.sixback_ubuntu.server import (
    SIRIUSXM_HLS_AES_KEY,
    is_siriusxm_hls_key,
    rewrite_hls_playlist,
    should_capture_siriusxm_playlist_success,
    should_capture_siriusxm_fetch_success,
    summarize_hls_playlist,
    trim_hls_playlist,
)


class FakeServer:
    public_base = "http://ubuntu.example:8000"

    def __init__(self) -> None:
        self.siriusxm_proxy_urls: dict[str, str] = {}


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

    def test_rewrite_hls_playlist_uses_root_relative_token_urls(self) -> None:
        server = FakeServer()
        body = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-KEY:METHOD=AES-128,URI=\"https://example.test/key\"",
                "#EXTINF:9.75,",
                "audio/segment.aac",
            ]
        )

        rewritten = rewrite_hls_playlist(body, "https://example.test/live/playlist.m3u8", server)

        self.assertNotIn("https://example.test/key", rewritten)
        self.assertNotIn("audio/segment.aac", rewritten)
        self.assertNotIn("http://ubuntu.example:8000", rewritten)
        self.assertEqual(rewritten.count("/siriusxm/proxy/fetch/"), 2)
        self.assertEqual(
            sorted(server.siriusxm_proxy_urls.values()),
            [
                "https://example.test/key",
                "https://example.test/live/audio/segment.aac",
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


if __name__ == "__main__":
    unittest.main()
