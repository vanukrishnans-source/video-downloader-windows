import unittest
from vdl import core


class T(unittest.TestCase):
    def test_scan(self):
        t, urls, poster = core.scan_html('<title>X</title><video poster="p.jpg"><source src="a.mp4"></video>'
                                         '<meta property="og:video" content="https://c/b.m3u8"><a href="/seg1.ts">', "http://h/d/")
        self.assertEqual(urls, ["http://h/d/a.mp4", "https://c/b.m3u8"])
        self.assertEqual(poster, "http://h/d/p.jpg")

    def test_formats(self):
        self.assertIn("height<=720", core.format_opts("720p")["format"])
        self.assertEqual(core.format_opts("MP3 (audio)")["postprocessors"][0]["preferredcodec"], "mp3")


if __name__ == "__main__":
    unittest.main()
