import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import url_to_mp3


class YoutubeAccessOptionsTests(unittest.TestCase):
    """Cookies, proxy and player clients reach yt-dlp only when configured."""

    def test_nothing_configured_adds_no_options(self):
        self.assertEqual(url_to_mp3.youtube_access_opts({}), {})
        self.assertEqual(url_to_mp3.youtube_access_opts({"YTDLP_COOKIES_FILE": "  ", "YTDLP_PROXY": "", "YTDLP_PLAYER_CLIENTS": " , "}), {})

    def test_existing_cookies_file_proxy_and_clients_are_passed(self):
        with TemporaryDirectory() as directory:
            cookies = Path(directory) / "youtube-cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            options = url_to_mp3.youtube_access_opts({
                "YTDLP_COOKIES_FILE": str(cookies),
                "YTDLP_PROXY": "socks5://proxy.example:1080",
                "YTDLP_PLAYER_CLIENTS": "android, web_embedded",
            })
        self.assertEqual(options["cookiefile"], str(cookies))
        self.assertEqual(options["proxy"], "socks5://proxy.example:1080")
        self.assertEqual(options["extractor_args"], {"youtube": {"player_client": ["android", "web_embedded"]}})

    def test_missing_cookies_file_is_skipped(self):
        options = url_to_mp3.youtube_access_opts({"YTDLP_COOKIES_FILE": "/etc/secrets/absent.txt"})
        self.assertNotIn("cookiefile", options)

    def test_download_passes_access_options_to_both_yt_dlp_calls(self):
        seen = []

        class FakeYoutubeDL:
            def __init__(self, opts):
                seen.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {"id": "nBDFtTDXLAs", "title": "Lecture"}

        with TemporaryDirectory() as directory, patch.dict(os.environ, {"YTDLP_PROXY": "http://proxy.example:8080"}), patch.object(
            url_to_mp3.yt_dlp, "YoutubeDL", FakeYoutubeDL
        ):
            with self.assertRaises(FileNotFoundError):
                url_to_mp3.download_youtube_mp3("https://youtu.be/nBDFtTDXLAs", directory)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(opts["proxy"] == "http://proxy.example:8080" for opts in seen))


if __name__ == "__main__":
    unittest.main()
