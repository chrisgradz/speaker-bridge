from __future__ import annotations

import unittest

from sixback_ubuntu.sixback_ubuntu.server import (
    rewrite_hls_playlist,
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


if __name__ == "__main__":
    unittest.main()
