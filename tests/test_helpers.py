"""Small building blocks: names, numbers, zoom stops, decoding, the preview cache, negatives, the icon."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from support import ROOT, make_roll, pm
from PIL import Image, ImageOps


class Basics(unittest.TestCase):
    def test_version_matches_release_files(self) -> None:
        spec = (ROOT / "packaging" / "proofmark.spec").read_text(encoding="utf-8")
        meta = (ROOT / "packaging" / "io.github.shammer03.ProofMark.metainfo.xml").read_text(encoding="utf-8")
        self.assertIn(f"Version:        {pm.APP_VERSION}\n", spec)
        self.assertIn(f'<release version="{pm.APP_VERSION}"', meta)

    def test_files_sort_like_a_person_would(self) -> None:
        names = ["img_10.jpg", "img_2.jpg", "IMG_1.jpg"]
        self.assertEqual(sorted(names, key=pm.natural_key), ["IMG_1.jpg", "img_2.jpg", "img_10.jpg"])

    def test_folder_name_gives_date_and_film(self) -> None:
        date, film = pm.parse_folder_info("2026-08-20_Kodak-Portra-400", ["Kodak Portra 400", "Ilford HP5 Plus 400"])
        self.assertEqual(date, "2026-08-20")
        self.assertEqual(film, "Kodak Portra 400")

    def test_frame_numbers(self) -> None:
        saved = dict(pm.NUMBERING)
        try:
            for style, want in (("edge", "7A"), ("edge0", "07A"), ("simple", "#07"), ("none", "")):
                pm.NUMBERING["style"] = style
                self.assertEqual(pm.format_frame_number(7), want)
        finally:
            pm.NUMBERING.update(saved)

    def test_zoom_pauses_at_each_stop(self) -> None:
        self.assertEqual(pm.snap_detent(0.60, 1.20, pm.ZOOM_DETENTS), 0.75)
        self.assertEqual(pm.snap_detent(0.75, 1.20, pm.ZOOM_DETENTS), 0.95)
        self.assertAlmostEqual(pm.snap_detent(1.90, 0.70, pm.ZOOM_DETENTS), 1.425)
        self.assertEqual(pm.snap_detent(0.80, 0.85, pm.ZOOM_DETENTS), 0.85)  # no stop in between


class Decoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.roll = make_roll(Path(tempfile.mkdtemp()) / "roll", count=2)

    def test_exif_rotation_is_applied(self) -> None:
        img = pm.open_any_image(self.roll / "img_22_rotated.jpg")
        self.assertEqual(img.size, (600, 900))  # stored landscape, shown upright portrait

    def test_png_and_tiff_open(self) -> None:
        for name in ("img_20.png", "img_21.tif"):
            self.assertEqual(pm.open_any_image(self.roll / name).mode, "RGB")

    def test_corrupt_file_raises_instead_of_crashing(self) -> None:
        with self.assertRaises(Exception):
            pm.open_any_image(self.roll / "img_23_corrupt.jpg")

    def test_exif_read_for_rapidraw(self) -> None:
        self.assertEqual(pm.read_rr_exif(self.roll / "img_22_rotated.jpg").get("Artist"), "Test Artist")


class PreviewCache(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.tif = self.dir / "scan.tif"
        Image.new("RGB", (2400, 1600), (90, 120, 150)).save(self.tif)

    def test_small_jpeg_is_not_cached(self) -> None:
        make_roll(self.dir / "roll", count=1, extras=False)
        self.assertIsNone(pm.preview_cache_path(self.dir / "roll" / "img_01.jpg"))

    def test_cache_lives_outside_the_roll(self) -> None:
        cached = pm.preview_cache_path(self.tif)
        self.assertTrue(str(cached).startswith(os.environ["XDG_CACHE_HOME"]))
        pm.load_preview(self.tif)
        self.assertTrue(cached.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["scan.tif"])

    def test_cached_preview_matches(self) -> None:
        first = pm.load_preview(self.tif)
        second = pm.load_preview(self.tif)  # from the cache
        self.assertEqual(first.size, second.size)
        self.assertLessEqual(max(first.size), max(pm.PREVIEW_W, pm.PREVIEW_H))

    def test_edited_file_is_decoded_again(self) -> None:
        before = pm.preview_cache_path(self.tif)
        time.sleep(0.01)
        Image.new("RGB", (2400, 1600), (200, 10, 10)).save(self.tif)
        self.assertNotEqual(pm.preview_cache_path(self.tif), before)
        self.assertGreater(pm.load_preview(self.tif).getpixel((5, 5))[0], 150)

    def test_cache_is_trimmed_oldest_first(self) -> None:
        cache = pm.preview_cache_dir() / "zz"
        cache.mkdir(parents=True, exist_ok=True)
        old, new = cache / "old.jpg", cache / "new.jpg"
        old.write_bytes(b"x" * 1000)
        new.write_bytes(b"x" * 1000)
        os.utime(old, (1, 1))
        pm.prune_preview_cache(limit=sum(f.stat().st_size for f in pm.preview_cache_dir().glob("*/*.jpg")) - 500)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class Negatives(unittest.TestCase):
    def test_colour_negative_becomes_a_neutral_positive(self) -> None:
        pos = Image.new("RGB", (400, 300), (235, 235, 235))  # light grey sky over a dark ground
        pos.paste((30, 30, 30), (0, 150, 400, 300))
        neg = ImageOps.invert(pos).point(lambda v: int(40 + v * 0.6))  # flat, like film
        mask = (255, 150, 90)  # the orange mask of colour negative film
        neg = Image.merge("RGB", [b.point(lambda v, k=k: v * k // 255) for b, k in zip(neg.split(), mask)])
        back = pm.as_positive(neg)
        sky, ground = back.getpixel((200, 50)), back.getpixel((200, 250))
        self.assertGreater(min(sky), 200)
        self.assertLess(max(ground), 60)
        self.assertLess(max(sky) - min(sky), 30)  # the orange cast is gone

    def test_same_levels_for_thumbnail_and_preview(self) -> None:
        neg = Image.new("RGB", (400, 300), (180, 120, 80))
        neg.paste((60, 30, 20), (0, 0, 400, 150))
        lut = pm.positive_lut(neg)
        small = neg.resize((100, 75))
        self.assertEqual(pm.as_positive(small, lut).getpixel((50, 10)), pm.as_positive(neg, lut).getpixel((200, 40)))


class Icon(unittest.TestCase):
    def test_icon_draws_with_transparent_corners(self) -> None:
        img = pm.build_icon_pixmap(128).toImage()
        self.assertEqual(img.pixelColor(0, 0).alpha(), 0)
        self.assertGreater(img.pixelColor(64, 64).alpha(), 0)

    def test_packaged_icon_exists(self) -> None:
        self.assertTrue((ROOT / "packaging" / "proofmark-512.png").exists())


if __name__ == "__main__":
    unittest.main()
