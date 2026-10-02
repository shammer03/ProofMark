"""
Shared set-up for the tests. Importing this first makes the tests safe to run on a real desktop:

- HOME, XDG_CONFIG_HOME and XDG_CACHE_HOME point into a throw-away folder, so your own settings,
  recent rolls, preview cache and Desktop launcher are never read or written;
- Qt draws off screen (no windows appear);
- every test photo is generated here, in that throw-away folder.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="proofmark-tests-"))
tempfile.tempdir = str(SANDBOX)  # every temporary folder the tests make goes in here too
atexit.register(shutil.rmtree, SANDBOX, True)
os.environ["HOME"] = str(SANDBOX / "home")
os.environ["XDG_CONFIG_HOME"] = str(SANDBOX / "home" / ".config")
os.environ["XDG_CACHE_HOME"] = str(SANDBOX / "home" / ".cache")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
(SANDBOX / "home").mkdir()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proofmark as pm  # noqa: E402  (must come after the environment above)
from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])
# Dialogs that would wait for a person: answer "Yes" / "OK" straight away.
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.exec = lambda self: 0


def pump(ms: int = 40) -> None:
    """Let Qt run for `ms` milliseconds (timers, repaints, background loading)."""
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _photo(w: int, h: int, n: int) -> Image.Image:
    """A small made-up landscape: sky, sun, hill; colours vary with n."""
    sky = [(120, 180, 230), (200, 160, 220), (240, 180, 90), (110, 150, 200)][n % 4]
    im = Image.new("RGB", (w, h), sky)
    d = ImageDraw.Draw(im)
    d.ellipse((w * 0.65, h * 0.15, w * 0.8, h * 0.15 + w * 0.15), fill=(255, 240, 200))
    d.polygon([(0, h), (0, h * 0.6), (w * 0.4, h * 0.45 + n * 3), (w, h * 0.65), (w, h)], fill=(40, 120, 70))
    d.text((10, 10), f"frame {n}", fill=(0, 0, 0))
    return im


def make_roll(folder: Path, count: int = 8, extras: bool = True) -> Path:
    """
    A roll folder of test photos: JPEGs (one vertical), and with `extras` also a PNG, a TIFF,
    a JPEG whose EXIF says "rotate 90°", a corrupt JPEG and a fake RAW file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, count + 1):
        w, h = (600, 900) if n == 3 else (900, 600)
        _photo(w, h, n).save(folder / f"img_{n:02d}.jpg", quality=85)
    if extras:
        _photo(900, 600, 20).save(folder / "img_20.png")
        _photo(900, 600, 21).save(folder / "img_21.tif")
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90° clockwise to view
        exif[0x013B] = "Test Artist"
        _photo(900, 600, 22).save(folder / "img_22_rotated.jpg", exif=exif.tobytes())
        (folder / "img_23_corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
        (folder / "img_24_fake.nef").write_bytes(b"not a raw file")
    return folder


def snapshot(folder: Path) -> dict[str, bytes]:
    """Every photo's bytes, to prove later that none of them changed."""
    return {p.name: p.read_bytes() for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in pm.ALL_EXTS}


class SheetDriver:
    """Opens a roll in a fresh main window and drives the mouse like a person would."""

    def __init__(self, folder: Path, size: tuple[int, int] = (1600, 1000)) -> None:
        self.win = pm.MainWindow(pm.ConfigStore())
        self.win.resize(*size)
        self.win.show()
        pump()
        self.page = self.win.open_roll(folder)
        self.wait_loaded()
        self.canvas = self.page.canvas
        self.vp = self.canvas.viewport()
        pump(150)

    def wait_loaded(self, page=None, timeout: float = 30) -> None:
        page = page or self.page
        end = time.time() + timeout
        while page.loading() and time.time() < end:
            pump(30)

    def close(self) -> None:
        self.win.close()
        pump(50)
        self.win.deleteLater()
        pump(20)

    def point(self, i: int, fx: float = 0.5, fy: float = 0.5) -> QPoint:
        r = self.canvas._image_rect(i)
        return QPoint(int(r.x() + r.width() * fx), int(r.y() + r.height() * fy))

    def drag(self, a: QPoint, b: QPoint) -> None:
        QTest.mouseMove(self.vp, a)
        QTest.mousePress(self.vp, Qt.LeftButton, Qt.NoModifier, a)
        for k in range(1, 9):
            QTest.mouseMove(self.vp, QPoint(a.x() + (b.x() - a.x()) * k // 8, a.y() + (b.y() - a.y()) * k // 8))
            pump(5)
        QTest.mouseRelease(self.vp, Qt.LeftButton, Qt.NoModifier, b)
        pump()

    def click(self, pt: QPoint) -> None:
        QTest.mouseMove(self.vp, pt)
        QTest.mouseClick(self.vp, Qt.LeftButton, Qt.NoModifier, pt)
        pump()

    def double_click(self, pt: QPoint) -> None:
        """The events a real double-click sends: press, release, double-click, release."""
        QTest.mouseMove(self.vp, pt)
        QTest.mousePress(self.vp, Qt.LeftButton, Qt.NoModifier, pt)
        QTest.mouseRelease(self.vp, Qt.LeftButton, Qt.NoModifier, pt)
        QTest.mouseDClick(self.vp, Qt.LeftButton, Qt.NoModifier, pt)
        QTest.mouseRelease(self.vp, Qt.LeftButton, Qt.NoModifier, pt)
        pump()

    def wheel(self, pt: QPoint, dy: int, mods=Qt.NoModifier) -> None:
        ev = QWheelEvent(QPointF(pt), QPointF(self.vp.mapToGlobal(pt)), QPoint(), QPoint(0, dy),
                         Qt.NoButton, mods, Qt.NoScrollPhase, False)
        QApplication.sendEvent(self.vp, ev)
        pump()
