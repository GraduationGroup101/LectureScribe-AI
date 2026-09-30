import builtins
import errno
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


class ReadOnlyCookieTests(unittest.TestCase):
    """Exercise real yt-dlp cookie loading and saving without network requests."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "youtube-cookies.txt"
        self.original = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\ttest_cookie\tfixture\n"
        ).encode()
        self.source.write_bytes(self.original)
        self.seen = []
        real_open = builtins.open

        def read_only_open(filename, mode="r", *args, **kwargs):
            if isinstance(filename, (str, os.PathLike)) and Path(filename) == self.source:
                if any(flag in mode for flag in "wax+"):
                    raise OSError(errno.EROFS, "Read-only file system", str(self.source))
            return real_open(filename, mode, *args, **kwargs)

        for replacement in (
            patch.dict(os.environ, {"YTDLP_COOKIES_FILE": str(self.source)}),
            patch("builtins.open", side_effect=read_only_open),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def extract_info(self, ydl, url, download=False):
        self.seen.append(Path(ydl.params["cookiefile"]))
        cookie = next((cookie for cookie in ydl.cookiejar if cookie.name == "test_cookie"), None)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie.value, "fixture")
        if download:
            audio = ydl.prepare_filename({"id": "nBDFtTDXLAs", "title": "Lecture", "ext": "mp3"})
            Path(audio).write_bytes(b"audio fixture")
        return {"id": "nBDFtTDXLAs", "title": "Lecture"}

    def assert_cookie_copy_removed(self):
        self.assertTrue(self.seen)
        self.assertTrue(all(path != self.source and not path.exists() for path in self.seen))
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_read_only_secret_works_for_metadata_and_audio(self):
        with patch.object(url_to_mp3.yt_dlp.YoutubeDL, "extract_info", lambda ydl, url, download=False: self.extract_info(ydl, url, download)):
            path, metadata = url_to_mp3.download_youtube_mp3(
                "https://youtu.be/nBDFtTDXLAs", self.directory, return_metadata=True
            )
        self.assertTrue(Path(path).is_file())
        self.assertEqual(metadata["video_id"], "nBDFtTDXLAs")
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[0], self.seen[1])
        self.assert_cookie_copy_removed()

    def test_cookie_copy_is_removed_on_cache_return(self):
        cached = self.directory / "nBDFtTDXLAs_Lecture.mp3"
        cached.write_bytes(b"cached audio")
        with patch.object(url_to_mp3.yt_dlp.YoutubeDL, "extract_info", lambda ydl, url, download=False: self.extract_info(ydl, url, download)):
            path = url_to_mp3.download_youtube_mp3("https://youtu.be/nBDFtTDXLAs", self.directory)
        self.assertEqual(Path(path), cached)
        self.assertEqual(len(self.seen), 1)
        self.assert_cookie_copy_removed()

    def test_cookie_copy_is_removed_when_extraction_raises(self):
        def fail(ydl, url, download=False):
            self.extract_info(ydl, url)
            raise RuntimeError("Extraction failed")

        with patch.object(url_to_mp3.yt_dlp.YoutubeDL, "extract_info", fail):
            with self.assertRaisesRegex(RuntimeError, "Extraction failed"):
                url_to_mp3.download_youtube_mp3("https://youtu.be/nBDFtTDXLAs", self.directory)
        self.assert_cookie_copy_removed()

    def test_each_download_has_an_independent_cookie_copy(self):
        with url_to_mp3.writable_youtube_access_opts() as first:
            with url_to_mp3.writable_youtube_access_opts() as second:
                self.assertNotEqual(first["cookiefile"], second["cookiefile"])
                Path(first["cookiefile"]).write_text("Changed session", encoding="utf-8")
                self.assertEqual(Path(second["cookiefile"]).read_bytes(), self.original)
        self.assertFalse(Path(first["cookiefile"]).exists())
        self.assertFalse(Path(second["cookiefile"]).exists())
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
