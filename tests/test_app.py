"""The app itself, driven like a person would: open rolls, mark, zoom, scroll, sync, print and export."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from support import SheetDriver, make_roll, pm, pump, snapshot
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton

TEMPLATE = make_roll(Path(tempfile.mkdtemp()) / "template")


def fresh_roll(name: str = "roll") -> Path:
    folder = Path(tempfile.mkdtemp()) / name
    shutil.copytree(TEMPLATE, folder)
    return folder


class AppTest(unittest.TestCase):
    """Each test gets its own copy of the test roll and its own main window."""

    def setUp(self) -> None:
        self.folder = fresh_roll()
        self.originals = snapshot(self.folder)
        self.d = SheetDriver(self.folder)
        self.c = self.d.canvas

    def tearDown(self) -> None:
        self.d.close()
        self.assertEqual(snapshot(self.folder), self.originals, "a photo was modified")

    def frame(self, name: str) -> int:
        return next(i for i, f in enumerate(self.c.frames) if f.name == name)


class OpeningRolls(AppTest):
    def test_every_readable_file_loads(self) -> None:
        loaded = [f.name for f in self.c.frames if f.preview is not None]
        self.assertEqual(len(loaded), 11)  # 8 JPEGs + PNG + TIFF + rotated JPEG
        broken = {f.name for f in self.c.frames if f.error}
        self.assertEqual(broken, {"img_23_corrupt.jpg", "img_24_fake.nef"})

    def test_window_title_is_the_roll(self) -> None:
        self.assertEqual(self.d.win.windowTitle(), "roll")

    def test_welcome_page_when_nothing_is_open(self) -> None:
        win = self.d.win
        self.assertIs(win.central.currentWidget(), win.tabs)
        win.close_tab(win.tabs.indexOf(self.d.page))
        pump()
        self.assertIs(win.central.currentWidget(), win.welcome)
        self.assertEqual(win.windowTitle(), "")

    def test_import_button_comes_first(self) -> None:
        buttons = [w for w in self.d.win.tool_bar.findChildren(QToolButton) if w.objectName() == "importButton"]
        self.assertEqual([b.text() for b in buttons], ["Import"])

    def test_import_several_rolls_at_once(self) -> None:
        a, b = fresh_roll("RollA"), fresh_roll("RollB")
        self.d.win.import_folders([a, b, a])
        pump(200)
        titles = [self.d.win.tabs.tabText(i) for i in range(self.d.win.tabs.count())]
        self.assertEqual(self.d.win.tabs.count(), 3, titles)
        self.d.win.import_folders([b])
        self.assertEqual(self.d.win.tabs.count(), 3)  # already open: not opened twice
        for i in range(self.d.win.tabs.count()):
            self.d.wait_loaded(self.d.win.tabs.widget(i))


class Marking(AppTest):
    def setUp(self) -> None:
        super().setUp()
        self.i = self.frame("img_02.jpg")

    def test_every_tool_draws_by_dragging(self) -> None:
        for n, tool in enumerate([pm.T_STAR, pm.T_REJECT, pm.T_PUSH, pm.T_PULL, pm.T_CROP,
                                  pm.T_LINE, pm.T_ARROW, pm.T_RING]):
            self.d.win.set_tool(tool)
            i = self.c.order[n]
            self.d.drag(self.d.point(i, .3, .3), self.d.point(i, .7, .7))
            self.assertEqual([m.kind for m in self.c.frames[i].marks][-1:], [tool])
            self.c.sel = None

    def test_click_alone_draws_nothing(self) -> None:
        self.d.win.set_tool(pm.T_STAR)
        self.d.click(self.d.point(self.i))
        pump(QApplication.doubleClickInterval() + 150)
        self.assertEqual(self.c.frames[self.i].marks, [])
        self.assertTrue(self.c.loupe_locked)  # it enlarges instead

    def test_double_click_removes_newest_mark_without_enlarging(self) -> None:
        self.d.win.set_tool(pm.T_STAR)
        for fx in (.25, .5, .75):
            self.d.drag(self.d.point(self.i, fx, .3), self.d.point(self.i, fx + .1, .45))
        self.assertEqual(len(self.c.frames[self.i].marks), 3)
        seen = []
        p = self.d.point(self.i, .5, .8)
        QTest.mouseMove(self.d.vp, p)
        QTest.mousePress(self.d.vp, Qt.LeftButton, Qt.NoModifier, p)
        QTest.mouseRelease(self.d.vp, Qt.LeftButton, Qt.NoModifier, p)
        for _ in range(4):
            pump(25)
            seen.append(self.c.loupe_locked)
        QTest.mouseDClick(self.d.vp, Qt.LeftButton, Qt.NoModifier, p)
        QTest.mouseRelease(self.d.vp, Qt.LeftButton, Qt.NoModifier, p)
        for _ in range(10):
            pump(50)
            seen.append(self.c.loupe_locked)
        self.assertEqual(len(self.c.frames[self.i].marks), 2)
        self.assertFalse(any(seen), "the photo flashed to 95%")
        self.d.double_click(p)
        self.assertEqual(len(self.c.frames[self.i].marks), 1)

    def test_loupe_mode_double_click_keeps_marks(self) -> None:
        self.d.win.set_tool(pm.T_RING)
        self.d.drag(self.d.point(self.i, .2, .2), self.d.point(self.i, .6, .6))
        self.c.sel = None
        self.d.win.set_tool(pm.T_INSPECT)
        self.d.double_click(self.d.point(self.i))
        self.assertEqual(len(self.c.frames[self.i].marks), 1)

    def test_undo_and_redo(self) -> None:
        self.d.win.set_tool(pm.T_STAR)
        self.d.drag(self.d.point(self.i, .3, .3), self.d.point(self.i, .5, .5))
        self.c.undo()
        self.assertEqual(self.c.frames[self.i].marks, [])
        self.c.redo()
        self.assertEqual(len(self.c.frames[self.i].marks), 1)

    def test_rotate_turns_marks_with_the_photo(self) -> None:
        self.d.win.set_tool(pm.T_STAR)
        self.d.drag(self.d.point(self.i, .1, .1), self.d.point(self.i, .2, .2))
        before = [list(p) for p in self.c.frames[self.i].marks[0].pts]
        self.c._rotate(self.i, 1)
        after = self.c.frames[self.i].marks[0].pts
        self.assertEqual(self.c.frames[self.i].rotation, 1)
        self.assertAlmostEqual(after[0][0], 1 - before[0][1])
        self.c._rotate(self.i, 3)
        for got, want in zip(self.c.frames[self.i].marks[0].pts, before):
            self.assertAlmostEqual(got[0], want[0])
            self.assertAlmostEqual(got[1], want[1])


class Viewing(AppTest):
    def test_click_switches_60_and_95(self) -> None:
        i = self.c.order[1]
        self.d.click(self.d.point(i))
        self.assertTrue(self.c.loupe_locked)
        box, img = self.c.loupe_geometry(i, True)
        self.d.click(img.center().toPoint())
        self.assertFalse(self.c.loupe_locked)

    def test_wheel_scrolls_to_the_last_row(self) -> None:
        self.d.page.canvas.set_columns(3)
        pump(150)
        sb = self.c.verticalScrollBar()
        pt = self.d.point(self.c.order[1])
        for _ in range(40):
            self.d.wheel(pt, -120)
        self.assertGreater(sb.maximum(), 0)
        self.assertEqual(sb.value(), sb.maximum())
        self.assertFalse(self.c.loupe_locked)
        self.assertLessEqual(self.c._cell_rect(self.c.order[-1]).bottom(), self.d.vp.height() + 1)

    def test_ctrl_wheel_zooms_then_wheel_keeps_zooming(self) -> None:
        pt = self.d.point(self.c.order[1])
        QTest.mouseMove(self.d.vp, pt)
        pump()
        self.d.wheel(pt, 120, Qt.ControlModifier)
        self.assertTrue(self.c.loupe_locked)
        self.assertGreater(self.c.mag, pm.LOUPE_HOVER)
        for _ in range(12):
            self.d.wheel(QPoint(400, 400), 120)
        self.assertLessEqual(self.c.mag, pm.LOUPE_MAX + 1e-6)
        self.assertGreater(self.c.mag, pm.LOUPE_FIT)

    def test_negatives_as_positives(self) -> None:
        win, fr = self.d.win, self.c.frames[self.c.order[0]]
        sky_scan = fr.preview.pixelColor(fr.preview.width() // 3, 5).lightness()
        win.positive_action.setChecked(True)
        pump()
        sky_pos = fr.preview.pixelColor(fr.preview.width() // 3, 5).lightness()
        self.assertLess(sky_pos, sky_scan)  # a light sky reads as a negative's dark one
        info = json.loads((self.folder / ".proofmark-roll.json").read_text(encoding="utf-8"))
        self.assertTrue(info["positive"])
        job = win._sheet_job(self.d.page, None)
        self.assertEqual(job.pixmap_for(fr).toImage().pixelColor(fr.preview.width() // 3, 5).lightness(), sky_pos)
        win.positive_action.setChecked(False)
        pump()
        self.assertEqual(fr.preview.pixelColor(fr.preview.width() // 3, 5).lightness(), sky_scan)


class SavingAndOutput(AppTest):
    def test_sync_writes_sidecars_only(self) -> None:
        self.d.win.set_tool(pm.T_STAR)
        i = self.c.order[0]
        self.d.drag(self.d.point(i, .3, .3), self.d.point(i, .5, .5))
        self.d.win.sync_all()
        end = time.time() + 15
        pm_file = pm.pm_sidecar_path(self.c.frames[i].path)
        while not pm_file.exists() and time.time() < end:
            pump(50)
        pump(300)
        self.assertTrue(pm_file.exists())
        data = json.loads(pm_file.read_text(encoding="utf-8"))
        self.assertEqual(data["marks"][0]["kind"], pm.T_STAR)
        self.assertIn("user:star", json.loads(pm.rr_sidecar_path(self.c.frames[i].path).read_text())["tags"])

    def test_export_pdf_and_image_never_overwrite(self) -> None:
        out = Path(tempfile.mkdtemp())
        cfg = self.d.win.cfg
        cfg.set_setting("export_mode", "folder")
        cfg.set_setting("export_folder", str(out))
        cfg.set_setting("export_size", 4000)
        self.d.win.export_pdf()
        self.d.win.export_image()
        self.d.win.export_image()
        pump()
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(len([n for n in names if n.endswith(".pdf")]), 1, names)
        self.assertEqual(len([n for n in names if n.endswith(".jpg")]), 2, names)
        self.assertGreater((out / names[0]).stat().st_size, 10_000)

    def test_save_ink_switch(self) -> None:
        win = self.d.win
        win.ink_action.trigger()
        self.assertIs(win.cfg.setting("print_ink_saver"), True)
        win.ink_action.trigger()
        self.assertIs(win.cfg.setting("print_ink_saver"), False)

    def test_every_toolbar_button_has_icon_and_description(self) -> None:
        acts = [a for a in self.d.win.tool_bar.actions() if a.text() and not a.isSeparator()]
        self.assertEqual([a.text() for a in acts if a.icon().isNull()], [])
        self.assertEqual([a.text() for a in acts if not a.toolTip()], [])

    def test_windows_open_without_errors(self) -> None:
        win = self.d.win
        for name in ("open_settings", "open_equipment", "open_profile", "show_keys"):
            getattr(win, name)()
            pump(60)
        for tab in range(3):
            win.show_about(tab)
            pump(60)


if __name__ == "__main__":
    unittest.main()
