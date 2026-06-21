from __future__ import annotations

import unittest

from soundtouch_bridge.speaker import validate_bridge_base_url, validate_speaker_host


class SecurityHardeningTests(unittest.TestCase):
    def test_speaker_host_validation_accepts_plain_public_and_lan_hosts(self) -> None:
        self.assertEqual(validate_speaker_host("192.168.10.22"), "192.168.10.22")
        self.assertEqual(validate_speaker_host("203.0.113.22"), "203.0.113.22")
        self.assertEqual(validate_speaker_host("speaker.example.com"), "speaker.example.com")

    def test_speaker_host_validation_rejects_url_shaped_values(self) -> None:
        for host in (
            "http://192.168.10.22",
            "192.168.10.22:8090",
            "127.0.0.1:1/anything?",
            "example.com@127.0.0.1",
            "192.168.10.22/info",
            "192.168.10.22?x=1",
            "192.168.10.22\nsys reboot",
        ):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    validate_speaker_host(host)

    def test_bridge_base_url_validation_accepts_normal_http_urls(self) -> None:
        self.assertEqual(validate_bridge_base_url("http://192.168.10.230:8000/"), "http://192.168.10.230:8000")
        self.assertEqual(validate_bridge_base_url("https://bose1.example.com:8000"), "https://bose1.example.com:8000")

    def test_bridge_base_url_validation_rejects_control_characters_and_extra_parts(self) -> None:
        for base_url in (
            "http://bridge.local\nsys reboot",
            "http://user:pass@bridge.local:8000",
            "ftp://bridge.local:8000",
            "http://bridge.local:8000?x=1",
            "http://bridge.local:8000#frag",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    validate_bridge_base_url(base_url)


if __name__ == "__main__":
    unittest.main()
