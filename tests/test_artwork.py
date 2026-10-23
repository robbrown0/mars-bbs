from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

from marsv2.artwork import (
    ArtworkError, MAX_IMAGE_BYTES, _SafeRedirect, image_type, local_artwork,
    validate_image,
)


class ArtworkTests(unittest.TestCase):
    def test_only_bounded_browser_image_formats_are_accepted(self) -> None:
        self.assertEqual(image_type(b"\xff\xd8\xffjpeg"), "image/jpeg")
        self.assertEqual(image_type(b"\x89PNG\r\n\x1a\npng"), "image/png")
        self.assertEqual(image_type(b"RIFF\x00\x00\x00\x00WEBPdata"), "image/webp")
        self.assertEqual(image_type(b"GIF89aimage"), "image/gif")
        self.assertIsNone(image_type(b"<svg xmlns='http://www.w3.org/2000/svg'>"))
        with self.assertRaisesRegex(ArtworkError, "supported"):
            validate_image(b"<svg></svg>")
        with self.assertRaisesRegex(ArtworkError, "5 MB"):
            validate_image(b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES)

    def test_redirects_cannot_escape_the_artwork_provider_allowlist(self) -> None:
        redirect = _SafeRedirect()
        request = Request("https://coverartarchive.org/release-group/example/front-1200")
        with self.assertRaisesRegex(ArtworkError, "unapproved host"):
            redirect.redirect_request(
                request, None, 302, "Found", {}, "http://127.0.0.1/private")
        upgraded = redirect.redirect_request(
            request, None, 302, "Found", {},
            "http://archive.org/download/example/cover.jpg")
        self.assertEqual(upgraded.full_url,
                         "https://archive.org/download/example/cover.jpg")

    def test_folder_art_ignores_symlinks_and_accepts_real_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            track = album / "Track.mp3"
            track.write_bytes(b"not-a-real-mp3")
            outside = root / "outside.jpg"
            outside.write_bytes(b"\xff\xd8\xffoutside")
            os.symlink(outside, album / "cover.jpg")
            self.assertIsNone(local_artwork(str(track)))
            (album / "cover.jpg").unlink()
            payload = b"\xff\xd8\xffreal-cover"
            (album / "cover.jpg").write_bytes(payload)
            self.assertEqual(local_artwork(str(track)),
                             ("image/jpeg", payload, "folder"))


if __name__ == "__main__":
    unittest.main()
