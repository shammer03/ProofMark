"""The non-destructive rules: ProofMark's sidecars, and merging into RapidRAW's and other apps' files."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import make_roll, pm

XMP = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="{rating}">
   <dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">Kept as it is</dc:title>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""


class RapidRawMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        make_roll(self.dir, count=1, extras=False)
        self.img = self.dir / "img_01.jpg"
        self.rr = pm.rr_sidecar_path(self.img)

    def write_rr(self, data: dict) -> None:
        self.rr.write_text(json.dumps(data), encoding="utf-8")

    def read_rr(self) -> dict:
        return json.loads(self.rr.read_text(encoding="utf-8"))

    def test_sidecar_names(self) -> None:
        self.assertEqual(pm.pm_sidecar_path(self.img).name, "img_01.jpg.pmdata")
        self.assertEqual(self.rr.name, "img_01.jpg.rrdata")

    def test_nothing_to_say_creates_no_file(self) -> None:
        owned = pm.merge_rapidraw(self.img, {}, 0, set(), {})
        self.assertIsNotNone(owned)
        self.assertFalse(self.rr.exists())

    def test_new_sidecar_gets_rating_tags_and_exif(self) -> None:
        owned = pm.merge_rapidraw(self.img, {"Artist": "S. Hambrick"}, 5, {"user:star"}, {}, label="green")
        data = self.read_rr()
        self.assertEqual(data["rating"], 5)
        self.assertIn("user:star", data["tags"])
        self.assertIn("color:green", data["tags"])
        self.assertEqual(data["exif"]["Artist"], "S. Hambrick")
        self.assertEqual(owned["rating"], 5)

    def test_rapidraw_edits_are_kept(self) -> None:
        adjustments = {"exposure": 0.7, "crop": {"x": 1, "y": 2}}
        self.write_rr({"version": 1, "rating": 0, "adjustments": adjustments, "tags": ["user:portfolio"]})
        pm.merge_rapidraw(self.img, {}, 4, {"user:star"}, {})
        data = self.read_rr()
        self.assertEqual(data["adjustments"], adjustments)
        self.assertIn("user:portfolio", data["tags"])
        self.assertIn("user:star", data["tags"])

    def test_rating_set_in_rapidraw_wins(self) -> None:
        self.write_rr({"version": 1, "rating": 2, "adjustments": {}})
        owned = pm.merge_rapidraw(self.img, {}, 5, set(), {})
        self.assertEqual(self.read_rr()["rating"], 2)
        self.assertIsNone(owned["rating"])

    def test_own_rating_can_change(self) -> None:
        owned = pm.merge_rapidraw(self.img, {}, 3, {"user:star"}, {})
        owned = pm.merge_rapidraw(self.img, {}, 5, {"user:star"}, owned)
        self.assertEqual(self.read_rr()["rating"], 5)

    def test_exif_field_typed_in_rapidraw_wins(self) -> None:
        self.write_rr({"version": 1, "rating": 0, "adjustments": {}, "exif": {"Artist": "Someone Else"}})
        pm.merge_rapidraw(self.img, {"Artist": "S. Hambrick"}, 0, set(), {})
        self.assertEqual(self.read_rr()["exif"]["Artist"], "Someone Else")

    def test_removed_mark_takes_only_its_own_tag(self) -> None:
        self.write_rr({"version": 1, "rating": 0, "adjustments": {}, "tags": ["user:portfolio"]})
        owned = pm.merge_rapidraw(self.img, {}, 0, {"user:star", "user:crop"}, {})
        pm.merge_rapidraw(self.img, {}, 0, {"user:star"}, owned)
        tags = self.read_rr()["tags"]
        self.assertNotIn("user:crop", tags)
        self.assertIn("user:star", tags)
        self.assertIn("user:portfolio", tags)

    def test_colour_label_set_in_rapidraw_wins(self) -> None:
        self.write_rr({"version": 1, "rating": 0, "adjustments": {}, "tags": ["color:blue"]})
        pm.merge_rapidraw(self.img, {}, 0, set(), {}, label="red")
        tags = self.read_rr()["tags"]
        self.assertIn("color:blue", tags)
        self.assertNotIn("color:red", tags)

    def test_unreadable_sidecar_is_never_overwritten(self) -> None:
        self.rr.write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(pm.merge_rapidraw(self.img, {"Artist": "x"}, 5, {"user:star"}, {}))
        self.assertEqual(self.rr.read_text(encoding="utf-8"), "{ this is not json")

    def test_photo_itself_is_untouched(self) -> None:
        before = self.img.read_bytes()
        pm.merge_rapidraw(self.img, {"Artist": "S. Hambrick"}, 5, {"user:star"}, {}, label="green")
        self.assertEqual(self.img.read_bytes(), before)


class XmpMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.xmp = Path(tempfile.mkdtemp()) / "img_01.xmp"

    def test_sets_empty_rating_and_keeps_the_rest(self) -> None:
        self.xmp.write_text(XMP.format(rating="0"), encoding="utf-8")
        changed, owned = pm.merge_xmp_rating(self.xmp, 4, None)
        text = self.xmp.read_text(encoding="utf-8")
        self.assertTrue(changed)
        self.assertEqual(owned, 4)
        self.assertIn('xmp:Rating="4"', text)
        self.assertIn("Kept as it is", text)

    def test_rating_from_another_app_wins(self) -> None:
        self.xmp.write_text(XMP.format(rating="2"), encoding="utf-8")
        changed, owned = pm.merge_xmp_rating(self.xmp, 5, None)
        self.assertFalse(changed)
        self.assertIsNone(owned)
        self.assertIn('xmp:Rating="2"', self.xmp.read_text(encoding="utf-8"))

    def test_own_rating_can_change(self) -> None:
        self.xmp.write_text(XMP.format(rating="3"), encoding="utf-8")
        changed, owned = pm.merge_xmp_rating(self.xmp, 5, 3)
        self.assertTrue(changed)
        self.assertEqual(owned, 5)

    def test_label_is_added_when_missing(self) -> None:
        self.xmp.write_text(XMP.format(rating="0"), encoding="utf-8")
        changed, owned = pm.merge_xmp_prop(self.xmp, "Label", "Green", None)
        self.assertTrue(changed)
        self.assertIn("<xmp:Label>Green</xmp:Label>", self.xmp.read_text(encoding="utf-8"))


class RollFile(unittest.TestCase):
    def test_roll_info_round_trip(self) -> None:
        folder = make_roll(Path(tempfile.mkdtemp()) / "2026-08-20_Portra-400", count=3, extras=False)
        roll = pm.Roll(folder, "Kodak Portra 400", "", "")
        roll.title, roll.lab, roll.keywords, roll.positive = "Lake", "C-41", ["lake", "summer"], True
        roll.order = [2, 0, 1]
        roll.hidden = {1}
        roll.save_info()
        again = pm.Roll(folder, "Kodak Portra 400", "", "")
        self.assertEqual((again.title, again.lab, again.keywords, again.positive),
                         ("Lake", "C-41", ["lake", "summer"], True))
        self.assertEqual(again.order, [2, 0, 1])
        self.assertEqual(again.hidden, {1})
        self.assertEqual(again.label(1), "—")

    def test_order_survives_new_files(self) -> None:
        folder = make_roll(Path(tempfile.mkdtemp()) / "roll", count=3, extras=False)
        roll = pm.Roll(folder, "", "", "")
        roll.order = [2, 1, 0]
        roll.save_info()
        (folder / "img_00.jpg").write_bytes((folder / "img_01.jpg").read_bytes())
        again = pm.Roll(folder, "", "", "")
        names = [again.frames[i].name for i in again.order]
        self.assertEqual(names[:3], ["img_03.jpg", "img_02.jpg", "img_01.jpg"])
        self.assertIn("img_00.jpg", names)


if __name__ == "__main__":
    unittest.main()
