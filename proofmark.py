#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 S. Hambrick
"""
ProofMark — darkroom contact-sheet proofing with wax grease-pencil marks.

Target : Fedora Linux (Wayland / X11)
Stack  : Python 3, PySide6 (Qt 6), Pillow, rawpy (optional, for RAW files)
Extras : exiftool (optional) to read RAW EXIF when seeding RapidRAW sidecars; originals are never written

Module order (top-down execution safety):
  1. Data models, emulsion presets, equipment config, grease shader
  2. Dialogs / custom widgets (equipment manager, profile)
  3. Background workers (image loader, rapid sync)
  4. Contact sheet canvas
  5. Roll page + main window + desktop integration
"""
from __future__ import annotations

import io
import json
import math
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape, quoteattr

from PIL import Image, ImageOps

try:
    import rawpy  # type: ignore
except ImportError:  # RAW support degrades gracefully
    rawpy = None

from PySide6.QtCore import QMarginsF, QProcess, QUrl
from PySide6.QtGui import QDesktopServices, QPageLayout
from PySide6.QtWidgets import QCheckBox, QColorDialog, QFontComboBox, QTextBrowser, QToolButton

try:  # print support ships with PySide6 on Fedora; degrade gracefully if missing
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter
except ImportError:  # pragma: no cover
    QPrintDialog = None  # type: ignore[assignment,misc]
    QPrinter = None  # type: ignore[assignment,misc]

from PySide6.QtCore import (QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QFontMetrics,
                           QIcon, QImage, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QComboBox,
                               QCompleter, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

Image.MAX_IMAGE_PIXELS = None

# ════════════════════════════════════════════════════════════════════════════
# 1. DATA MODELS, PRESETS, CONFIG, SHADER
# ════════════════════════════════════════════════════════════════════════════

APP_NAME = "ProofMark"
APP_VERSION = "1.1.2"
BUILD_NUMBER = 7
BUILD_DATE = "2026-09-30"
RELEASE_CHANNEL = "stable"
APP_ID = "io.github.shammer03.ProofMark"  # reverse-DNS id used by Flatpak / AppStream; change to your own
UPDATE_REPO = "shammer03/ProofMark"  # GitHub "owner/repo" that publishes releases (or set it in Settings ▸ Updates)
SCRIPT_DIR = Path(__file__).resolve().parent

# Newest first. `release.py bump` inserts new entries at the marker.
CHANGELOG: list[tuple[str, str, list[str]]] = [
    # <changelog-insert>
    ("1.1.2", "2026-09-30", [
        'Non-destructive like RapidRAW: your original image files are never modified',
        "Artist, copyright and film / camera / lens go into RapidRAW's .rrdata sidecar and are applied when RapidRAW exports",
        "ProofMark's marks moved to their own .pmdata sidecar, so RapidRAW edits are no longer overwritten",
        'An existing .xmp from another app is no longer replaced']),
    ("1.1.1", "2026-09-30", [
        'Print and PDF export: contact sheet is centred with correct margins',
        "Images that can't be opened are labelled instead of showing loading… forever",
        'Closing during loading or syncing no longer risks a crash or lost sidecars',
        'Wayland: app ID registers correctly; Ctrl+C in a terminal closes cleanly',
        'Number fields show their up / down arrows on the dark theme']),
    ("1.1.0", "2026-09-30", [
        "Crop box and ring are real boxes: drag one out, or click for a default, then drag its handles to resize",
        "New Adjust tool (E): select any mark, move it, resize it with handles, nudge with the arrow keys, Delete removes",
        "Version and build-number tracking, Version History, and an in-app update check",
        "One-click Print Contact Sheet and Export PDF: clean, unmarked, same layout as the screen",
        "Settings: font, interface scale, accent colour, menu bar and toolbar placement, printing, sync and updates",
        "Packaging files for RPM (COPR / Fedora) and Flatpak (Flathub), plus a release helper script"]),
    ("1.0.3", "2026-09", [
        "About and Developer pages; click-drag to size marks; window opens maximised and fits the screen"]),
    ("1.0.2", "2026-09", [
        "Brush size slider and four wax colours; favorites menus for films, cameras and lenses"]),
    ("1.0.1", "2026-09", [
        "Personal equipment inventory with mounts; Fedora desktop launcher"]),
    ("1.0.0", "2026-09", [
        "First release: contact sheet, grease-pencil tools, loupe, compare mode, session recovery, sidecar sync"]),
]


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text.strip().lstrip("vV").split("-")[0])
    return tuple(int(n) for n in nums) or (0,)


_GIT_HASH: Optional[str] = None


def git_hash() -> str:
    """Short commit hash when running from a git checkout, else an empty string."""
    global _GIT_HASH
    if _GIT_HASH is None:
        _GIT_HASH = ""
        if (SCRIPT_DIR / ".git").exists() and shutil.which("git"):
            try:
                out = subprocess.run(["git", "-C", str(SCRIPT_DIR), "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True, timeout=3, check=False)
                if out.returncode == 0:
                    _GIT_HASH = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    return _GIT_HASH


def version_string(full: bool = False) -> str:
    text = f"{APP_VERSION} (build {BUILD_NUMBER})"
    if full:
        text += f" · {RELEASE_CHANNEL} · {BUILD_DATE}"
        if git_hash():
            text += f" · {git_hash()}"
    return text


def install_kind() -> str:
    """flatpak | system (RPM etc.) | git (checkout) | script (loose file)."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return "flatpak"
    here = Path(__file__).resolve()
    if (here.parent / ".git").exists():
        return "git"
    if str(here).startswith(("/usr/", "/opt/")):
        return "system"
    return "script"
# Fill these in to personalise Help ▸ About the Developer.
# If "name" is blank, the Photographer / Artist name from your User Profile is used.
DEV_INFO = {
    "name": "S. Hambrick",
    "role": "Photographer & developer",
    "website": "https://github.com/shammer03/ProofMark",
    "email": "",
    "bio": "",
}
CONFIG_DIR = Path.home() / ".config" / "proofmark"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = CONFIG_DIR / "last_session.json"

THUMB_W, THUMB_H = 450, 320
PREVIEW_W, PREVIEW_H = 1920, 1440

C_BASE = QColor("#0A0A0A")
C_BAR = QColor("#141414")
C_TEXT = QColor("#FFFFFF")
C_AMBER = QColor("#FFA726")
C_AMBER_DIM = QColor(255, 167, 38, 170)
ACCENT_HEX = "#FFA726"
REBATE_SCALE = 1.0


def set_accent(hex_color: str) -> None:
    """Change the amber accent used by painting code and the stylesheet."""
    global C_AMBER, C_AMBER_DIM, ACCENT_HEX
    c = QColor(hex_color)
    if c.isValid():
        ACCENT_HEX = c.name().upper()
        C_AMBER = QColor(c)
        C_AMBER_DIM = QColor(c.red(), c.green(), c.blue(), 170)


def set_rebate_scale(percent: int) -> None:
    global REBATE_SCALE
    REBATE_SCALE = max(0.5, min(2.0, percent / 100.0))
C_FILM = QColor("#050505")
C_GREASE = QColor("#FF3B30")

RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf",
            ".pef", ".srw", ".x3f"}
STD_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
ALL_EXTS = RAW_EXTS | STD_EXTS

# Tool identifiers
T_INSPECT = "inspect"
T_STAR = "star"
T_REJECT = "reject"
T_PUSH = "push"
T_PULL = "pull"
T_CROP = "crop"
T_LINE = "line"
T_ARROW = "arrow"
T_RING = "ring"
T_ADJUST = "adjust"
BRUSH_COLORS = [("Red", "#FF3B30"), ("Toxic Green", "#39FF14"),
                ("Silver", "#E6E6EA"), ("Yellow", "#FFE600")]
DEFAULT_BRUSH_COLOR = BRUSH_COLORS[0][1]
POINT_TOOLS = {T_STAR, T_REJECT, T_PUSH, T_PULL}   # click places, drag outward sizes
BOX_TOOLS = {T_CROP, T_RING}                      # drag a box; click places a default box
DRAG_TOOLS = {T_LINE, T_ARROW, T_CROP, T_RING}

TOOL_LIST = [
    (T_INSPECT, "Inspect", "V", "Inspect: hover for loupe, click to enlarge"),
    (T_STAR, "★ Pick", "P", "Star pick  (* / P)"),
    (T_REJECT, "☒ Reject", "X", "Boxed reject  (X)"),
    (T_PUSH, "+ Push", "+", "Exposure push  (+)"),
    (T_PULL, "− Pull", "-", "Exposure pull  (-)"),
    (T_CROP, "# Crop", "#", "Crop box  (#) — drag a box, then drag its handles to adjust"),
    (T_LINE, "✎ Line", "L", "Freehand grease line  (L)"),
    (T_ARROW, "→ Arrow", "A", "Grease arrow  (A)"),
    (T_RING, "◯ Ring", "R", "Squircle ring  (R) — drag it out, then drag its handles to adjust"),
    (T_ADJUST, "✥ Adjust", "E", "Adjust  (E): select a mark, drag to move, drag handles to resize, Del removes"),
]

BALANCE_TAGS = {
    "D": "Daylight",
    "T": "Tungsten",
    "Pan": "Panchromatic",
    "Ortho": "Orthochromatic",
    "IR": "Infrared",
}

# Barcode alphabet:  '|' wide bar  '!' narrow bar  ' ' wide gap  '.' narrow gap
# NOTE: edge prefixes / DX values are representative and fully editable in the
# Equipment Manager; verify against your own film boxes for archival accuracy.
EMULSION_EDGE_DATA: dict[str, dict[str, Any]] = {
    "Kodak Tri-X 400": {"edge": "KODAK TRI-X 400", "dx": "DX 5063 TX", "iso": 400, "balance": "Pan",
                        "barcode": "|!| |!!| ! |.|!| |!|!| |!! |"},
    "Kodak T-Max 100": {"edge": "KODAK T-MAX 100", "dx": "DX 5052 TMX", "iso": 100, "balance": "Pan",
                        "barcode": "!|! |.|!| ||! |!| !|!| |.|"},
    "Kodak T-Max 400": {"edge": "KODAK T-MAX 400", "dx": "DX 5053 TMY", "iso": 400, "balance": "Pan",
                        "barcode": "|!|! |!| . |!!| |!| ! ||!"},
    "Kodak Portra 160": {"edge": "KODAK PORTRA 160", "dx": "DX 5160 PRT", "iso": 160, "balance": "D",
                         "barcode": "!| |!| ||.| !|! |!| .|!|"},
    "Kodak Portra 400": {"edge": "KODAK PORTRA 400", "dx": "DX 5405 PRT", "iso": 400, "balance": "D",
                         "barcode": "|!! |!|. |!| ||! |.|! |!|"},
    "Kodak Portra 800": {"edge": "KODAK PORTRA 800", "dx": "DX 5800 PRT", "iso": 800, "balance": "D",
                         "barcode": "||! |!| !|! |.| |!!| |!|!"},
    "Kodak Ektar 100": {"edge": "KODAK EKTAR 100", "dx": "DX 5100 EKT", "iso": 100, "balance": "D",
                        "barcode": "!|!| |!| |.|! !|| |!| |!!|"},
    "Kodak Gold 200": {"edge": "KODAK GOLD 200", "dx": "DX 5200 GLD", "iso": 200, "balance": "D",
                       "barcode": "|.|! |!| ||! !|! |!|. |!|"},
    "Cinestill 800T": {"edge": "CINESTILL 800T", "dx": "DX 5800 CST", "iso": 800, "balance": "T",
                       "barcode": "|!|! ||. |!| !|!| |.|! ||"},
    "Cinestill 50D": {"edge": "CINESTILL 50D", "dx": "DX 5050 CSD", "iso": 50, "balance": "D",
                      "barcode": "!||! |!| .|!| |!!| |.|! |"},
    "Ilford HP5 Plus 400": {"edge": "ILFORD HP5 PLUS", "dx": "DX 4005 HP5", "iso": 400, "balance": "Pan",
                            "barcode": "|!| ||! |!|. !|! |!| |.|!"},
    "Ilford FP4 Plus 125": {"edge": "ILFORD FP4 PLUS", "dx": "DX 4125 FP4", "iso": 125, "balance": "Pan",
                            "barcode": "!|! |!!| |.| ||! |!|! |"},
    "Ilford Delta 100": {"edge": "ILFORD DELTA 100", "dx": "DX 4100 DLT", "iso": 100, "balance": "Pan",
                         "barcode": "|.|! !|| |!| ||!| !|! |"},
    "Ilford Delta 400": {"edge": "ILFORD DELTA 400", "dx": "DX 4400 DLT", "iso": 400, "balance": "Pan",
                         "barcode": "|!|. |!! |!| |.|! ||! !|"},
    "Fujifilm Acros 100 II": {"edge": "FUJIFILM ACROS 100", "dx": "DX 6100 ACR", "iso": 100, "balance": "Pan",
                              "barcode": "!|!| ||. |!| |!!| .|!"},
    "Fujifilm Velvia 50": {"edge": "FUJICHROME VELVIA 50", "dx": "DX 6050 RVP", "iso": 50, "balance": "D",
                           "barcode": "|!| !|.| |!!| |!| ||! |."},
    "Fujifilm Provia 100F": {"edge": "FUJICHROME PROVIA 100F", "dx": "DX 6100 RDP", "iso": 100, "balance": "D",
                             "barcode": "!||! |!| |.|! !|!| |!"},
    "Rollei Infrared 400": {"edge": "ROLLEI IR 400", "dx": "DX 7400 IR", "iso": 400, "balance": "IR",
                            "barcode": "|!|! !|| .|!| |!|! ||"},
    "Ilford Ortho Plus 80": {"edge": "ILFORD ORTHO PLUS", "dx": "DX 4080 ORT", "iso": 80, "balance": "Ortho",
                             "barcode": "!|!| |.|! ||! |!| !|"},
}

LENS_MAKERS = [
    "Pentax", "Nikon", "Canon", "Leica", "Zeiss", "Carl Zeiss Jena",
    "Schneider-Kreuznach", "Rodenstock", "Fujinon", "Mamiya", "Hasselblad",
    "Olympus", "Minolta", "Voigtländer", "Sigma", "Tamron", "Tokina",
    "Cooke", "Angénieux", "Wollensak", "Kodak", "Sinar", "Contax", "Yashica",
    "Zenza Bronica", "Chinon", "Sears",
]
LENS_SERIES: dict[str, list[str]] = {
    "Pentax": ["SMC Pentax-M", "SMC Pentax-A", "SMC Pentax", "SMC Pentax-FA",
               "SMC Pentax-67", "SMC Pentax-D FA", "SMC Pentax-A Zoom", "SMC Pentax-A*",
               "Super-Takumar", "Takumar", "SMC Takumar", "Super-Multi-Coated Takumar"],
    "Zenza Bronica": ["Zenzanon-S", "Zenzanon-PS", "Zenzanon-MC"],
    "Chinon": ["Auto", "Auto Chinon"],
    "Sears": ["Auto", "MC Auto"],
    "Yashica": ["Yashinon", "ML", "Yashikor"],
    "Nikon": ["Nikkor", "Nikkor-W", "Nikkor-M", "Nikkor-SW", "Nikkor-AM",
              "AI-S Nikkor", "AF Nikkor", "AF-S Nikkor"],
    "Schneider-Kreuznach": ["Symmar-S", "Symmar", "Apo-Symmar", "Super-Angulon",
                            "Apo-Digitar", "Xenar", "Xenotar", "Tele-Xenar"],
    "Rodenstock": ["Sironar-N", "Sironar-S", "Apo-Sironar", "Grandagon-N",
                   "Apo-Ronar", "Geronar", "Imagon"],
    "Leica": ["Summicron", "Summilux", "Elmar", "Elmarit", "Summaron", "Noctilux"],
    "Zeiss": ["Planar", "Sonnar", "Distagon", "Biogon", "Tessar", "Ultron"],
    "Carl Zeiss Jena": ["Tessar", "Biotar", "Flektogon", "Pancolar"],
    "Fujinon": ["Fujinon-W", "Fujinon-A", "Fujinon-C", "Fujinon-SW"],
    "Mamiya": ["Sekor", "Sekor C", "Sekor Z", "Sekor E"],
    "Canon": ["FD", "New FD", "FL", "EF"],
    "Hasselblad": ["Planar CF", "Sonnar CF", "Distagon CF", "Zeiss Planar T*"],
}

GEAR_VERSION = 2
MOUNT_CHOICES = ["K-mount", "KA-mount", "M42 Screw", "SQ Mount", "TLR", "Fixed", "Other"]

# Original placeholder examples (removed automatically when migrating an old config)
LEGACY_CAMERAS = ["Pentax 67", "Pentax K1000", "Nikon F3", "Hasselblad 500C/M",
                  "Leica M6", "Mamiya RB67", "Toyo 45A (4x5)"]
LEGACY_LENSES = ["Pentax SMC Pentax-M 50mm f/1.7", "Nikon Nikkor 50mm f/1.4",
                 "Schneider-Kreuznach Symmar-S 150mm f/5.6", "Nikon Nikkor-W 150mm f/5.6"]

# Owner's inventory (duplicates merged). (name, mount)
USER_CAMERAS = [
    ("Pentax LX", "KA-mount"), ("Pentax Super-A", "KA-mount"),
    ("Pentax Super Program", "KA-mount"), ("Pentax K1000", "K-mount"),
    ("Pentax Spotmatic SP", "M42 Screw"), ("Pentax ES Spotmatic", "M42 Screw"),
    ("Pentax Spotmatic", "M42 Screw"), ("Yashica Electro 35 GT", "Fixed"),
    ("Yashica Mat EM", "TLR"), ("Minolta Hi-Matic AF", "Fixed"),
    ("Zenza Bronica SQ", "SQ Mount"),
]
USER_LENSES = [
    ("Pentax SMC Pentax-M 50mm f/1.7", "K-mount"),
    ("Pentax SMC Pentax-M 50mm f/1.2", "K-mount"),
    ("Pentax SMC Pentax-M f/2.8", "K-mount"),
    ("Pentax SMC Pentax-A 50mm f/1.7", "KA-mount"),
    ("Pentax SMC Pentax-A* f/1.4", "K-mount"),
    ("Pentax SMC Pentax-A Zoom 35~70mm f/4", "KA-mount"),
    ("Pentax Takumar (Bayonet)", "K-mount"),
    ("Pentax Super-Takumar 50mm f/1.4", "M42 Screw"),
    ("Pentax Super-Takumar 35mm f/3.5", "M42 Screw"),
    ("Pentax Super-Takumar 300mm f/4", "M42 Screw"),
    ("Chinon Auto 50mm f/1.9", "KA-mount"),
    ("Sears Auto 55mm f/1.4", "KA-mount"),
    ("Zenza Bronica Zenzanon-S 80mm f/2.8", "SQ Mount"),
]
DEFAULT_CAMERAS = [n for n, _m in USER_CAMERAS]
DEFAULT_LENSES = [n for n, _m in USER_LENSES]
DEFAULT_MOUNTS = {n: m for n, m in USER_CAMERAS + USER_LENSES}


def natural_key(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def generate_barcode(seed_text: str) -> str:
    """Deterministic ASCII barcode for user-defined stocks."""
    rng = random.Random(zlib.crc32(seed_text.encode("utf-8")))
    out: list[str] = []
    for _ in range(14):
        out.append(rng.choice("||!!!"))
        if rng.random() < 0.6:
            out.append(rng.choice("!|"))
        out.append(rng.choice(" .."))
    return "".join(out).strip()


@dataclass
class Mark:
    """A single grease-pencil mark. Points are normalised to the image rect."""
    kind: str
    pts: list[list[float]]
    seed: int
    size: float = 1.0
    color: str = DEFAULT_BRUSH_COLOR

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "pts": [[round(x, 5), round(y, 5)] for x, y in self.pts], "seed": self.seed,
                "size": round(self.size, 3), "color": self.color}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Mark":
        return Mark(str(d["kind"]), [[float(p[0]), float(p[1])] for p in d.get("pts", [])],
                    int(d.get("seed", 1)), float(d.get("size", 1.0)), str(d.get("color", DEFAULT_BRUSH_COLOR)))


class Frame:
    """One image on the roll."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.thumb: Optional[QPixmap] = None
        self.preview: Optional[QImage] = None
        self.aspect: float = THUMB_W / THUMB_H
        self.marks: list[Mark] = []
        self.error: str = ""  # set when the file could not be decoded

    @property
    def rejected(self) -> bool:
        return any(m.kind == T_REJECT for m in self.marks)

    @property
    def starred(self) -> bool:
        return any(m.kind == T_STAR for m in self.marks)

    @property
    def rating(self) -> int:
        if self.rejected:
            return -1
        return 5 if self.starred else 0

    @property
    def sidecar(self) -> Path:
        return pm_sidecar_path(self.path)


class Roll:
    """A folder of frames plus its equipment assignment."""

    def __init__(self, folder: Path, film: str, camera: str, lens: str) -> None:
        self.folder = folder
        self.film = film
        self.camera = camera
        self.lens = lens
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ALL_EXTS]
        files.sort(key=lambda p: natural_key(p.name))
        self.frames: list[Frame] = [Frame(p) for p in files]

    def load_sidecar_marks(self) -> None:
        for fr in self.frames:
            # ProofMark <= 1.1.1 kept its marks in <image>.rrdata, the file RapidRAW owns;
            # read them from there until the next sync moves them into <image>.pmdata.
            for path in (fr.sidecar, rr_sidecar_path(fr.path)):
                try:
                    if path.exists():
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and "marks" in data:
                            fr.marks = [Mark.from_dict(m) for m in data["marks"]]
                            break
                except (OSError, ValueError, KeyError, TypeError):
                    continue

    def apply_marks(self, table: dict[str, list[dict[str, Any]]]) -> None:
        for fr in self.frames:
            if fr.name in table:
                try:
                    fr.marks = [Mark.from_dict(m) for m in table[fr.name]]
                except (KeyError, TypeError, ValueError):
                    continue

    def marks_table(self) -> dict[str, list[dict[str, Any]]]:
        return {fr.name: [m.to_dict() for m in fr.marks] for fr in self.frames if fr.marks}


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ── Non-destructive metadata: originals are never written to ────────────────
# ProofMark keeps its own data in <image>.pmdata. Artist, copyright and film / gear notes go into
# the "exif" map of RapidRAW's <image>.rrdata sidecar, which RapidRAW applies when it exports.

def pm_sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + ".pmdata")


def rr_sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + ".rrdata")


# EXIF tag id -> key RapidRAW uses in its "exif" map (kamadak-exif tag names).
RR_EXIF_KEYS = {
    0x010E: "ImageDescription", 0x010F: "Make", 0x0110: "Model", 0x0131: "Software", 0x0132: "DateTime",
    0x013B: "Artist", 0x8298: "Copyright", 0x829A: "ExposureTime", 0x829D: "FNumber",
    0x8827: "PhotographicSensitivity", 0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized",
    0x9204: "ExposureBiasValue", 0x920A: "FocalLength", 0xA405: "FocalLengthIn35mmFilm",
    0xA433: "LensMake", 0xA434: "LensModel",
}
# exiftool names (used for RAW files) that differ from RapidRAW's
_EXIFTOOL_RENAMES = {"ISO": "PhotographicSensitivity", "ExposureCompensation": "ExposureBiasValue",
                     "ModifyDate": "DateTime", "CreateDate": "DateTimeDigitized"}


def _rr_format(key: str, value: Any) -> str:
    """Format a value the way RapidRAW displays it (e.g. "1/125 s", "f/2.8", "50 mm")."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, tuple):
        value = value[0] if value else ""
    if isinstance(value, str):
        return value.replace("\x00", "").strip()  # dates stay "YYYY:MM:DD hh:mm:ss", as RapidRAW stores them
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if key == "ExposureTime":
        return f"1/{round(1 / num)} s" if 0 < num < 1 else f"{num:g} s"
    if key == "FNumber":
        return f"f/{num:g}"
    if key == "FocalLength":
        return f"{num:g} mm"
    return f"{num:g}"


def read_rr_exif(image: Path) -> Optional[dict[str, str]]:
    """The image's own EXIF keyed like RapidRAW's map. {} = no EXIF; None = couldn't be read."""
    out: dict[str, str] = {}
    try:
        if image.suffix.lower() in RAW_EXTS:
            tool = shutil.which("exiftool")
            if not tool:
                return None
            res = subprocess.run([tool, "-j", "-n", "-EXIF:all", str(image)],
                                 capture_output=True, text=True, timeout=30, check=False)
            if res.returncode != 0:
                return None
            rows = json.loads(res.stdout or "[]")
            wanted = set(RR_EXIF_KEYS.values())
            for name, value in (rows[0] if rows else {}).items():
                key = _EXIFTOOL_RENAMES.get(name, name)
                if key in wanted:
                    out[key] = _rr_format(key, value)
            if not out:
                return None  # every real RAW file has EXIF: exiftool couldn't read this one
        else:
            with Image.open(image) as img:
                exif = img.getexif()
                tags = dict(exif)
                tags.update(exif.get_ifd(0x8769))
            for tag, key in RR_EXIF_KEYS.items():
                if tag in tags:
                    out[key] = _rr_format(key, tags[tag])
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None
    return {k: v for k, v in out.items() if v}


def merge_rapidraw_exif(image: Path, values: dict[str, str], owned: dict[str, str]) -> Optional[dict[str, str]]:
    """
    Put `values` into the "exif" map of RapidRAW's sidecar, keeping everything else in the file.
    A field is only (over)written when it is empty or still holds what ProofMark wrote last time
    (`owned`), so edits made in RapidRAW win. Returns the fields ProofMark now owns, or None when
    the sidecar was left alone (unreadable, or the image's EXIF couldn't be read to seed it).
    """
    path = rr_sidecar_path(image)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # never overwrite a sidecar we can't parse
        if not isinstance(data, dict):
            return None
        if "marks" in data and "adjustments" not in data:
            data = {}  # written by ProofMark <= 1.1.1; its marks now live in .pmdata
    before = json.dumps(data, sort_keys=True)
    exif = data.get("exif")
    if not isinstance(exif, dict):
        # RapidRAW shows this map instead of the file's EXIF once it exists, so start from the file's.
        exif = read_rr_exif(image)
        if exif is None:
            return None
    new_owned: dict[str, str] = {}
    for key, value in values.items():
        current = str(exif.get(key, "")).replace('"', "").strip()
        if value and (not current or current == "..." or current.startswith("0x") or current == owned.get(key)):
            exif[key] = value
            new_owned[key] = value
    if not new_owned and not path.exists():
        return new_owned  # nothing to hand over: don't create a sidecar just for the file's own EXIF
    data.setdefault("version", 1)
    data.setdefault("rating", 0)
    data.setdefault("adjustments", None)
    data["exif"] = exif
    if json.dumps(data, sort_keys=True) != before:
        atomic_write_json(path, data)
    return new_owned


DEFAULT_SETTINGS: dict[str, Any] = {
    "font_family": "", "font_size": 10, "ui_scale": 100, "accent": "#FFA726",
    "menu_mode": "top", "toolbar_area": "top", "rebate_scale": 100,
    "default_columns": 6, "hover_loupe": True,
    "autosave_secs": 4, "sync_enabled": True, "rapidraw_exif": True,
    "print_direct": False, "print_ink_saver": False, "print_header": True,
    "auto_update_check": True, "update_repo": "",
}


class ConfigStore:
    """Persistent equipment inventory + user profile (~/.config/proofmark/config.json)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = self._defaults()
        self.renames: list[tuple[str, str, str]] = []  # (kind, old, new) pending for open rolls
        self.load()
        self._migrate_gear()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        films = []
        for name, meta in EMULSION_EDGE_DATA.items():
            films.append({"name": name, "iso": meta["iso"], "balance": meta["balance"],
                          "edge": meta["edge"], "dx": meta["dx"], "barcode": meta["barcode"],
                          "favorite": False})
        return {
            "films": films,
            "cameras": list(DEFAULT_CAMERAS),
            "lenses": list(DEFAULT_LENSES),
            "mounts": dict(DEFAULT_MOUNTS),
            "gear_version": GEAR_VERSION,
            "desktop_shortcut_made": False,
            "profile": {"artist": "", "copyright": "", "film": "", "camera": "", "lens": ""},
            "last_import_parent": str(Path.home() / "Pictures"),
        }

    def load(self) -> None:
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for key, value in loaded.items():
                self.data[key] = value
            self.data["gear_version"] = loaded.get("gear_version", 0)
        except (OSError, ValueError):
            pass

    def _migrate_gear(self) -> None:
        """Replace the original placeholder gear with the owner's inventory (keeps custom additions)."""
        if self.data.get("gear_version", 0) >= GEAR_VERSION:
            return
        for key, legacy, mine in (("cameras", LEGACY_CAMERAS, DEFAULT_CAMERAS),
                                  ("lenses", LEGACY_LENSES, DEFAULT_LENSES)):
            kept = [x for x in self.data.get(key, []) if x not in legacy]
            self.data[key] = kept + [x for x in mine if x not in kept]
        mounts = self.data.setdefault("mounts", {})
        for name, mount in DEFAULT_MOUNTS.items():
            mounts.setdefault(name, mount)
        for k in ("camera", "lens"):
            if self.profile.get(k) in LEGACY_CAMERAS + LEGACY_LENSES:
                self.profile[k] = ""
        self.data["gear_version"] = GEAR_VERSION
        self.save()

    @property
    def mounts(self) -> dict[str, str]:
        return self.data.setdefault("mounts", {})

    @property
    def favs(self) -> list[str]:
        return self.data.setdefault("fav_gear", [])

    def is_fav(self, name: str) -> bool:
        return name in self.favs

    def is_favorite(self, kind: str, name: str) -> bool:
        if kind == "film":
            return bool(self.film_meta(name).get("favorite", False))
        return self.is_fav(name)

    def toggle_favorite(self, kind: str, name: str) -> bool:
        """Pin / unpin an item. Returns the new favorite state."""
        if kind == "film":
            for f in self.films:
                if f["name"] == name:
                    f["favorite"] = not f.get("favorite", False)
                    self.save()
                    return bool(f["favorite"])
            return False
        if name in self.favs:
            self.favs.remove(name)
            state = False
        else:
            self.favs.append(name)
            state = True
        self.save()
        return state

    def sorted_gear(self, items: list[str]) -> list[str]:
        return sorted(items, key=lambda s: (s not in self.favs, s.lower()))

    def note_rename(self, kind: str, old: str, new: str) -> None:
        if kind != "film":
            if old in self.mounts:
                self.mounts[new] = self.mounts.pop(old)
            if old in self.favs:
                self.favs[self.favs.index(old)] = new
        if self.profile.get(kind) == old:
            self.profile[kind] = new
        self.renames.append((kind, old, new))

    def setting(self, key: str) -> Any:
        return self.data.setdefault("settings", {}).get(key, DEFAULT_SETTINGS[key])

    def set_setting(self, key: str, value: Any) -> None:
        self.data.setdefault("settings", {})[key] = value

    def forget_gear(self, name: str) -> None:
        self.mounts.pop(name, None)
        if name in self.favs:
            self.favs.remove(name)

    def save(self) -> None:
        try:
            atomic_write_json(CONFIG_FILE, self.data)
        except OSError:
            pass

    # -- accessors -----------------------------------------------------------
    @property
    def films(self) -> list[dict[str, Any]]:
        return self.data["films"]

    @property
    def cameras(self) -> list[str]:
        return self.data["cameras"]

    @property
    def lenses(self) -> list[str]:
        return self.data["lenses"]

    @property
    def profile(self) -> dict[str, str]:
        return self.data["profile"]

    def sorted_films(self) -> list[dict[str, Any]]:
        return sorted(self.films, key=lambda f: (not f.get("favorite", False), f["name"].lower()))

    def film_meta(self, name: str) -> dict[str, Any]:
        for f in self.films:
            if f["name"] == name:
                return f
        return {"name": name, "iso": 400, "balance": "Pan", "edge": name.upper(),
                "dx": "DX 0000", "barcode": generate_barcode(name), "favorite": False}


def fit_rect(box: QRectF, aspect: float) -> QRectF:
    """Largest rect of `aspect` (w/h) centred inside `box`."""
    if box.height() <= 0 or box.width() <= 0:
        return QRectF(box)
    if box.width() / box.height() > aspect:
        h = box.height()
        w = h * aspect
    else:
        w = box.width()
        h = w / aspect
    return QRectF(box.center().x() - w / 2, box.center().y() - h / 2, w, h)


def norm_in_rect(rect: QRectF, pos: QPointF) -> list[float]:
    x = (pos.x() - rect.x()) / max(1e-6, rect.width())
    y = (pos.y() - rect.y()) / max(1e-6, rect.height())
    return [min(1.0, max(0.0, x)), min(1.0, max(0.0, y))]


# ── Textured wax grease pencil shader ───────────────────────────────────────

def _resample(pts: list[QPointF], step: float) -> list[QPointF]:
    """Walk a polyline and emit points every `step` pixels."""
    if not pts:
        return []
    out = [QPointF(pts[0])]
    carry = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b.x() - a.x(), b.y() - a.y())
        if seg < 1e-6:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append(QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t))
            d += step
        carry = seg - (d - step)
    out.append(QPointF(pts[-1]))
    return out


def render_heavy_grease_stroke(painter: QPainter, pts: list[QPointF], base_width: float,
                               color: QColor, seed: int) -> None:
    """
    Multi-pass procedural china-marker stroke.
    Deterministic per seed so a mark never shimmers between repaints.
    Passes: broad translucent body -> jittered mid layers -> thin dry core,
    with random dry skips (wax friction), width/opacity variation and crumbs.
    """
    if not pts:
        return
    if len(pts) == 1:
        pts = [pts[0], QPointF(pts[0].x() + 0.1, pts[0].y())]
    dense = _resample(pts, max(0.8, base_width * 0.32))
    if len(dense) < 2:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    passes = ((1.00, 0.26, 0.00), (0.78, 0.32, 0.00), (0.52, 0.42, 0.10), (0.26, 0.60, 0.16))
    for idx, (w_mul, a_mul, skip) in enumerate(passes):
        rng = random.Random(seed * 131 + idx * 7919)
        jit = base_width * 0.20
        pen = QPen()
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        prev: Optional[QPointF] = None
        for p in dense:
            q = QPointF(p.x() + rng.gauss(0, jit * 0.5), p.y() + rng.gauss(0, jit * 0.5))
            if prev is not None and rng.random() >= skip:
                c = QColor(color)
                c.setAlphaF(min(1.0, a_mul * (0.55 + 0.9 * rng.random())))
                pen.setColor(c)
                pen.setWidthF(max(0.6, base_width * w_mul * (0.72 + 0.56 * rng.random())))
                painter.setPen(pen)
                painter.drawLine(prev, q)
            prev = q
    # Crumb dispersion
    rng = random.Random(seed * 977 + 5)
    painter.setPen(Qt.NoPen)
    for p in dense:
        if rng.random() < 0.32:
            ang = rng.random() * math.tau
            dist = abs(rng.gauss(0, base_width * 0.85)) + base_width * 0.42
            r = rng.uniform(0.25, 1.0) * base_width * 0.17
            c = QColor(color)
            c.setAlphaF(rng.uniform(0.25, 0.75))
            painter.setBrush(c)
            painter.drawEllipse(QPointF(p.x() + math.cos(ang) * dist, p.y() + math.sin(ang) * dist), r, r)
    painter.restore()


def mark_polylines(mark: Mark, rect: QRectF) -> list[list[QPointF]]:
    """Expand a mark into stroke polylines in pixel space of `rect`."""
    def P(pt: list[float]) -> QPointF:
        return QPointF(rect.x() + pt[0] * rect.width(), rect.y() + pt[1] * rect.height())

    k = mark.kind
    if not mark.pts:
        return []
    if k == T_LINE:
        return [[P(p) for p in mark.pts]] if len(mark.pts) >= 2 else []
    if k == T_ARROW:
        if len(mark.pts) < 2:
            return []
        a, b = P(mark.pts[0]), P(mark.pts[1])
        length = math.hypot(b.x() - a.x(), b.y() - a.y())
        if length < 1.0:
            return []
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = (min(length * 0.35, rect.width() * 0.07) + 2.0) * max(0.6, mark.size)
        h1 = QPointF(b.x() - head * math.cos(ang - 0.5), b.y() - head * math.sin(ang - 0.5))
        h2 = QPointF(b.x() - head * math.cos(ang + 0.5), b.y() - head * math.sin(ang + 0.5))
        return [[a, b], [h1, b, h2]]
    if k == T_RING:
        if len(mark.pts) < 2:
            return []
        a, b = P(mark.pts[0]), P(mark.pts[1])
        cx, cy = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
        rx, ry = abs(b.x() - a.x()) / 2, abs(b.y() - a.y()) / 2
        if rx < 1.0 and ry < 1.0:
            return []
        steps = 72
        poly: list[QPointF] = []
        for s in range(int(steps * 1.08) + 1):  # slight overlap: hand-closed loop
            t = math.tau * s / steps + 0.6
            c, sn = math.cos(t), math.sin(t)
            poly.append(QPointF(cx + rx * math.copysign(abs(c) ** 0.5, c),
                                cy + ry * math.copysign(abs(sn) ** 0.5, sn)))  # superellipse n=4
        return [poly]
    if k == T_CROP and len(mark.pts) >= 2:
        box = QRectF(P(mark.pts[0]), P(mark.pts[1])).normalized()
        ext = max(4.0, 0.12 * min(box.width(), box.height()))  # crop-mark overshoot
        return [[QPointF(box.left() - ext, box.top()), QPointF(box.right() + ext, box.top())],
                [QPointF(box.left() - ext, box.bottom()), QPointF(box.right() + ext, box.bottom())],
                [QPointF(box.left(), box.top() - ext), QPointF(box.left(), box.bottom() + ext)],
                [QPointF(box.right(), box.top() - ext), QPointF(box.right(), box.bottom() + ext)]]
    c = P(mark.pts[0])
    r = 0.075 * min(rect.width(), rect.height()) * mark.size
    if k == T_STAR:
        rr = r * 1.15
        return [[QPointF(c.x() + rr * math.cos(-math.pi / 2 + 4 * math.pi / 5 * i),
                         c.y() + rr * math.sin(-math.pi / 2 + 4 * math.pi / 5 * i)) for i in range(6)]]
    if k == T_REJECT:
        box = [QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() - r),
               QPointF(c.x() + r, c.y() + r), QPointF(c.x() - r, c.y() + r), QPointF(c.x() - r, c.y() - r)]
        return [box, [QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r)],
                [QPointF(c.x() + r, c.y() - r), QPointF(c.x() - r, c.y() + r)]]
    if k == T_PUSH:
        return [[QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y())],
                [QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r)]]
    if k == T_PULL:
        return [[QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y())]]
    if k == T_CROP:
        g, e = r * 0.45, r * 1.15
        return [[QPointF(c.x() - g, c.y() - e), QPointF(c.x() - g, c.y() + e)],
                [QPointF(c.x() + g, c.y() - e), QPointF(c.x() + g, c.y() + e)],
                [QPointF(c.x() - e, c.y() - g), QPointF(c.x() + e, c.y() - g)],
                [QPointF(c.x() - e, c.y() + g), QPointF(c.x() + e, c.y() + g)]]
    return []


POINT_KINDS = {T_STAR, T_REJECT, T_PUSH, T_PULL}
HANDLE_CURSORS = {
    "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor, "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor, "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "a": Qt.SizeAllCursor, "b": Qt.SizeAllCursor,
}


def is_box_mark(m: Mark) -> bool:
    return m.kind in (T_CROP, T_RING) and len(m.pts) >= 2


def is_point_mark(m: Mark) -> bool:
    return m.kind in POINT_KINDS or (m.kind == T_CROP and len(m.pts) == 1)


def point_unit(m: Mark, rect: QRectF) -> float:
    """Glyph radius in pixels at size 1.0 for point marks."""
    return 0.075 * min(rect.width(), rect.height()) * (1.15 if m.kind in (T_STAR, T_CROP) else 1.0)


def _px(rect: QRectF, pt: list[float]) -> QPointF:
    return QPointF(rect.x() + pt[0] * rect.width(), rect.y() + pt[1] * rect.height())


def _norm(rect: QRectF, x: float, y: float) -> list[float]:
    return [min(1.0, max(0.0, (x - rect.x()) / max(1e-6, rect.width()))),
            min(1.0, max(0.0, (y - rect.y()) / max(1e-6, rect.height())))]


def mark_bounds_px(mark: Mark, rect: QRectF) -> QRectF:
    if is_box_mark(mark) or (mark.kind == T_ARROW and len(mark.pts) >= 2):
        return QRectF(_px(rect, mark.pts[0]), _px(rect, mark.pts[1])).normalized()
    if mark.kind == T_LINE and mark.pts:
        xs = [_px(rect, p).x() for p in mark.pts]
        ys = [_px(rect, p).y() for p in mark.pts]
        return QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))
    c = _px(rect, mark.pts[0])
    r = point_unit(mark, rect) * mark.size
    return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)


def handle_points(mark: Mark, rect: QRectF) -> dict[str, QPointF]:
    if mark.kind == T_ARROW and len(mark.pts) >= 2:
        return {"a": _px(rect, mark.pts[0]), "b": _px(rect, mark.pts[1])}
    b = mark_bounds_px(mark, rect)
    if is_point_mark(mark):
        return {"nw": b.topLeft(), "ne": b.topRight(), "se": b.bottomRight(), "sw": b.bottomLeft()}
    cx, cy = b.center().x(), b.center().y()
    return {"nw": b.topLeft(), "n": QPointF(cx, b.top()), "ne": b.topRight(), "e": QPointF(b.right(), cy),
            "se": b.bottomRight(), "s": QPointF(cx, b.bottom()), "sw": b.bottomLeft(), "w": QPointF(b.left(), cy)}


def hit_handle(mark: Mark, rect: QRectF, pos: QPointF, radius: float = 9.0) -> Optional[str]:
    best, best_d = None, radius
    for name, pt in handle_points(mark, rect).items():
        d = math.hypot(pos.x() - pt.x(), pos.y() - pt.y())
        if d <= best_d:
            best, best_d = name, d
    return best


def _seg_dist(p: QPointF, a: QPointF, b: QPointF) -> float:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return math.hypot(p.x() - a.x(), p.y() - a.y())
    t = max(0.0, min(1.0, ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / l2))
    return math.hypot(p.x() - (a.x() + t * dx), p.y() - (a.y() + t * dy))


def mark_hit(mark: Mark, rect: QRectF, pos: QPointF, tol: float = 9.0) -> bool:
    """True if `pos` touches the mark (point glyph area, or near any of its strokes)."""
    if is_point_mark(mark):
        return mark_bounds_px(mark, rect).adjusted(-4, -4, 4, 4).contains(pos)
    for poly in mark_polylines(mark, rect):
        for a, b in zip(poly, poly[1:]):
            if _seg_dist(pos, a, b) <= tol:
                return True
    return False


def default_box_pts(rect: QRectF, n: list[float], scale: float) -> list[list[float]]:
    half = 0.15 * min(rect.width(), rect.height()) * scale
    hx = min(0.49, half / max(1e-6, rect.width()))
    hy = min(0.49, half / max(1e-6, rect.height()))
    cx, cy = min(1 - hx, max(hx, n[0])), min(1 - hy, max(hy, n[1]))
    return [[cx - hx, cy - hy], [cx + hx, cy + hy]]


def move_mark(mark: Mark, pts0: list[list[float]], dx: float, dy: float) -> None:
    xs, ys = [p[0] for p in pts0], [p[1] for p in pts0]
    dx = max(-min(xs), min(1.0 - max(xs), dx))
    dy = max(-min(ys), min(1.0 - max(ys), dy))
    mark.pts = [[p[0] + dx, p[1] + dy] for p in pts0]


def resize_mark(mark: Mark, rect: QRectF, handle: str, pos: QPointF, orig: dict[str, Any]) -> None:
    pts0 = [list(p) for p in orig["pts"]]
    if mark.kind == T_ARROW:
        mark.pts = pts0
        mark.pts[0 if handle == "a" else 1] = _norm(rect, pos.x(), pos.y())
        return
    if is_point_mark(mark):  # corner handle: uniform scale about the centre
        c = _px(rect, pts0[0])
        r = math.hypot(pos.x() - c.x(), pos.y() - c.y()) / math.sqrt(2.0)
        mark.size = max(0.25, min(8.0, r / max(1.0, point_unit(mark, rect))))
        return
    b: QRectF = orig["bounds"]
    left, top, right, bottom = b.left(), b.top(), b.right(), b.bottom()
    if "w" in handle:
        left = pos.x()
    if "e" in handle:
        right = pos.x()
    if "n" in handle:
        top = pos.y()
    if "s" in handle:
        bottom = pos.y()
    nb = QRectF(QPointF(left, top), QPointF(right, bottom)).normalized()
    nb.setWidth(max(6.0, nb.width()))
    nb.setHeight(max(6.0, nb.height()))
    if is_box_mark(mark):
        mark.pts = [_norm(rect, nb.left(), nb.top()), _norm(rect, nb.right(), nb.bottom())]
    else:  # freehand line: scale every point from the old bounds into the new ones
        bw, bh = max(1e-6, b.width()), max(1e-6, b.height())
        out = []
        for pt in pts0:
            o = _px(rect, pt)
            out.append(_norm(rect, nb.left() + (o.x() - b.left()) / bw * nb.width(),
                             nb.top() + (o.y() - b.top()) / bh * nb.height()))
        mark.pts = out


def draw_mark(painter: QPainter, mark: Mark, rect: QRectF, width: float) -> None:
    for i, poly in enumerate(mark_polylines(mark, rect)):
        render_heavy_grease_stroke(painter, poly, width * mark.size, QColor(mark.color), mark.seed + i * 17)


def draw_barcode(painter: QPainter, rect: QRectF, code: str, color: QColor) -> None:
    widths = {"|": 3.0, "!": 1.0, " ": 2.0, ".": 1.0}
    total = sum(widths.get(ch, 1.0) for ch in code) or 1.0
    unit = rect.width() / total
    x = rect.x()
    for ch in code:
        w = widths.get(ch, 1.0) * unit
        if ch in "|!":
            painter.fillRect(QRectF(x, rect.y(), max(0.6, w), rect.height()), color)
        x += w


# ── Stylesheet ──────────────────────────────────────────────────────────────

STYLE = """
* { font-family: "Inter", "Cantarell", "DejaVu Sans", sans-serif; }
QMainWindow, QDialog, QWidget { background: #0A0A0A; color: #FFFFFF; }
QMenuBar, QToolBar, QStatusBar { background: #141414; color: #FFFFFF; border: none; }
QMenuBar::item:selected, QMenu::item:selected { background: #FFA726; color: #000000; }
QMenu { background: #141414; border: 1px solid #2a2a2a; }
QToolBar { spacing: 4px; padding: 4px; border-bottom: 1px solid #222; }
QToolButton { background: #1b1b1b; color: #FFFFFF; border: 1px solid #2a2a2a; padding: 5px 9px; border-radius: 3px; }
QToolButton:hover { border-color: #FFA726; }
QToolButton:checked { background: #FFA726; color: #000000; font-weight: bold; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #141414; color: #bbbbbb; padding: 7px 16px; border: 1px solid #222; border-bottom: none; }
QTabBar::tab:selected { color: #000000; background: #FFA726; font-weight: bold; }
QPushButton { background: #1b1b1b; color: #FFFFFF; border: 1px solid #333; padding: 6px 14px; border-radius: 3px; }
QPushButton:hover { border-color: #FFA726; color: #FFA726; }
QComboBox, QLineEdit, QSpinBox, QListWidget { background: #141414; color: #FFFFFF; border: 1px solid #333;
    padding: 4px 6px; selection-background-color: #FFA726; selection-color: #000000; }
QComboBox QAbstractItemView { background: #141414; color: #FFFFFF; selection-background-color: #FFA726; selection-color: #000; }
QListWidget::item { padding: 5px; }
QLabel#amber { color: #FFA726; font-weight: bold; letter-spacing: 1px; }
QScrollBar:vertical { background: #0A0A0A; width: 12px; }
QScrollBar::handle:vertical { background: #333; min-height: 30px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #FFA726; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip { background: #141414; color: #FFA726; border: 1px solid #FFA726; }
QToolBar QLabel { background: transparent; color: #dddddd; padding: 0 4px; }
QSlider::groove:horizontal { height: 5px; background: #333; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #FFA726; border-radius: 2px; }
QSlider::handle:horizontal { background: #FFA726; width: 18px; height: 18px; margin: -7px 0; border-radius: 9px; }
QSpinBox { padding-right: 18px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: border; width: 16px; background: #222222; border-left: 1px solid #333; }
QSpinBox::up-button { subcontrol-position: top right; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #3a3a3a; }
QSpinBox::up-arrow { image: url("@ARROW_DIR@/up.png"); width: 8px; height: 8px; }
QSpinBox::down-arrow { image: url("@ARROW_DIR@/down.png"); width: 8px; height: 8px; }
"""

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "proofmark"


def _spin_arrow_dir() -> str:
    """Light arrow images for spin boxes (the dark stylesheet hides Qt's own arrows)."""
    out = CACHE_DIR / "ui"
    try:
        out.mkdir(parents=True, exist_ok=True)
        for name, tip_y, base_y in (("up", 2.0, 8.0), ("down", 8.0, 2.0)):
            target = out / f"{name}.png"
            if target.exists():
                continue
            img = QImage(10, 10, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#DDDDDD"))
            p.drawPolygon([QPointF(5, tip_y), QPointF(9, base_y), QPointF(1, base_y)])
            p.end()
            img.save(str(target), "PNG")
    except OSError:
        pass
    return out.as_posix()

_STYLE_FONT_RULE = '* { font-family: "Inter", "Cantarell", "DejaVu Sans", sans-serif; }'


def build_style(family: str, size: int, accent: str) -> str:
    """STYLE with the user's font and accent colour applied."""
    fam = f'"{family}", ' if family else ""
    css = STYLE.replace(_STYLE_FONT_RULE, '* { font-family: ' + fam +
                        '"Inter", "Cantarell", "DejaVu Sans", sans-serif; font-size: ' + str(size) + 'pt; }')
    return css.replace("#FFA726", accent).replace("@ARROW_DIR@", _spin_arrow_dir())

# ════════════════════════════════════════════════════════════════════════════
# 2. DIALOGS & CUSTOM WIDGETS
# ════════════════════════════════════════════════════════════════════════════


class AddFilmDialog(QDialog):
    """Create or edit a film stock with a balance tag."""

    def __init__(self, parent: Optional[QWidget] = None, film: Optional[dict[str, Any]] = None) -> None:
        super().__init__(parent)
        self.film = film
        self.setWindowTitle("Edit Film Stock" if film else "Add Film Stock")
        self.name_edit = QLineEdit()
        self.iso_spin = QSpinBox()
        self.iso_spin.setRange(6, 6400)
        self.iso_spin.setValue(400)
        self.balance_combo = QComboBox()
        for code, label in BALANCE_TAGS.items():
            self.balance_combo.addItem(f"({code}) {label}", code)
        self.balance_combo.setCurrentIndex(2)
        self.edge_edit = QLineEdit()
        self.edge_edit.setPlaceholderText("Edge print, e.g. ACME PAN 400 (optional)")
        self.dx_edit = QLineEdit()
        self.dx_edit.setPlaceholderText("DX code, e.g. DX 5063 TX (optional)")
        if film:
            self.name_edit.setText(film["name"])
            self.iso_spin.setValue(int(film.get("iso", 400)))
            idx = self.balance_combo.findData(film.get("balance", "Pan"))
            if idx >= 0:
                self.balance_combo.setCurrentIndex(idx)
            self.edge_edit.setText(film.get("edge", ""))
            self.dx_edit.setText(film.get("dx", ""))
        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Box ISO", self.iso_spin)
        form.addRow("Balance", self.balance_combo)
        form.addRow("Edge text", self.edge_edit)
        form.addRow("DX code", self.dx_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(420, 240)

    def _accept(self) -> None:
        if self.name_edit.text().strip():
            self.accept()

    def result_film(self) -> dict[str, Any]:
        name = self.name_edit.text().strip()
        base: dict[str, Any] = dict(self.film) if self.film else {"barcode": generate_barcode(name), "favorite": False}
        base.update({"name": name, "iso": self.iso_spin.value(),
                     "balance": self.balance_combo.currentData(),
                     "edge": self.edge_edit.text().strip() or name.upper(),
                     "dx": self.dx_edit.text().strip() or "DX 0000"})
        return base


class AddLensDialog(QDialog):
    """Lens entry with manufacturer/series auto-complete."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Lens")
        self.maker = QComboBox()
        self.maker.setEditable(True)
        self.maker.addItems(LENS_MAKERS)
        self.maker.setCurrentIndex(-1)
        self.series = QComboBox()
        self.series.setEditable(True)
        self.focal = QLineEdit()
        self.focal.setPlaceholderText("e.g. 50mm")
        self.aperture = QLineEdit()
        self.aperture.setPlaceholderText("e.g. f/1.7")
        for combo in (self.maker, self.series):
            comp = combo.completer()
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            comp.setCompletionMode(QCompleter.PopupCompletion)
        self.maker.currentTextChanged.connect(self._refresh_series)
        self._refresh_series("")
        self.preview = QLabel("")
        self.preview.setObjectName("amber")
        for w in (self.maker.lineEdit(), self.series.lineEdit(), self.focal, self.aperture):
            w.textChanged.connect(self._update_preview)
        form = QFormLayout()
        form.addRow("Manufacturer", self.maker)
        form.addRow("Optical series", self.series)
        form.addRow("Focal length", self.focal)
        form.addRow("Max aperture", self.aperture)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.preview)
        lay.addWidget(buttons)
        self.resize(460, 260)

    def _refresh_series(self, maker: str) -> None:
        options = LENS_SERIES.get(maker.strip())
        if options is None:
            options = sorted({s for lst in LENS_SERIES.values() for s in lst})
        current = self.series.currentText()
        self.series.blockSignals(True)
        self.series.clear()
        self.series.addItems(options)
        self.series.setEditText(current)
        self.series.blockSignals(False)

    def lens_string(self) -> str:
        parts = [self.maker.currentText().strip(), self.series.currentText().strip(),
                 self.focal.text().strip(), self.aperture.text().strip()]
        return " ".join(p for p in parts if p)

    def _update_preview(self) -> None:
        self.preview.setText(self.lens_string())

    def _accept(self) -> None:
        if self.lens_string():
            self.accept()


class EditGearDialog(QDialog):
    """Edit a camera / lens name and mount."""

    def __init__(self, noun: str, name: str, mount: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit {noun}")
        self.name_edit = QLineEdit(name)
        self.mount = QComboBox()
        self.mount.setEditable(True)
        self.mount.addItems(MOUNT_CHOICES)
        self.mount.setEditText(mount)
        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Mount", self.mount)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(460, 150)

    def _accept(self) -> None:
        if self.name_edit.text().strip():
            self.accept()

    def values(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.mount.currentText().strip()


class StringListTab(QWidget):
    """Cameras / lenses list: favorites pin to top; right-click menu; mount per item."""

    def __init__(self, cfg: ConfigStore, items: list[str], adder: Callable[[QWidget], Optional[str]],
                 on_change: Callable[[], None], noun: str, kind: str) -> None:
        super().__init__()
        self.cfg = cfg
        self.items = items
        self.adder = adder
        self.on_change = on_change
        self.noun = noun
        self.kind = kind
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(lambda _i: self._edit())
        hint = QLabel("Right-click an item for Favorite / Edit / Duplicate / Delete. Favorites (★) pin to the top.")
        hint.setStyleSheet("color:#888;")
        row = QHBoxLayout()
        for label, slot in ((f"Add {noun}", self._add), ("Toggle Favorite", self._toggle_fav),
                            ("Edit…", self._edit), ("Duplicate", self._duplicate), ("Delete", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.list)
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for s in self.cfg.sorted_gear(self.items):
            mount = self.cfg.mounts.get(s, "")
            fav = self.cfg.is_fav(s)
            item = QListWidgetItem(("★ " if fav else "") + s + (f"   —   {mount}" if mount else ""))
            item.setData(Qt.UserRole, s)
            if fav:
                item.setForeground(C_AMBER)
            self.list.addItem(item)

    def _current(self) -> Optional[str]:
        item = self.list.currentItem()
        name = item.data(Qt.UserRole) if item else None
        return name if name in self.items else None

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if not item:
            return
        self.list.setCurrentItem(item)
        name = self._current()
        if not name:
            return
        menu = QMenu(self)
        a_fav = menu.addAction("Remove from Favorites" if self.cfg.is_fav(name) else "Add to Favorites")
        a_edit = menu.addAction("Edit…")
        a_dup = menu.addAction("Duplicate")
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen == a_fav:
            self._toggle_fav()
        elif chosen == a_edit:
            self._edit()
        elif chosen == a_dup:
            self._duplicate()
        elif chosen == a_del:
            self._delete()

    def _add(self) -> None:
        text = self.adder(self)
        if text and text not in self.items:
            self.items.append(text)
            mount, ok = QInputDialog.getItem(self, "Mount", f"Mount for “{text}”:", MOUNT_CHOICES, 0, True)
            if ok and mount.strip():
                self.cfg.mounts[text] = mount.strip()
            self.on_change()
            self.refresh()

    def _toggle_fav(self) -> None:
        name = self._current()
        if name:
            self.cfg.toggle_favorite(self.kind, name)
            self.on_change()
            self.refresh()

    def _edit(self) -> None:
        old = self._current()
        if not old:
            return
        dlg = EditGearDialog(self.noun, old, self.cfg.mounts.get(old, ""), self)
        if dlg.exec() != QDialog.Accepted:
            return
        new, mount = dlg.values()
        if new != old:
            if new in self.items:
                QMessageBox.warning(self, "Duplicate", f"A {self.noun.lower()} with that name already exists.")
                return
            self.items[self.items.index(old)] = new
            self.cfg.note_rename(self.kind, old, new)
        if mount:
            self.cfg.mounts[new] = mount
        else:
            self.cfg.mounts.pop(new, None)
        self.on_change()
        self.refresh()

    def _duplicate(self) -> None:
        name = self._current()
        if not name:
            return
        new, n = f"{name} (copy)", 2
        while new in self.items:
            new, n = f"{name} (copy {n})", n + 1
        self.items.append(new)
        if name in self.cfg.mounts:
            self.cfg.mounts[new] = self.cfg.mounts[name]
        self.on_change()
        self.refresh()

    def _delete(self) -> None:
        name = self._current()
        if name and QMessageBox.question(self, "Delete", f"Delete “{name}”?") == QMessageBox.Yes:
            self.items.remove(name)
            self.cfg.forget_gear(name)
            self.on_change()
            self.refresh()


class EquipmentManagerDialog(QDialog):
    """Films / Camera Bodies / Lenses inventory."""

    changed = Signal()
    TAB_INDEX = {"film": 0, "camera": 1, "lens": 2}

    def __init__(self, cfg: ConfigStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Equipment Inventory")
        self.resize(720, 560)
        self.tabs = QTabWidget()

        films = QWidget()
        self.film_list = QListWidget()
        self.film_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.film_list.customContextMenuRequested.connect(self._film_menu)
        self.film_list.itemDoubleClicked.connect(lambda _i: self._edit_film())
        row = QHBoxLayout()
        for label, slot in (("Add Film Stock", self._add_film), ("Toggle Favorite", self._toggle_favorite),
                            ("Edit…", self._edit_film), ("Duplicate", self._duplicate_film),
                            ("Delete", self._delete_film)):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        row.addStretch(1)
        hint = QLabel("Right-click a stock for Favorite / Edit / Duplicate / Delete. Favorites (★) pin to the top.")
        hint.setStyleSheet("color:#888;")
        fl = QVBoxLayout(films)
        fl.addWidget(hint)
        fl.addWidget(self.film_list)
        fl.addLayout(row)

        self.tabs.addTab(films, "Film Emulsions")
        self.tabs.addTab(StringListTab(cfg, cfg.cameras, lambda p: self._ask_text(p, "Camera body"),
                                       self._notify, "Camera", "camera"), "Camera Bodies")
        self.tabs.addTab(StringListTab(cfg, cfg.lenses, self._ask_lens, self._notify, "Lens", "lens"), "Lenses")
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addWidget(close, 0, Qt.AlignRight)
        self.refresh_films()

    def select_tab(self, kind: str) -> None:
        self.tabs.setCurrentIndex(self.TAB_INDEX.get(kind, 0))

    def _notify(self) -> None:
        self.cfg.save()
        self.changed.emit()

    @staticmethod
    def _ask_text(parent: QWidget, label: str) -> Optional[str]:
        text, ok = QInputDialog.getText(parent, f"Add {label}", f"{label} name:")
        return text.strip() if ok and text.strip() else None

    @staticmethod
    def _ask_lens(parent: QWidget) -> Optional[str]:
        dlg = AddLensDialog(parent)
        return dlg.lens_string() if dlg.exec() == QDialog.Accepted else None

    def refresh_films(self) -> None:
        self.film_list.clear()
        for f in self.cfg.sorted_films():
            star = "★ " if f.get("favorite") else ""
            item = QListWidgetItem(f"{star}{f['name']}   ·   ISO {f['iso']}   ({f['balance']})")
            item.setData(Qt.UserRole, f["name"])
            if f.get("favorite"):
                item.setForeground(C_AMBER)
            self.film_list.addItem(item)

    def _selected_film(self) -> Optional[dict[str, Any]]:
        item = self.film_list.currentItem()
        if not item:
            return None
        name = item.data(Qt.UserRole)
        return next((f for f in self.cfg.films if f["name"] == name), None)

    def _film_menu(self, pos) -> None:
        item = self.film_list.itemAt(pos)
        if not item:
            return
        self.film_list.setCurrentItem(item)
        film = self._selected_film()
        if not film:
            return
        menu = QMenu(self)
        a_fav = menu.addAction("Remove from Favorites" if film.get("favorite") else "Add to Favorites")
        a_edit = menu.addAction("Edit…")
        a_dup = menu.addAction("Duplicate")
        menu.addSeparator()
        a_del = menu.addAction("Delete")
        chosen = menu.exec(self.film_list.mapToGlobal(pos))
        if chosen == a_fav:
            self._toggle_favorite()
        elif chosen == a_edit:
            self._edit_film()
        elif chosen == a_dup:
            self._duplicate_film()
        elif chosen == a_del:
            self._delete_film()

    def _add_film(self) -> None:
        dlg = AddFilmDialog(self)
        if dlg.exec() == QDialog.Accepted:
            film = dlg.result_film()
            if any(f["name"] == film["name"] for f in self.cfg.films):
                QMessageBox.warning(self, "Duplicate", "A film with that name already exists.")
                return
            self.cfg.films.append(film)
            self._notify()
            self.refresh_films()

    def _toggle_favorite(self) -> None:
        film = self._selected_film()
        if film:
            self.cfg.toggle_favorite("film", film["name"])
            self._notify()
            self.refresh_films()

    def _edit_film(self) -> None:
        film = self._selected_film()
        if not film:
            return
        old = film["name"]
        dlg = AddFilmDialog(self, film)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.result_film()
        if new["name"] != old and any(f["name"] == new["name"] for f in self.cfg.films):
            QMessageBox.warning(self, "Duplicate", "A film with that name already exists.")
            return
        film.update(new)
        if new["name"] != old:
            self.cfg.note_rename("film", old, new["name"])
        self._notify()
        self.refresh_films()

    def _duplicate_film(self) -> None:
        film = self._selected_film()
        if not film:
            return
        names = {f["name"] for f in self.cfg.films}
        new_name, n = f"{film['name']} (copy)", 2
        while new_name in names:
            new_name, n = f"{film['name']} (copy {n})", n + 1
        clone = dict(film)
        clone.update({"name": new_name, "favorite": False})
        self.cfg.films.append(clone)
        self._notify()
        self.refresh_films()

    def _delete_film(self) -> None:
        film = self._selected_film()
        if film and QMessageBox.question(self, "Delete", f"Delete “{film['name']}”?") == QMessageBox.Yes:
            self.cfg.films.remove(film)
            self._notify()
            self.refresh_films()


class UserProfileDialog(QDialog):
    """Photographer identity and default startup equipment."""

    def __init__(self, cfg: ConfigStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("User Profile & Defaults")
        p = cfg.profile
        self.artist = QLineEdit(p.get("artist", ""))
        self.copyright = QLineEdit(p.get("copyright", ""))
        self.copyright.setPlaceholderText("© 2026 Your Name. All rights reserved.")
        self.film = QComboBox()
        self.camera = QComboBox()
        self.lens = QComboBox()
        self.film.addItem("(none)", "")
        for f in cfg.sorted_films():
            self.film.addItem(("★ " if f.get("favorite") else "") + f["name"], f["name"])
        self.camera.addItem("(none)", "")
        for c in cfg.sorted_gear(cfg.cameras):
            self.camera.addItem(("★ " if cfg.is_fav(c) else "") + c, c)
        self.lens.addItem("(none)", "")
        for l in cfg.sorted_gear(cfg.lenses):
            self.lens.addItem(("★ " if cfg.is_fav(l) else "") + l, l)
        for combo, key in ((self.film, "film"), (self.camera, "camera"), (self.lens, "lens")):
            idx = combo.findData(p.get(key, ""))
            combo.setCurrentIndex(max(0, idx))
        form = QFormLayout()
        form.addRow("Photographer / Artist", self.artist)
        form.addRow("Copyright notice", self.copyright)
        form.addRow("Default film", self.film)
        form.addRow("Default camera", self.camera)
        form.addRow("Default lens", self.lens)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(480, 300)

    def _save(self) -> None:
        self.cfg.profile.update({
            "artist": self.artist.text().strip(), "copyright": self.copyright.text().strip(),
            "film": self.film.currentData() or "", "camera": self.camera.currentData() or "",
            "lens": self.lens.currentData() or ""})
        self.cfg.save()
        self.accept()


class AboutDialog(QDialog):
    """Help ▸ About ProofMark / About the Developer."""

    def __init__(self, cfg: ConfigStore, start_tab: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About ProofMark")
        self.resize(620, 520)
        tabs = QTabWidget()
        tabs.addTab(self._page(self._app_html(), with_icon=True), "About ProofMark")
        tabs.addTab(self._page(self._dev_html(cfg), with_icon=False), "About the Developer")
        tabs.addTab(self._history_widget(), "Version History")
        tabs.setCurrentIndex(start_tab)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(close, 0, Qt.AlignRight)

    @staticmethod
    def _page(html: str, with_icon: bool) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(14, 14, 14, 14)
        if with_icon:
            icon = QLabel()
            icon.setPixmap(build_icon_pixmap(128).scaled(112, 112, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            icon.setAlignment(Qt.AlignTop)
            row.addWidget(icon)
        text = QLabel(html)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        row.addWidget(text, 1)
        return w

    @staticmethod
    def _app_html() -> str:
        return (
            f'<h2 style="color:{ACCENT_HEX};">{APP_NAME} <span style="color:#888;font-size:12pt;">v{APP_VERSION} · build {BUILD_NUMBER}</span></h2>'
            "<p><b>Darkroom contact-sheet proofing for film photographers.</b></p>"
            f"<p style='color:#888;'>{escape(version_string(True))}<br>Install type: {install_kind()}</p>"
            "<p>ProofMark lays out a roll of scans as a contact sheet with simulated emulsion rebate "
            "(frame numbers, edge print and barcodes) and lets you mark it up the way you would in the "
            "darkroom: with a wax grease pencil.</p>"
            "<ul>"
            "<li>Star picks, boxed rejects, exposure push / pull, crop framing, lines, arrows and rings — "
            "click to place, click-drag to size, in four wax colours</li>"
            "<li>Hover loupe with magnification, and 2-up compare</li>"
            "<li>Equipment inventory: films, camera bodies and lenses with favorites and mounts</li>"
            "<li>Non-destructive: marks live in <code>.pmdata</code> / <code>.xmp</code> sidecars, and artist, copyright and gear go into RapidRAW's <code>.rrdata</code>. Your original files are never modified</li>"
            "<li>Automatic session recovery</li></ul>"
            "<p style='color:#888;'>Built with Python, Qt 6 (PySide6), Pillow and rawpy.<br>"
            f"Settings: <code>{CONFIG_DIR}</code></p>")

    @staticmethod
    def _history_widget() -> QWidget:
        html = [f"<h2 style='color:{ACCENT_HEX};'>Version History</h2>",
                f"<p style='color:#888;'>You are running <b>{APP_VERSION}</b> (build {BUILD_NUMBER}, {BUILD_DATE}).</p>"]
        for ver, date, notes in CHANGELOG:
            html.append(f"<h3>v{ver} <span style='color:#888;font-size:10pt;'>{date}</span></h3><ul>")
            html.extend(f"<li>{escape(n)}</li>" for n in notes)
            html.append("</ul>")
        view = QTextBrowser()
        view.setHtml("".join(html))
        view.setOpenExternalLinks(True)
        return view

    @staticmethod
    def _dev_html(cfg: ConfigStore) -> str:
        name = DEV_INFO["name"] or cfg.profile.get("artist", "") or "Your name here"
        parts = [f'<h2 style="color:{ACCENT_HEX};">{escape(name)}</h2>']
        if DEV_INFO["role"]:
            parts.append(f"<p><b>{escape(DEV_INFO['role'])}</b></p>")
        parts.append(f"<p>{escape(DEV_INFO['bio'])}</p>" if DEV_INFO["bio"] else
                     "<p>ProofMark was designed and built for real darkroom workflows — scanning, proofing and "
                     "sorting film the analog way, with the speed of software.</p>")
        if DEV_INFO["website"]:
            url = escape(DEV_INFO["website"])
            parts.append(f'<p>Website: <a style="color:{ACCENT_HEX};" href="{url}">{url}</a></p>')
        if DEV_INFO["email"]:
            mail = escape(DEV_INFO["email"])
            parts.append(f'<p>Contact: <a style="color:{ACCENT_HEX};" href="mailto:{mail}">{mail}</a></p>')
        if not any(DEV_INFO.values()):
            parts.append("<p style='color:#888;'>Tip: edit the <code>DEV_INFO</code> block near the top of "
                         "proofmark.py to add your website, e-mail and bio here.</p>")
        parts.append(f"<p style='color:#888;'>Copyright notice from your profile: "
                     f"{escape(cfg.profile.get('copyright', '') or '—')}</p>")
        return "".join(parts)


class SettingsDialog(QDialog):
    """Application settings: appearance, contact sheet, files & sync, printing, updates."""

    def __init__(self, cfg: ConfigStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Settings")
        self.resize(600, 560)
        s = cfg.setting
        self._start_scale = int(s("ui_scale"))
        self._accent = str(s("accent"))
        tabs = QTabWidget()

        # ── Appearance ──
        ap = QWidget()
        fa = QFormLayout(ap)
        self.sys_font = QCheckBox("Use the system default font")
        self.sys_font.setChecked(not s("font_family"))
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(str(s("font_family")) or QApplication.font().family()))
        self.font_combo.setEnabled(not self.sys_font.isChecked())
        self.sys_font.toggled.connect(lambda on: self.font_combo.setEnabled(not on))
        self.font_size = self._spin(8, 22, int(s("font_size")), " pt")
        self.scale = self._spin(75, 200, self._start_scale, " %")
        self.scale.setToolTip("Scales the whole interface. Takes effect the next time ProofMark starts.")
        self.accent_btn = QPushButton()
        self.accent_btn.clicked.connect(self._pick_accent)
        self._paint_accent()
        self.menu_mode = QComboBox()
        self.menu_mode.addItem("Top bar (standard)", "top")
        self.menu_mode.addItem("Compact — ☰ Menu button in the toolbar", "compact")
        self.menu_mode.setCurrentIndex(max(0, self.menu_mode.findData(s("menu_mode"))))
        self.tb_area = QComboBox()
        self.tb_area.addItem("Top", "top")
        self.tb_area.addItem("Bottom", "bottom")
        self.tb_area.setCurrentIndex(max(0, self.tb_area.findData(s("toolbar_area"))))
        self.rebate = self._spin(70, 160, int(s("rebate_scale")), " %")
        fa.addRow(self.sys_font)
        fa.addRow("Font", self.font_combo)
        fa.addRow("Font size", self.font_size)
        fa.addRow("Interface scale", self.scale)
        fa.addRow("Accent colour", self.accent_btn)
        fa.addRow("Menu bar", self.menu_mode)
        fa.addRow("Toolbars", self.tb_area)
        fa.addRow("Film-edge text size", self.rebate)
        reset = QPushButton("Reset appearance to defaults")
        reset.clicked.connect(self._reset_appearance)
        fa.addRow(reset)
        tabs.addTab(ap, "Appearance")

        # ── Contact sheet ──
        cs = QWidget()
        fc = QFormLayout(cs)
        self.cols = self._spin(2, 14, int(s("default_columns")), "")
        self.hover_loupe = QCheckBox("Show the hover loupe by default")
        self.hover_loupe.setChecked(bool(s("hover_loupe")))
        fc.addRow("Default columns", self.cols)
        fc.addRow(self.hover_loupe)
        tabs.addTab(cs, "Contact Sheet")

        # ── Files & sync ──
        fs = QWidget()
        ff = QFormLayout(fs)
        self.autosave = self._spin(2, 60, int(s("autosave_secs")), " s")
        self.sync_enabled = QCheckBox("Write .pmdata / .xmp sidecars automatically")
        self.sync_enabled.setChecked(bool(s("sync_enabled")))
        self.rapidraw_exif = QCheckBox("Add artist, copyright and film / camera / lens to RapidRAW's .rrdata sidecar\n"
                                       "(applied when RapidRAW exports; your original files are never modified)")
        self.rapidraw_exif.setChecked(bool(s("rapidraw_exif")))
        ff.addRow("Session autosave every", self.autosave)
        ff.addRow(self.sync_enabled)
        ff.addRow(self.rapidraw_exif)
        tabs.addTab(fs, "Files && Sync")

        # ── Printing ──
        pr = QWidget()
        fp = QFormLayout(pr)
        self.print_direct = QCheckBox("One-click: print straight to the default printer (skip the print dialog)")
        self.print_direct.setChecked(bool(s("print_direct")))
        self.ink_saver = QCheckBox("Ink saver: white film base and dark text instead of the dark screen look")
        self.ink_saver.setChecked(bool(s("print_ink_saver")))
        self.print_header = QCheckBox("Print a header (roll, film, camera, lens, date) and page footer")
        self.print_header.setChecked(bool(s("print_header")))
        fp.addRow(self.print_direct)
        fp.addRow(self.ink_saver)
        fp.addRow(self.print_header)
        tabs.addTab(pr, "Printing")

        # ── Updates ──
        up = QWidget()
        fu = QFormLayout(up)
        self.auto_update = QCheckBox("Check for updates once a day")
        self.auto_update.setChecked(bool(s("auto_update_check")))
        self.repo = QLineEdit(str(s("update_repo")))
        self.repo.setPlaceholderText(UPDATE_REPO or "owner/repo  (GitHub repository that publishes releases)")
        fu.addRow(QLabel(f"Installed: {version_string(True)}    ·    install type: {install_kind()}"))
        fu.addRow(self.auto_update)
        fu.addRow("Update source", self.repo)
        tabs.addTab(up, "Updates")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    @staticmethod
    def _spin(lo: int, hi: int, value: int, suffix: str) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(value)
        sp.setSuffix(suffix)
        return sp

    def _paint_accent(self) -> None:
        c = QColor(self._accent)
        fg = "#000000" if c.lightness() > 128 else "#FFFFFF"
        self.accent_btn.setText(self._accent.upper())
        self.accent_btn.setStyleSheet(f"background:{self._accent}; color:{fg}; font-weight:bold;")

    def _pick_accent(self) -> None:
        c = QColorDialog.getColor(QColor(self._accent), self, "Accent colour")
        if c.isValid():
            self._accent = c.name().upper()
            self._paint_accent()

    def _reset_appearance(self) -> None:
        d = DEFAULT_SETTINGS
        self.sys_font.setChecked(True)
        self.font_size.setValue(d["font_size"])
        self.scale.setValue(d["ui_scale"])
        self._accent = d["accent"]
        self._paint_accent()
        self.menu_mode.setCurrentIndex(self.menu_mode.findData(d["menu_mode"]))
        self.tb_area.setCurrentIndex(self.tb_area.findData(d["toolbar_area"]))
        self.rebate.setValue(d["rebate_scale"])

    def restart_needed(self) -> bool:
        return self.scale.value() != self._start_scale

    def _save(self) -> None:
        values = {
            "font_family": "" if self.sys_font.isChecked() else self.font_combo.currentFont().family(),
            "font_size": self.font_size.value(), "ui_scale": self.scale.value(), "accent": self._accent,
            "menu_mode": self.menu_mode.currentData(), "toolbar_area": self.tb_area.currentData(),
            "rebate_scale": self.rebate.value(), "default_columns": self.cols.value(),
            "hover_loupe": self.hover_loupe.isChecked(), "autosave_secs": self.autosave.value(),
            "sync_enabled": self.sync_enabled.isChecked(), "rapidraw_exif": self.rapidraw_exif.isChecked(),
            "print_direct": self.print_direct.isChecked(), "print_ink_saver": self.ink_saver.isChecked(),
            "print_header": self.print_header.isChecked(), "auto_update_check": self.auto_update.isChecked(),
            "update_repo": self.repo.text().strip()}
        for key, value in values.items():
            self.cfg.set_setting(key, value)
        self.cfg.save()
        self.accept()


class UpdateDialog(QDialog):
    """Shows release notes for a newer version. Returns 2 from exec() when 'Update Now' is chosen."""

    def __init__(self, info: dict[str, Any], kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self.resize(580, 480)
        head = QLabel(f"<h3 style='color:{ACCENT_HEX};'>ProofMark v{escape(str(info['latest']))} is available</h3>"
                      f"<p>You have v{APP_VERSION} (build {BUILD_NUMBER}).</p>")
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "_No release notes provided._")
        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(notes, 1)
        hints = {
            "flatpak": f"Installed as a Flatpak. Update from GNOME Software, or run:  flatpak update {APP_ID}",
            "system": "Installed from a package. Update from GNOME Software, or run:  sudo dnf upgrade proofmark",
            "script": "Update Now downloads the new proofmark.py, verifies its checksum and keeps a .bak copy of this version.",
            "git": "Update Now runs  git pull --ff-only  in the application folder."}
        hint = QLabel(hints.get(kind, ""))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#999;")
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        if kind in ("git", "script"):
            upd = QPushButton("Update Now")
            upd.clicked.connect(lambda: self.done(2))
            row.addWidget(upd)
        page = QPushButton("Release Page")
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(str(info.get("page", "")))))
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        row.addWidget(page)
        row.addWidget(later)
        lay.addLayout(row)


class FavCombo(QComboBox):
    """Combo whose closed box and open list items both support a right-click menu."""

    itemContextRequested = Signal(str, QPoint)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._closed_menu)
        self.view().viewport().installEventFilter(self)

    def _closed_menu(self, pos: QPoint) -> None:
        data = self.currentData()
        if data:
            self.itemContextRequested.emit(str(data), self.mapToGlobal(pos))

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if (obj is self.view().viewport() and ev.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
                and ev.button() == Qt.RightButton):
            if ev.type() == QEvent.MouseButtonPress:
                idx = self.view().indexAt(ev.position().toPoint())
                if idx.isValid():
                    data = self.itemData(idx.row())
                    gpos = ev.globalPosition().toPoint()
                    self.hidePopup()
                    QTimer.singleShot(0, lambda: self.itemContextRequested.emit(str(data), gpos))
            return True
        return super().eventFilter(obj, ev)


# ════════════════════════════════════════════════════════════════════════════
# 3. BACKGROUND WORKERS
# ════════════════════════════════════════════════════════════════════════════

def _pil_to_qimage(img: Image.Image) -> QImage:
    img = img.convert("RGB")
    w, h = img.size
    return QImage(img.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()


def open_any_image(path: Path) -> Image.Image:
    """Decode a standard image or RAW file into an upright RGB PIL image."""
    if path.suffix.lower() in RAW_EXTS:
        if rawpy is None:
            raise RuntimeError("rawpy is not installed")
        with rawpy.imread(str(path)) as raw:
            img: Optional[Image.Image] = None
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    img.load()
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = Image.fromarray(thumb.data)
            except Exception:  # no / unsupported embedded preview
                img = None
            if img is None or max(img.size) < 1200:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
                img = Image.fromarray(rgb)
    else:
        img = Image.open(path)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            img.draft("RGB", (PREVIEW_W * 2, PREVIEW_H * 2))
        img.load()
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


class ImageLoaderThread(QThread):
    """Decodes thumbnails (450x320) and previews (1920x1440) off the UI thread."""

    frameReady = Signal(int, QImage, QImage)
    frameFailed = Signal(int, str)
    progress = Signal(int, int)

    def __init__(self, paths: list[Path], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self._abort = False

    def stop(self) -> None:
        self._abort = True

    def run(self) -> None:
        total = len(self.paths)
        for i, path in enumerate(self.paths):
            if self._abort:
                return
            try:
                img = open_any_image(path)
                preview = img.copy()
                preview.thumbnail((PREVIEW_W, PREVIEW_H), Image.LANCZOS)
                thumb = preview.copy()
                thumb.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
                self.frameReady.emit(i, _pil_to_qimage(thumb), _pil_to_qimage(preview))
            except Exception as exc:  # corrupt file must never kill the loader
                self.frameFailed.emit(i, f"{path.name}: {exc}")
            self.progress.emit(i + 1, total)


@dataclass
class SyncJob:
    path: Path
    marks: list[dict[str, Any]]
    rating: int
    artist: str
    copyright: str
    camera: str
    lens: str
    iso: int
    film: str


class RapidSyncWorker(QThread):
    """Writes .pmdata / .xmp sidecars and RapidRAW .rrdata metadata off the UI thread. Never touches originals."""

    status = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._queue: "queue.Queue[Optional[SyncJob]]" = queue.Queue()
        self._running = True
        self.rapidraw_exif = True

    def submit(self, job: SyncJob) -> None:
        self._queue.put(job)

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)

    def run(self) -> None:
        while self._running:
            job = self._queue.get()
            if job is None:
                break
            batch: dict[Path, SyncJob] = {job.path: job}  # coalesce duplicates
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._running = False
                    break
                batch[nxt.path] = nxt
            errors = 0
            for j in batch.values():
                errors += 0 if self._process(j) else 1
            msg = f"Synced {len(batch)} frame(s)"
            if errors:
                msg += f" — {errors} with warnings"
            self.status.emit(msg)

    def _process(self, job: SyncJob) -> bool:
        ok = True
        sidecar = pm_sidecar_path(job.path)
        owned: dict[str, str] = {}
        try:
            old = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
            owned = dict(old.get("rapidraw_exif") or {}) if isinstance(old, dict) else {}
        except (OSError, ValueError, TypeError):
            pass
        if self.rapidraw_exif:
            gear = [f"Film: {job.film} (ISO {job.iso})" if job.film else "",
                    f"Camera: {job.camera}" if job.camera else "", f"Lens: {job.lens}" if job.lens else ""]
            values = {"Artist": job.artist, "Copyright": job.copyright,
                      "UserComment": "  ·  ".join(g for g in gear if g)}
            try:
                result = merge_rapidraw_exif(job.path, values, owned)
            except OSError:
                result = None
            if result is None:
                ok = False
            else:
                owned = result
        try:
            atomic_write_json(sidecar, {
                "version": 1, "file": job.path.name, "rating": job.rating, "marks": job.marks,
                "film": job.film, "iso": job.iso, "camera": job.camera, "lens": job.lens,
                "artist": job.artist, "copyright": job.copyright, "rapidraw_exif": owned})
            xmp = job.path.with_suffix(".xmp")
            # Same file name RapidRAW and Lightroom use: only replace an .xmp that ProofMark wrote itself.
            if not xmp.exists() or "ns.proofmark.app" in xmp.read_text(encoding="utf-8", errors="replace"):
                xmp.write_text(self._xmp(job), encoding="utf-8")
        except OSError:
            ok = False
        return ok

    @staticmethod
    def _xmp(job: SyncJob) -> str:
        kinds = [m["kind"] for m in job.marks]
        attrs = (f' xmp:Rating="{job.rating}" tiff:Model={quoteattr(job.camera)}'
                 f' aux:Lens={quoteattr(job.lens)} proofmark:Film={quoteattr(job.film)}')
        artist = f"<dc:creator><rdf:Seq><rdf:li>{escape(job.artist)}</rdf:li></rdf:Seq></dc:creator>" if job.artist else ""
        rights = (f'<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">{escape(job.copyright)}</rdf:li></rdf:Alt></dc:rights>'
                  if job.copyright else "")
        marks = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in kinds)
        return (
            '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            '   xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
            '   xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
            '   xmlns:tiff="http://ns.adobe.com/tiff/1.0/"\n'
            '   xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
            '   xmlns:aux="http://ns.adobe.com/exif/1.0/aux/"\n'
            '   xmlns:proofmark="http://ns.proofmark.app/1.0/"' + attrs + '>\n'
            f'   {artist}\n   {rights}\n'
            f'   <exif:ISOSpeedRatings><rdf:Seq><rdf:li>{job.iso}</rdf:li></rdf:Seq></exif:ISOSpeedRatings>\n'
            f'   <proofmark:Marks><rdf:Bag>{marks}</rdf:Bag></proofmark:Marks>\n'
            '  </rdf:Description>\n </rdf:RDF>\n</x:xmpmeta>\n<?xpacket end="w"?>\n')


# ════════════════════════════════════════════════════════════════════════════
# 4. CONTACT SHEET CANVAS
# ════════════════════════════════════════════════════════════════════════════

class UpdateCheckWorker(QThread):
    """Asks the GitHub releases API for the latest release (runs off the UI thread)."""

    finishedCheck = Signal(dict)

    def __init__(self, repo: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.repo = repo

    def run(self) -> None:
        import urllib.request
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": f"ProofMark/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out: dict[str, Any] = {
                "ok": True, "latest": str(data.get("tag_name", "")).lstrip("vV"), "notes": data.get("body") or "",
                "page": data.get("html_url", ""),
                "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}}
        except Exception as exc:  # network / JSON problems must never crash the app
            out = {"ok": False, "error": str(exc)}
        self.finishedCheck.emit(out)


class UpdateInstallWorker(QThread):
    """git checkout: `git pull --ff-only`.  Loose script: download proofmark.py, verify, swap, keep a backup."""

    finishedInstall = Signal(bool, str)

    def __init__(self, kind: str, latest: str, assets: dict[str, str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.latest = latest
        self.assets = assets

    def run(self) -> None:
        try:
            if self.kind == "git":
                res = subprocess.run(["git", "-C", str(SCRIPT_DIR), "pull", "--ff-only"],
                                     capture_output=True, text=True, timeout=120, check=False)
                self.finishedInstall.emit(res.returncode == 0, (res.stdout or res.stderr).strip() or "git pull finished.")
            else:
                self.finishedInstall.emit(True, self._install_script())
        except Exception as exc:
            self.finishedInstall.emit(False, str(exc))

    def _install_script(self) -> str:
        import hashlib
        import urllib.request

        def fetch(url: str) -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": f"ProofMark/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()

        url, sha_url = self.assets.get("proofmark.py"), self.assets.get("proofmark.py.sha256")
        if not url or not sha_url:
            raise RuntimeError("This release has no proofmark.py + proofmark.py.sha256 assets, so it can't be installed automatically.")
        data = fetch(url)
        want = fetch(sha_url).decode("utf-8").split()[0].strip().lower()
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError("Checksum mismatch: the download was corrupted or altered. Update aborted.")
        src = data.decode("utf-8")
        m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
        if not m or parse_version(m.group(1)) != parse_version(self.latest):
            raise RuntimeError("The downloaded file's version doesn't match the release. Update aborted.")
        compile(src, "proofmark.py", "exec")  # syntax check before we replace anything
        target = Path(__file__).resolve()
        backup = target.with_name(f"{target.name}.bak-{APP_VERSION}")
        shutil.copy2(target, backup)
        tmp = target.with_name(target.name + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return f"Updated to v{self.latest}. The previous version was saved as {backup.name}."


# ── Clean (unmarked) contact sheet rendering: shared by the screen and the printer ──

def frame_slot_rect(cell: QRectF, rebate_h: float) -> QRectF:
    m = max(3.0, cell.width() * 0.022)
    return QRectF(cell.x() + m, cell.y() + m, cell.width() - 2 * m, cell.height() - rebate_h - m)


def paint_rebate_strip(p: QPainter, cell: QRectF, rebate_h: float, number: str, meta: dict[str, Any],
                       ink_saver: bool = False) -> None:
    """Simulated emulsion rebate: frame number, edge print + DX code, barcode."""
    strip = QRectF(cell.x(), cell.bottom() - rebate_h, cell.width(), rebate_h)
    main = QColor("#111111") if ink_saver else C_AMBER
    dim = QColor(0, 0, 0, 150) if ink_saver else C_AMBER_DIM
    p.fillRect(strip, QColor("#F2F2F2") if ink_saver else QColor("#020202"))
    font = QFont("DejaVu Sans Mono")
    font.setPixelSize(max(6, int(rebate_h * 0.52 * REBATE_SCALE)))
    font.setBold(True)
    p.setFont(font)
    pad = max(rebate_h * 0.17, cell.width() * 0.02)
    num_w = QFontMetrics(font, p.device()).horizontalAdvance(number)
    p.setPen(main)
    p.drawText(QRectF(strip.x() + pad, strip.y(), num_w + 4, strip.height()), Qt.AlignVCenter | Qt.AlignLeft, number)
    bar_w = min(strip.width() * 0.22, rebate_h * 3.5)
    bar = QRectF(strip.right() - pad - bar_w, strip.y() + strip.height() * 0.22, bar_w, strip.height() * 0.56)
    draw_barcode(p, bar, meta.get("barcode", ""), dim)
    edge_x = strip.x() + pad * 2 + num_w
    edge_w = max(0.0, bar.x() - pad - edge_x)
    font.setBold(False)
    p.setFont(font)
    text = QFontMetrics(font, p.device()).elidedText(
        f"▶ {meta.get('edge', '')}  {meta.get('dx', '')}  ▶", Qt.ElideRight, int(edge_w))
    p.setPen(dim)
    p.drawText(QRectF(edge_x, strip.y(), edge_w, strip.height()), Qt.AlignVCenter | Qt.AlignLeft, text)


def paint_frame_clean(p: QPainter, cell: QRectF, rebate_h: float, idx: int, meta: dict[str, Any],
                      pm: Optional[QPixmap], ink_saver: bool = False) -> None:
    """One frame exactly as on screen, without marks, selection or hover."""
    p.save()
    p.setClipRect(cell)
    p.fillRect(cell, QColor("#FFFFFF") if ink_saver else C_FILM)
    slot = frame_slot_rect(cell, rebate_h)
    p.fillRect(slot, QColor("#E6E6E6") if ink_saver else QColor("#0D0D0D"))
    if pm is not None and pm.height() > 0:
        p.drawPixmap(fit_rect(slot, pm.width() / pm.height()), pm, QRectF(pm.rect()))
    paint_rebate_strip(p, cell, rebate_h, f"{idx + 1}A", meta, ink_saver)
    p.setPen(QPen(QColor("#000000") if ink_saver else QColor("#1C1C1C"), max(1.0, cell.width() * 0.004)))
    p.setBrush(Qt.NoBrush)
    p.drawRect(cell)
    p.restore()


def render_contact_sheet(printer: Any, roll: Roll, columns: int, meta: dict[str, Any],
                         pixmap_for: Callable[[Frame], Optional[QPixmap]],
                         ink_saver: bool = False, header: bool = True) -> int:
    """Paginate the roll onto `printer` (paper or PDF) using the on-screen column count. Returns page count."""
    import datetime
    p = QPainter()
    if not p.begin(printer):
        return 0
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    # The painter's origin is already the top-left of the printable area (inside the margins).
    paint = printer.pageLayout().paintRectPixels(printer.resolution())
    rect = QRectF(0, 0, paint.width(), paint.height())
    cols = max(1, columns)
    n = len(roll.frames)
    head_h = rect.height() * 0.045 if header else 0.0
    foot_h = rect.height() * 0.03
    avail_h = rect.height() - head_h - foot_h
    ratio = THUMB_H / THUMB_W + 0.085  # cell height / cell width, same proportions as the screen
    cell_w = int(rect.width() // cols)
    if cell_w * ratio > avail_h:
        cell_w = int(avail_h / ratio)
    cell_h = int(cell_w * ratio)
    rebate_h = cell_w * 0.085
    per_page = max(1, int(avail_h // cell_h)) * cols
    pages = max(1, math.ceil(n / per_page))
    x0 = rect.x() + (rect.width() - cell_w * cols) / 2
    font = QFont("DejaVu Sans")
    font.setPixelSize(max(6, int(rect.height() * 0.014)))
    p.setFont(font)
    title = f"{roll.folder.name}   ·   {roll.film}   ·   {roll.camera}   ·   {roll.lens}"
    for page in range(pages):
        if page:
            printer.newPage()
        p.setPen(QColor("#000000"))
        if header:
            p.drawText(QRectF(rect.x(), rect.y(), rect.width() * 0.75, head_h), Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(font, p.device()).elidedText(title, Qt.ElideRight, int(rect.width() * 0.75)))
            p.drawText(QRectF(rect.x(), rect.y(), rect.width(), head_h), Qt.AlignVCenter | Qt.AlignRight,
                       datetime.date.today().isoformat())
            p.drawText(QRectF(rect.x(), rect.bottom() - foot_h, rect.width(), foot_h), Qt.AlignVCenter | Qt.AlignLeft,
                       f"ProofMark v{APP_VERSION}  ·  {n} frames")
            p.drawText(QRectF(rect.x(), rect.bottom() - foot_h, rect.width(), foot_h), Qt.AlignVCenter | Qt.AlignRight,
                       f"Page {page + 1} of {pages}")
        first = page * per_page
        rows_here = math.ceil(min(per_page, n - first) / cols) if n else 0
        for k in range(rows_here * cols):
            i = first + k
            r, c = divmod(k, cols)
            cell = QRectF(x0 + c * cell_w, rect.y() + head_h + r * cell_h, cell_w, cell_h)
            if i >= n:
                p.fillRect(cell, QColor("#FFFFFF") if ink_saver else C_FILM)
            else:
                paint_frame_clean(p, cell, rebate_h, i, meta, pixmap_for(roll.frames[i]), ink_saver)
    p.end()
    return pages


class ContactSheetCanvas(QAbstractScrollArea):
    """
    Edge-to-edge contact sheet with rebate bars, grease marks, loupe and 2-up compare.

    Interaction map
      hover            -> 50% loupe (opposite side of the cursor)
      click (Inspect)  -> loupe scales to 85%;  right-click / Space also lock it
      Ctrl+wheel       -> loupe magnification 0.7x .. 4.5x (marks never hidden)
      right-drag       -> pan a locked, magnified loupe
      double-click     -> pop topmost mark (frame first, then across frames)
      Ctrl+Z           -> pop most recent mark anywhere
      C                -> 2-up compare (A locked, B follows hover); C / Esc exits
    """

    toolChanged = Signal(str)
    marksChanged = Signal(int)
    compareChanged = Signal(bool)
    status = Signal(str)
    brushChanged = Signal(float, str)

    def __init__(self, roll: Roll, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.roll = roll
        self.frames = roll.frames
        self.tool = T_INSPECT
        self.columns = 6
        self.loupe_enabled = True
        self.brush_size = 1.0
        self.brush_color = DEFAULT_BRUSH_COLOR
        self.cell_w = 100
        self.cell_h = 100
        self.x0 = 0
        self.rebate_h = 20.0

        self.hover_index = -1
        self.hover_pos = QPointF(-1, -1)
        self.selected = -1
        self.loupe_locked = False
        self.locked_index = -1
        self.zoom = 1.0
        self.focus = QPointF(0.5, 0.5)
        self.compare = False
        self.compare_a = -1

        self._drag: Optional[dict[str, Any]] = None
        self._edit: Optional[dict[str, Any]] = None
        self.sel: Optional[tuple[int, Mark]] = None  # selected mark (handles shown)
        self._pan_last: Optional[QPointF] = None
        self._last_click_added: Optional[tuple[int, Mark]] = None
        self._click_locked = False
        self.history: list[tuple[int, Mark]] = []
        self._pix_cache: "OrderedDict[Path, QPixmap]" = OrderedDict()

        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())

    # ── public API ─────────────────────────────────────────────────────────
    def set_tool(self, tool: str) -> None:
        if tool == self.tool:
            return
        self._drag = None
        self._edit = None
        if tool == T_INSPECT:
            self.sel = None
        self.tool = tool
        self.toolChanged.emit(tool)
        hint = next((h for t, _l, _k, h in TOOL_LIST if t == tool), "")
        self.status.emit(hint)
        self.viewport().update()

    def set_columns(self, cols: int) -> None:
        self.columns = max(1, cols)
        self._update_scrollbar()
        self.viewport().update()

    def set_brush_size(self, size: float) -> None:
        self.brush_size = max(0.25, min(4.0, size))
        self.brushChanged.emit(self.brush_size, self.brush_color)
        self.viewport().update()

    def set_brush_color(self, color: str) -> None:
        self.brush_color = color
        self.brushChanged.emit(self.brush_size, self.brush_color)
        self.viewport().update()

    def _ghost(self, p: QPainter, rect: QRectF, width: float) -> None:
        """Translucent preview of the mark that a plain click would place."""
        if self._drag is not None or self._edit is not None:
            return
        n = norm_in_rect(rect, self.hover_pos)
        if self.tool in POINT_TOOLS:
            mk = Mark(self.tool, [n], 1, self.brush_size, self.brush_color)
        elif self.tool in BOX_TOOLS:
            mk = Mark(self.tool, default_box_pts(rect, n, self.brush_size), 1, self.brush_size, self.brush_color)
        else:
            return
        p.save()
        p.setOpacity(0.45)
        draw_mark(p, mk, rect, width)
        p.restore()

    def _sel_valid(self) -> bool:
        return self.sel is not None and any(m is self.sel[1] for m in self.frames[self.sel[0]].marks)

    def _paint_selection(self, p: QPainter, idx: int, rect: QRectF) -> None:
        if self.compare or not self._sel_valid() or self.sel is None or self.sel[0] != idx:
            return
        mk = self.sel[1]
        p.save()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_AMBER, 1.0, Qt.DashLine))
        p.drawRect(mark_bounds_px(mk, rect))
        half = 4.0
        p.setPen(QPen(QColor("#000000"), 1.0))
        p.setBrush(C_AMBER)
        for pt in handle_points(mk, rect).values():
            p.drawRect(QRectF(pt.x() - half, pt.y() - half, half * 2, half * 2))
        p.restore()

    def _begin_edit(self, idx: int, mk: Mark, mode: str, pos: QPointF, rect: QRectF) -> None:
        self._edit = {"idx": idx, "mark": mk, "mode": mode, "start": QPointF(pos), "moved": False,
                      "pts": [list(p) for p in mk.pts], "size": mk.size, "bounds": mark_bounds_px(mk, rect)}

    def _update_cursor(self, pos: QPointF) -> None:
        cur = Qt.ArrowCursor
        target = None if self.compare else self._target_at(pos)
        if target and self.tool != T_INSPECT:
            idx, rect = target
            cur = Qt.CrossCursor if self.tool != T_ADJUST else Qt.ArrowCursor
            if self._sel_valid() and self.sel is not None and self.sel[0] == idx:
                h = hit_handle(self.sel[1], rect, pos)
                if h:
                    cur = HANDLE_CURSORS.get(h, Qt.SizeAllCursor)
                elif self.tool == T_ADJUST and mark_bounds_px(self.sel[1], rect).adjusted(-6, -6, 6, 6).contains(pos):
                    cur = Qt.SizeAllCursor
        self.viewport().setCursor(cur)

    def set_loupe_enabled(self, on: bool) -> None:
        self.loupe_enabled = on
        self.viewport().update()

    def on_frame_ready(self, idx: int, thumb: QImage, preview: QImage) -> None:
        if 0 <= idx < len(self.frames):
            fr = self.frames[idx]
            fr.thumb = QPixmap.fromImage(thumb)
            fr.preview = preview
            fr.aspect = max(0.05, preview.width() / max(1, preview.height()))
            self.viewport().update()

    def toggle_compare(self, on: Optional[bool] = None) -> None:
        want = (not self.compare) if on is None else on
        if want == self.compare:
            return
        if want:
            start = self.hover_index if self.hover_index >= 0 else (
                self.locked_index if self.loupe_locked else self.selected)
            if start < 0:
                self.status.emit("Compare: hover or click a frame first to set Image [A].")
                self.compareChanged.emit(False)
                return
            self.compare_a = start
            self.loupe_locked = False
        self.compare = want
        self.compareChanged.emit(want)
        self._update_scrollbar()
        self.viewport().update()

    # ── geometry ───────────────────────────────────────────────────────────
    @property
    def grid_top(self) -> int:
        return int(self.viewport().height() * 0.6) if self.compare else 0

    def _layout(self) -> None:
        vw = self.viewport().width()
        cols = max(1, self.columns)
        self.cell_w = max(40, vw // cols)
        self.x0 = max(0, (vw - self.cell_w * cols) // 2)  # centred: no left-side bunching
        self.rebate_h = max(16.0, self.cell_w * 0.085)
        self.cell_h = int(round(self.cell_w * THUMB_H / THUMB_W + self.rebate_h))  # rows flush

    def _update_scrollbar(self) -> None:
        self._layout()
        rows = math.ceil(len(self.frames) / max(1, self.columns)) if self.frames else 0
        avail = max(1, self.viewport().height() - self.grid_top)
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, rows * self.cell_h - avail))
        sb.setPageStep(avail)
        sb.setSingleStep(max(16, self.cell_h // 3))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_scrollbar()

    def _cell_rect(self, i: int) -> QRectF:
        row, col = divmod(i, max(1, self.columns))
        return QRectF(self.x0 + col * self.cell_w,
                      self.grid_top + row * self.cell_h - self.verticalScrollBar().value(),
                      self.cell_w, self.cell_h)

    def _slot_rect(self, cell: QRectF) -> QRectF:
        return frame_slot_rect(cell, self.rebate_h)

    def _image_rect(self, i: int) -> QRectF:
        return fit_rect(self._slot_rect(self._cell_rect(i)), self.frames[i].aspect)

    def cell_at(self, pos: QPointF) -> int:
        self._layout()
        gt = self.grid_top
        if pos.y() < gt or not self.frames:
            return -1
        yy = pos.y() - gt + self.verticalScrollBar().value()
        row = int(yy // self.cell_h)
        col = int((pos.x() - self.x0) // self.cell_w)
        if yy < 0 or col < 0 or col >= self.columns:
            return -1
        idx = row * self.columns + col
        return idx if 0 <= idx < len(self.frames) else -1

    def loupe_geometry(self, idx: int, locked: bool) -> tuple[QRectF, QRectF]:
        vw, vh = self.viewport().width(), self.viewport().height()
        frac = 0.85 if locked else 0.5
        bw, bh = vw * frac, vh * frac
        if locked:
            box = QRectF((vw - bw) / 2, (vh - bh) / 2, bw, bh)
        else:
            on_left = self.hover_pos.x() < vw / 2
            box = QRectF(vw - bw - 16 if on_left else 16, (vh - bh) / 2, bw, bh)
        base = fit_rect(box, self.frames[idx].aspect)
        w, h = base.width() * self.zoom, base.height() * self.zoom
        fx, fy = (self.focus.x(), self.focus.y()) if self.zoom > 1.0 else (0.5, 0.5)
        cx = box.center().x() + (0.5 - fx) * w
        cy = box.center().y() + (0.5 - fy) * h
        cx = self._clamp_axis(cx, w, box.left(), box.right())
        cy = self._clamp_axis(cy, h, box.top(), box.bottom())
        return box, QRectF(cx - w / 2, cy - h / 2, w, h)

    @staticmethod
    def _clamp_axis(c: float, size: float, lo: float, hi: float) -> float:
        if size <= hi - lo:
            return (lo + hi) / 2
        return max(hi - size / 2, min(lo + size / 2, c))

    def _rect_for(self, idx: int) -> QRectF:
        if self.loupe_locked and idx == self.locked_index:
            return self.loupe_geometry(idx, True)[1]
        return self._image_rect(idx)

    def _target_at(self, pos: QPointF) -> Optional[tuple[int, QRectF]]:
        if self.loupe_locked:
            box, img = self.loupe_geometry(self.locked_index, True)
            return (self.locked_index, img) if box.contains(pos) else None
        i = self.cell_at(pos)
        return (i, self._image_rect(i)) if i >= 0 else None

    def preview_pixmap(self, fr: Frame) -> Optional[QPixmap]:
        if fr.preview is None:
            return fr.thumb
        pm = self._pix_cache.get(fr.path)
        if pm is None:
            pm = QPixmap.fromImage(fr.preview)
            self._pix_cache[fr.path] = pm
            while len(self._pix_cache) > 8:
                self._pix_cache.popitem(last=False)
        else:
            self._pix_cache.move_to_end(fr.path)
        return pm

    # ── mark bookkeeping ───────────────────────────────────────────────────
    def _add_mark(self, idx: int, mark: Mark) -> None:
        self.frames[idx].marks.append(mark)
        self.history.append((idx, mark))
        self.marksChanged.emit(idx)
        self.viewport().update()

    def _remove_mark(self, idx: int, mark: Mark) -> None:
        marks = self.frames[idx].marks
        for j, m in enumerate(marks):
            if m is mark:
                del marks[j]
                break
        self.history = [(i, m) for i, m in self.history if m is not mark]
        if self.sel is not None and self.sel[1] is mark:
            self.sel = None
        self.marksChanged.emit(idx)
        self.viewport().update()

    def pop_topmost(self, idx: int) -> None:
        if 0 <= idx < len(self.frames) and self.frames[idx].marks:
            self._remove_mark(idx, self.frames[idx].marks[-1])
            return
        while self.history:
            fi, m = self.history[-1]
            if any(x is m for x in self.frames[fi].marks):
                self._remove_mark(fi, m)
                return
            self.history.pop()
        self.status.emit("Nothing to undo.")

    def _marks_with_drag(self, idx: int) -> list[Mark]:
        marks = list(self.frames[idx].marks)
        if self._drag and self._drag["idx"] == idx:
            marks.append(self._drag["mark"])
        return marks

    # ── painting ───────────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:  # noqa: N802
        vp = self.viewport()
        p = QPainter(vp)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        p.fillRect(vp.rect(), C_BASE)
        self._layout()
        vw, vh = vp.width(), vp.height()
        if not self.frames:
            p.setPen(C_AMBER)
            p.drawText(vp.rect(), Qt.AlignCenter, "No supported images in this roll folder.")
            p.end()
            return
        gt = self.grid_top
        p.save()
        p.setClipRect(QRect(0, gt, vw, vh - gt))
        scroll = self.verticalScrollBar().value()
        first = max(0, scroll // self.cell_h)
        last = (scroll + vh - gt) // self.cell_h + 1
        for row in range(first, last + 1):
            for col in range(self.columns):
                i = row * self.columns + col
                if i >= len(self.frames):
                    cell = QRectF(self.x0 + col * self.cell_w, gt + row * self.cell_h - scroll,
                                  self.cell_w, self.cell_h)
                    p.fillRect(cell, C_FILM)
                    continue
                self._paint_cell(p, i)
        p.restore()
        if self.compare:
            self._paint_compare(p, vw)
        elif self.loupe_locked and 0 <= self.locked_index < len(self.frames):
            p.fillRect(vp.rect(), QColor(0, 0, 0, 175))
            self._paint_loupe(p, self.locked_index, True)
        elif (self.loupe_enabled and self.hover_index >= 0 and self._drag is None
              and self._edit is None and self._pan_last is None):
            self._paint_loupe(p, self.hover_index, False)
        p.end()

    def _paint_cell(self, p: QPainter, i: int) -> None:
        fr = self.frames[i]
        cell = self._cell_rect(i)
        slot = self._slot_rect(cell)
        img = fit_rect(slot, fr.aspect)
        p.save()
        p.setClipRect(cell)
        p.fillRect(cell, C_FILM)
        p.fillRect(slot, QColor("#0D0D0D"))
        if fr.thumb is not None:
            p.setOpacity(0.5 if fr.rejected else 1.0)
            p.drawPixmap(img, fr.thumb, QRectF(fr.thumb.rect()))
            p.setOpacity(1.0)
        else:
            p.setPen(QColor("#555555"))
            p.drawText(slot, Qt.AlignCenter, "can't open" if fr.error else "loading…")
        width = max(1.5, img.width() * 0.011)
        for mk in self._marks_with_drag(i):
            draw_mark(p, mk, img, width)
        if i == self.hover_index and not self.loupe_locked and not self.compare:
            self._ghost(p, img, width)
        self._paint_selection(p, i, img)
        self._paint_rebate(p, i, cell)
        p.setPen(QPen(QColor("#1C1C1C"), 1))
        p.drawRect(cell.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.compare and i == self.compare_a:
            p.setPen(QPen(C_AMBER, 2))
            p.drawRect(cell.adjusted(1, 1, -1, -1))
        elif i == self.hover_index and not self.loupe_locked:
            p.setPen(QPen(C_AMBER_DIM, 1.5))
            p.drawRect(cell.adjusted(1, 1, -1, -1))
        p.restore()

    def _paint_rebate(self, p: QPainter, i: int, cell: QRectF) -> None:
        paint_rebate_strip(p, cell, self.rebate_h, f"{i + 1}A", self.roll_film_meta())

    def roll_film_meta(self) -> dict[str, Any]:
        return self._film_lookup(self.roll.film)

    _film_lookup: Callable[[str], dict[str, Any]] = staticmethod(lambda name: {"edge": name.upper(), "dx": "", "barcode": ""})  # type: ignore

    def _paint_loupe(self, p: QPainter, idx: int, locked: bool) -> None:
        fr = self.frames[idx]
        box, img = self.loupe_geometry(idx, locked)
        p.save()
        p.fillRect(box, QColor("#000000"))
        p.setClipRect(box)
        pm = self.preview_pixmap(fr)
        if pm is not None:
            p.drawPixmap(img, pm, QRectF(pm.rect()))
        else:
            p.setPen(QColor("#666666"))
            p.drawText(box, Qt.AlignCenter, f"Can't open this file\n{fr.error}" if fr.error else "loading…")
        width = max(1.8, img.width() * 0.008)
        for mk in self._marks_with_drag(idx):
            draw_mark(p, mk, img, width)
        if locked and box.contains(self.hover_pos):
            self._ghost(p, img, width)
        self._paint_selection(p, idx, img)
        p.setClipping(False)
        label = f"{idx + 1}A  {fr.name}   {self.zoom:.2f}×"
        p.setFont(QFont("DejaVu Sans Mono", 9))
        p.fillRect(QRectF(box.x(), box.y(), QFontMetrics(p.font()).horizontalAdvance(label) + 14, 20),
                   QColor(0, 0, 0, 200))
        p.setPen(C_AMBER)
        p.drawText(QRectF(box.x() + 7, box.y(), box.width(), 20), Qt.AlignVCenter | Qt.AlignLeft, label)
        p.setPen(QPen(C_AMBER, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(box)
        p.restore()

    def _paint_compare(self, p: QPainter, vw: int) -> None:
        h = self.grid_top
        p.fillRect(QRectF(0, 0, vw, h), QColor("#050505"))
        halves = [QRectF(6, 6, vw / 2 - 9, h - 12), QRectF(vw / 2 + 3, 6, vw / 2 - 9, h - 12)]
        b_idx = self.hover_index if 0 <= self.hover_index < len(self.frames) else -1
        for tag, idx, box in (("A", self.compare_a, halves[0]), ("B", b_idx, halves[1])):
            p.save()
            p.setClipRect(box)
            p.fillRect(box, QColor("#000000"))
            if idx >= 0:
                fr = self.frames[idx]
                pm = self.preview_pixmap(fr)
                img = fit_rect(box.adjusted(4, 22, -4, -4), fr.aspect)
                if pm is not None:
                    p.drawPixmap(img, pm, QRectF(pm.rect()))
                for mk in fr.marks:
                    draw_mark(p, mk, img, max(1.8, img.width() * 0.008))
                label = f"[{tag}]  {idx + 1}A  {fr.name}"
            else:
                p.setPen(QColor("#777777"))
                p.drawText(box, Qt.AlignCenter, "Hover a frame in the grid below for [B]")
                label = f"[{tag}]"
            p.setFont(QFont("DejaVu Sans Mono", 10, QFont.Bold))
            p.setPen(C_AMBER)
            p.drawText(QRectF(box.x() + 8, box.y() + 2, box.width(), 20), Qt.AlignVCenter | Qt.AlignLeft, label)
            p.restore()
            p.setPen(QPen(C_AMBER if tag == "A" else C_AMBER_DIM, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRect(box)

    # ── input ──────────────────────────────────────────────────────────────
    def viewportEvent(self, event) -> bool:  # noqa: N802
        t = event.type()
        if t == QEvent.Enter:
            self.setFocus()
        elif t == QEvent.Leave:
            if not self.loupe_locked:
                self.hover_index = -1
                self.viewport().update()
        return super().viewportEvent(event)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.setFocus()
        pos = e.position()
        self._click_locked = False
        if e.button() == Qt.RightButton:
            if self.loupe_locked:
                if self._target_at(pos) is None:
                    self.loupe_locked = False
                else:
                    self._pan_last = QPointF(pos)
            else:
                i = self.cell_at(pos)
                if i >= 0 and not self.compare:
                    self._lock_loupe(i, pos)
            self.viewport().update()
            return
        if e.button() != Qt.LeftButton:
            return
        if self.compare:
            i = self.cell_at(pos)
            if i >= 0:
                self.compare_a = i
                self.selected = i
                self.viewport().update()
            return
        target = self._target_at(pos)
        if target is None:
            if self.loupe_locked:
                self.loupe_locked = False
                self.viewport().update()
            return
        idx, rect = target
        self.selected = idx
        self._last_click_added = None
        # 1) handles of the selected mark win in every tool except Inspect
        if self.tool != T_INSPECT and self._sel_valid() and self.sel is not None and self.sel[0] == idx:
            sel_mark = self.sel[1]
            handle = hit_handle(sel_mark, rect, pos)
            if handle:
                self._begin_edit(idx, sel_mark, handle, pos, rect)
                self.viewport().update()
                return
            if self.tool == T_ADJUST and mark_bounds_px(sel_mark, rect).adjusted(-6, -6, 6, 6).contains(pos):
                self._begin_edit(idx, sel_mark, "move", pos, rect)
                self.viewport().update()
                return
        # 2) Adjust tool: pick the topmost mark under the cursor and start moving it
        if self.tool == T_ADJUST:
            picked = next((m for m in reversed(self.frames[idx].marks) if mark_hit(m, rect, pos)), None)
            if picked is None:
                self.sel = None
            else:
                self.sel = (idx, picked)
                self._begin_edit(idx, picked, "move", pos, rect)
            self.viewport().update()
            return
        if self.tool in POINT_TOOLS or self.tool in DRAG_TOOLS:
            # Point marks: click places at the brush size, click-drag outward sizes the mark live.
            # Box / arrow / line: drag draws it; a plain click on a box tool drops a default box.
            self.sel = None
            is_point = self.tool in POINT_TOOLS
            n = norm_in_rect(rect, pos)
            pts = [n] if (is_point or self.tool == T_LINE) else [n, list(n)]
            self._drag = {"idx": idx, "point": is_point, "start": QPointF(pos),
                          "mark": Mark(self.tool, pts, random.getrandbits(31), self.brush_size, self.brush_color)}
        else:  # inspect
            self._last_click_added = None
            if not self.loupe_locked and self.loupe_enabled:
                self._lock_loupe(idx, pos)
                self._click_locked = True
        self.viewport().update()

    def _lock_loupe(self, idx: int, pos: QPointF) -> None:
        self.loupe_locked = True
        self.locked_index = idx
        self.selected = idx
        n = norm_in_rect(self._image_rect(idx), pos)
        self.focus = QPointF(n[0], n[1])

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        pos = e.position()
        self.hover_pos = QPointF(pos)
        if self._pan_last is not None and self.loupe_locked:
            _box, img = self.loupe_geometry(self.locked_index, True)
            d = pos - self._pan_last
            self.focus = QPointF(min(1.0, max(0.0, self.focus.x() - d.x() / max(1.0, img.width()))),
                                 min(1.0, max(0.0, self.focus.y() - d.y() / max(1.0, img.height()))))
            self._pan_last = QPointF(pos)
        elif self._edit is not None:
            ed = self._edit
            mk: Mark = ed["mark"]
            rect = self._rect_for(ed["idx"])
            if ed["mode"] == "move":
                move_mark(mk, ed["pts"], (pos.x() - ed["start"].x()) / max(1e-6, rect.width()),
                          (pos.y() - ed["start"].y()) / max(1e-6, rect.height()))
            else:
                resize_mark(mk, rect, ed["mode"], pos, ed)
            ed["moved"] = True
        elif self._drag is not None:
            rect = self._rect_for(self._drag["idx"])
            n = norm_in_rect(rect, pos)
            mark: Mark = self._drag["mark"]
            if self._drag["point"]:
                cx = rect.x() + mark.pts[0][0] * rect.width()
                cy = rect.y() + mark.pts[0][1] * rect.height()
                dist = math.hypot(pos.x() - cx, pos.y() - cy)
                if dist > 4.0:
                    unit = 0.075 * min(rect.width(), rect.height()) * (1.15 if mark.kind == T_STAR else 1.0)
                    mark.size = max(0.25, min(8.0, dist / max(1.0, unit)))
                else:
                    mark.size = self.brush_size
            elif mark.kind == T_LINE:
                last = mark.pts[-1]
                if math.hypot(n[0] - last[0], n[1] - last[1]) > 0.002:
                    mark.pts.append(n)
            else:
                mark.pts[1] = n
        elif not self.loupe_locked:
            i = self.cell_at(pos)
            self.hover_index = i
            if i >= 0:
                n = norm_in_rect(self._image_rect(i), pos)
                self.focus = QPointF(n[0], n[1])
        if self._drag is None and self._edit is None and self._pan_last is None:
            self._update_cursor(pos)
        self.viewport().update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.RightButton:
            self._pan_last = None
        if e.button() == Qt.LeftButton and self._edit is not None:
            ed = self._edit
            self._edit = None
            if ed["moved"]:
                self.marksChanged.emit(ed["idx"])
        if e.button() == Qt.LeftButton and self._drag is not None:
            mark: Mark = self._drag["mark"]
            idx: int = self._drag["idx"]
            is_point: bool = self._drag["point"]
            self._drag = None
            rect = self._rect_for(idx)
            if is_point:
                valid = True
            elif mark.kind == T_LINE:
                valid = len(mark.pts) >= 3
            else:
                a, b = mark.pts[0], mark.pts[1]
                diag = math.hypot((b[0] - a[0]) * rect.width(), (b[1] - a[1]) * rect.height())
                if mark.kind in BOX_TOOLS and diag < 8.0:
                    mark.pts = default_box_pts(rect, a, self.brush_size)  # plain click: default box
                    valid = True
                else:
                    valid = diag > 6.0
            if valid:
                self._add_mark(idx, mark)
                self._last_click_added = (idx, mark)
                if mark.kind not in POINT_TOOLS:  # boxes / arrows / lines stay selected so they can be tweaked
                    self.sel = (idx, mark)
                    self.status.emit("Drag the handles to resize  •  E = Adjust tool to move  •  Del removes  •  Esc deselects")
        self.viewport().update()

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.LeftButton or self.compare:
            return
        pos = e.position()
        target = self._target_at(pos)
        idx = target[0] if target else self.cell_at(pos)
        # The first click of the double-click already placed a mark / locked the loupe: undo those.
        if self._last_click_added is not None:
            fi, mk = self._last_click_added
            if any(m is mk for m in self.frames[fi].marks):
                self._remove_mark(fi, mk)
            self._last_click_added = None
        if self._click_locked:
            self.loupe_locked = False
            self._click_locked = False
        self.pop_topmost(idx)

    def wheelEvent(self, e) -> None:  # noqa: N802
        if e.modifiers() & Qt.ControlModifier:
            idx = self.locked_index if self.loupe_locked else self.hover_index
            if idx >= 0 and not self.compare:
                pos = e.position()
                rect = self._rect_for(idx)
                if rect.contains(pos):
                    n = norm_in_rect(rect, pos)
                    self.focus = QPointF(n[0], n[1])
                steps = e.angleDelta().y() / 120.0
                self.zoom = max(0.7, min(4.5, self.zoom * (1.15 ** steps)))
                self.viewport().update()
            e.accept()
            return
        super().wheelEvent(e)
        if not self.loupe_locked:
            self.hover_index = self.cell_at(e.position())
            self.viewport().update()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        key, text, mods = e.key(), e.text(), e.modifiers()
        if mods & Qt.ControlModifier:
            if key == Qt.Key_Z:
                self.pop_topmost(-1)
                return
            super().keyPressEvent(e)
            return
        if self._sel_valid() and self.sel is not None:
            fi, sel_mark = self.sel
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                self._remove_mark(fi, sel_mark)
                return
            arrows = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
            if key in arrows:
                step = 0.02 if mods & Qt.ShiftModifier else 0.004
                move_mark(sel_mark, [list(p) for p in sel_mark.pts], arrows[key][0] * step, arrows[key][1] * step)
                self.marksChanged.emit(fi)
                self.viewport().update()
                return
        if text in ("[", "]"):
            self.set_brush_size(self.brush_size * (1.15 if text == "]" else 1 / 1.15))
            return
        if text in ("1", "2", "3", "4"):
            self.set_brush_color(BRUSH_COLORS[int(text) - 1][1])
            return
        table = {"*": T_STAR, "p": T_STAR, "x": T_REJECT, "+": T_PUSH, "=": T_PUSH, "-": T_PULL,
                 "_": T_PULL, "#": T_CROP, "l": T_LINE, "a": T_ARROW, "r": T_RING, "v": T_INSPECT, "e": T_ADJUST}
        tool = table.get(text.lower()) if text else None
        if key == Qt.Key_Escape:
            if self.compare:
                self.toggle_compare(False)
            elif self._drag is not None:
                self._drag = None
                self.viewport().update()
            elif self.sel is not None:
                self.sel = None
                self.viewport().update()
            elif self.loupe_locked:
                self.loupe_locked = False
                self.viewport().update()
            elif self.tool != T_INSPECT:
                self.set_tool(T_INSPECT)
            elif self.window().isFullScreen():
                self.window().showNormal()
        elif text in ("c", "C"):
            self.toggle_compare()
        elif tool:
            self.set_tool(tool)
        elif key == Qt.Key_Space:
            if self.loupe_locked:
                self.loupe_locked = False
            elif self.hover_index >= 0 and not self.compare:
                self._lock_loupe(self.hover_index, self.hover_pos)
            self.viewport().update()
        elif text == "0":
            self.zoom = 1.0
            self.viewport().update()
        else:
            super().keyPressEvent(e)


# ════════════════════════════════════════════════════════════════════════════
# 5. ROLL PAGE, MAIN WINDOW, DESKTOP INTEGRATION
# ════════════════════════════════════════════════════════════════════════════

class RollPage(QWidget):
    """One tab: equipment header + contact sheet + background loader."""

    syncJobs = Signal(list)
    dirty = Signal()
    equipmentChanged = Signal()
    openEquipment = Signal(str)

    def __init__(self, cfg: ConfigStore, folder: Path, camera: str, film: str, lens: str,
                 marks: Optional[dict[str, list[dict[str, Any]]]], columns: int, tool: str,
                 loupe: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.roll = Roll(folder, film, camera, lens)
        self.roll.load_sidecar_marks()
        if marks:
            self.roll.apply_marks(marks)
        self.canvas = ContactSheetCanvas(self.roll)
        self.canvas._film_lookup = cfg.film_meta  # type: ignore[method-assign,assignment]
        self.canvas.columns = columns
        self.canvas.tool = tool
        self.canvas.loupe_enabled = loupe

        self.film_combo = FavCombo()
        self.camera_combo = FavCombo()
        self.lens_combo = FavCombo()
        for kind, combo in (("film", self.film_combo), ("camera", self.camera_combo), ("lens", self.lens_combo)):
            combo.itemContextRequested.connect(lambda name, pos, k=kind: self._combo_menu(k, name, pos))
            combo.setToolTip("Right-click for favorites / edit")
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(14)
            combo.view().setMinimumWidth(380)
        self.info = QLabel("")
        head = QHBoxLayout()
        head.setContentsMargins(10, 6, 10, 6)
        title = QLabel(f"ROLL  {folder.name}")
        title.setObjectName("amber")
        head.addWidget(title)
        for label, combo in (("Film", self.film_combo), ("Camera", self.camera_combo), ("Lens", self.lens_combo)):
            head.addSpacing(10)
            head.addWidget(QLabel(label))
            head.addWidget(combo)
        head.addStretch(1)
        head.addWidget(self.info)
        bar = QWidget()
        bar.setStyleSheet("background:#141414;")
        bar.setLayout(head)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(bar)
        lay.addWidget(self.canvas, 1)

        self._sync_dirty: set[int] = set()
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(1200)
        self._sync_timer.timeout.connect(self.flush_sync)
        self.canvas.marksChanged.connect(self._on_marks_changed)
        self.refresh_equipment()
        for combo in (self.film_combo, self.camera_combo, self.lens_combo):
            combo.activated.connect(self._equipment_changed)

        self.loader = ImageLoaderThread([f.path for f in self.roll.frames], self)
        self.loader.frameReady.connect(self.canvas.on_frame_ready)
        self.loader.frameFailed.connect(self._on_frame_failed)
        self.loader.progress.connect(self._on_progress)
        self.loader.start()
        self._update_info(0, len(self.roll.frames))

    def _on_progress(self, done: int, total: int) -> None:
        self._update_info(done, total)

    def _on_frame_failed(self, idx: int, msg: str) -> None:
        if 0 <= idx < len(self.roll.frames):
            self.roll.frames[idx].error = msg
            self.canvas.viewport().update()

    def _update_info(self, done: int, total: int) -> None:
        text = f"{total} frames" if done >= total else f"Developing… {done}/{total}"
        failed = [f.error for f in self.roll.frames if f.error]
        if failed:
            text += f"  ·  {len(failed)} couldn't be opened"
            self.info.setToolTip("\n".join(failed))
        self.info.setText(text)

    def _combo_menu(self, kind: str, name: str, gpos: QPoint) -> None:
        menu = QMenu(self)
        a_fav = menu.addAction("Remove from Favorites" if self.cfg.is_favorite(kind, name) else "Add to Favorites")
        a_edit = menu.addAction("Edit in Equipment Manager…")
        chosen = menu.exec(gpos)
        if chosen == a_fav:
            self.cfg.toggle_favorite(kind, name)
            self.equipmentChanged.emit()
        elif chosen == a_edit:
            self.openEquipment.emit(kind)

    def refresh_equipment(self) -> None:
        for kind, old, new in self.cfg.renames:  # follow renames made in the manager
            if getattr(self.roll, kind, None) == old:
                setattr(self.roll, kind, new)
        def fill(combo: QComboBox, items: list[tuple[str, str]], current: str) -> None:
            combo.blockSignals(True)
            combo.clear()
            for label, value in items:
                combo.addItem(label, value)
            if current and combo.findData(current) < 0:
                combo.addItem(current, current)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)
        fill(self.film_combo, [(("★ " if f.get("favorite") else "") + f["name"], f["name"])
                               for f in self.cfg.sorted_films()], self.roll.film)
        fill(self.camera_combo, [(("★ " if self.cfg.is_fav(c) else "") + c, c)
                                 for c in self.cfg.sorted_gear(self.cfg.cameras)], self.roll.camera)
        fill(self.lens_combo, [(("★ " if self.cfg.is_fav(l) else "") + l, l)
                               for l in self.cfg.sorted_gear(self.cfg.lenses)], self.roll.lens)
        self.roll.film = self.film_combo.currentData() or self.roll.film
        self.roll.camera = self.camera_combo.currentData() or self.roll.camera
        self.roll.lens = self.lens_combo.currentData() or self.roll.lens
        self.canvas.viewport().update()

    def _equipment_changed(self) -> None:
        self.roll.film = self.film_combo.currentData() or ""
        self.roll.camera = self.camera_combo.currentData() or ""
        self.roll.lens = self.lens_combo.currentData() or ""
        self._sync_dirty.update(range(len(self.roll.frames)))
        self._sync_timer.start()
        self.dirty.emit()
        self.canvas.viewport().update()
        self.canvas.setFocus()

    def _on_marks_changed(self, idx: int) -> None:
        self._sync_dirty.add(idx)
        self._sync_timer.start()
        self.dirty.emit()

    def build_job(self, idx: int) -> SyncJob:
        fr = self.roll.frames[idx]
        prof = self.cfg.profile
        return SyncJob(fr.path, [m.to_dict() for m in fr.marks], fr.rating, prof.get("artist", ""),
                       prof.get("copyright", ""), self.roll.camera, self.roll.lens,
                       int(self.cfg.film_meta(self.roll.film).get("iso", 400)), self.roll.film)

    def flush_sync(self, everything: bool = False) -> None:
        if not everything and not self.cfg.setting("sync_enabled"):
            self._sync_dirty.clear()
            return
        idxs = range(len(self.roll.frames)) if everything else sorted(self._sync_dirty)
        jobs = [self.build_job(i) for i in idxs]
        self._sync_dirty.clear()
        if jobs:
            self.syncJobs.emit(jobs)

    def to_session(self) -> dict[str, Any]:
        return {"folder": str(self.roll.folder), "film": self.roll.film, "camera": self.roll.camera,
                "lens": self.roll.lens, "marks": self.roll.marks_table()}

    def shutdown(self) -> None:
        self.loader.stop()
        self.loader.wait()  # stops after the current file; a QThread destroyed while running aborts the app


class MainWindow(QMainWindow):
    def __init__(self, cfg: Optional[ConfigStore] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        self.setMinimumSize(720, 480)
        self.resize(int(avail.width() * 0.92), int(avail.height() * 0.92))
        self.cfg = cfg or ConfigStore()
        self.tool = T_INSPECT
        self._tb_area = Qt.TopToolBarArea
        self._upd_worker: Optional[QThread] = None
        self._upd_install: Optional[QThread] = None
        self._session_dirty = False
        self._session_ready = False

        self.sync_worker = RapidSyncWorker(self)
        self.sync_worker.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.sync_worker.start()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Import a roll to begin  (File ▸ Import Roll…, Ctrl+O)")
        self.brush_size = 1.0
        self.brush_color = DEFAULT_BRUSH_COLOR
        self._build_actions()

        self.autosave = QTimer(self)
        self.autosave.setInterval(4000)
        self.autosave.timeout.connect(self._autosave_tick)
        self.autosave.start()
        self.statusBar().addPermanentWidget(QLabel(f"v{version_string()}  "))
        self.col_spin.setValue(int(self.cfg.setting("default_columns")))
        self.loupe_action.setChecked(bool(self.cfg.setting("hover_loupe")))
        self.apply_settings()

    # ── UI construction ────────────────────────────────────────────────────
    def _build_actions(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        self._act(m_file, "Import Roll…", self.import_roll, "Ctrl+O")
        self._act(m_file, "Close Roll", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        m_file.addSeparator()
        self._act(m_file, "Print Contact Sheet…", self.print_sheet, "Ctrl+P")
        self._act(m_file, "Export Contact Sheet as PDF…", self.export_pdf, "Ctrl+Shift+E")
        m_file.addSeparator()
        self._act(m_file, "Sync All Frames Now", self.sync_all, "Ctrl+S")
        self._act(m_file, "Save Session", self.save_session)
        m_file.addSeparator()
        self._act(m_file, "Settings…", self.open_settings, "Ctrl+,")
        m_file.addSeparator()
        self._act(m_file, "Quit", self.close, "Ctrl+Q")
        m_view = mb.addMenu("&View")
        self._act(m_view, "Full Screen (F11) / Exit Full Screen", self.toggle_fullscreen, "F11")
        m_eq = mb.addMenu("&Equipment")
        self._act(m_eq, "Equipment Inventory…", self.open_equipment, "Ctrl+E")
        self._act(m_eq, "User Profile && Defaults…", self.open_profile, "Ctrl+Shift+P")
        m_help = mb.addMenu("&Help")
        self._act(m_help, "About ProofMark", lambda: self.show_about(0))
        self._act(m_help, "About the Developer", lambda: self.show_about(1))
        self._act(m_help, "Version History", lambda: self.show_about(2))
        self._act(m_help, "Check for Updates…", lambda: self.check_for_updates(False))
        m_help.addSeparator()
        self._act(m_help, "Keyboard Reference", self.show_keys)

        tb = self.addToolBar("Tools")
        self.tool_bar = tb
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        group = QActionGroup(self)
        group.setExclusive(True)
        self.tool_actions: dict[str, QAction] = {}
        for tool, label, key, tip in TOOL_LIST:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setToolTip(tip)
            act.setChecked(tool == self.tool)
            act.triggered.connect(lambda _c=False, t=tool: self.set_tool(t))
            group.addAction(act)
            tb.addAction(act)
            self.tool_actions[tool] = act
        tb.addSeparator()
        self.compare_action = QAction("⇋ Compare", self)
        self.compare_action.setCheckable(True)
        self.compare_action.setToolTip("2-up compare  (C)  — hover to change [B]; C / Esc exits")
        self.compare_action.triggered.connect(self._compare_clicked)
        tb.addAction(self.compare_action)
        self.loupe_action = QAction("Loupe", self)
        self.loupe_action.setCheckable(True)
        self.loupe_action.setChecked(True)
        self.loupe_action.setToolTip("Hover loupe on / off")
        self.loupe_action.triggered.connect(self._loupe_toggled)
        tb.addAction(self.loupe_action)
        self.print_action = QAction("Print Sheet", self)
        self.print_action.setToolTip("Print a clean, unmarked contact sheet  (Ctrl+P)")
        self.print_action.triggered.connect(lambda _c=False: self.print_sheet())
        tb.addAction(self.print_action)
        tb.addSeparator()
        tb.addWidget(QLabel(" Columns "))
        self.col_spin = QSpinBox()
        self.col_spin.setRange(2, 14)
        self.col_spin.setValue(6)
        self.col_spin.valueChanged.connect(self._columns_changed)
        tb.addWidget(self.col_spin)

        # Second toolbar row: brush size slider + wax colours
        self.addToolBarBreak()
        bb = self.addToolBar("Brush")
        self.brush_bar = bb
        bb.setMovable(False)
        bb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        bb.addWidget(QLabel("Brush size"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(25, 400)
        self.size_slider.setValue(100)
        self.size_slider.setFixedWidth(220)
        self.size_slider.setToolTip("Mark / brush size  ( [ smaller   ] larger )")
        self.size_slider.valueChanged.connect(lambda v: self._apply_brush(size=v / 100.0))
        bb.addWidget(self.size_slider)
        self.size_label = QLabel("100%")
        self.size_label.setMinimumWidth(48)
        bb.addWidget(self.size_label)
        bb.addSeparator()
        bb.addWidget(QLabel("Wax colour"))
        cgroup = QActionGroup(self)
        cgroup.setExclusive(True)
        self.color_actions: dict[str, QAction] = {}
        for i, (cname, chex) in enumerate(BRUSH_COLORS, start=1):
            act = QAction(QIcon(self._swatch(chex)), cname, self)
            act.setCheckable(True)
            act.setToolTip(f"{cname}  (key {i})")
            act.triggered.connect(lambda _c=False, h=chex: self._apply_brush(color=h))
            cgroup.addAction(act)
            bb.addAction(act)
            self.color_actions[chex] = act
        self.color_actions[self.brush_color].setChecked(True)

        # Compact-menu mode: every menu collapses into one ☰ button at the start of the toolbar
        self.burger = QMenu(self)
        for act in mb.actions():
            self.burger.addAction(act)
        btn = QToolButton()
        btn.setText("☰  Menu")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setMenu(self.burger)
        self.menu_button_action = tb.insertWidget(self.tool_actions[T_INSPECT], btn)
        self.menu_button_action.setVisible(False)

    @staticmethod
    def _swatch(hex_color: str, size: int = 18) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#FFFFFF"), 1.2))
        p.setBrush(QColor(hex_color))
        p.drawRoundedRect(QRectF(1.5, 1.5, size - 3, size - 3), 4, 4)
        p.end()
        return pm

    def _apply_brush(self, size: Optional[float] = None, color: Optional[str] = None) -> None:
        if size is not None:
            self.brush_size = max(0.25, min(4.0, size))
        if color is not None and color in self.color_actions:
            self.brush_color = color
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(int(round(self.brush_size * 100)))
        self.size_slider.blockSignals(False)
        self.size_label.setText(f"{int(round(self.brush_size * 100))}%")
        self.color_actions[self.brush_color].setChecked(True)
        for page in self.pages():
            page.canvas.brush_size = self.brush_size
            page.canvas.brush_color = self.brush_color
            page.canvas.viewport().update()
        self.mark_dirty()

    def _canvas_brush_changed(self, size: float, color: str) -> None:
        self._apply_brush(size=size, color=color)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showMaximized()
            self.statusBar().showMessage("Left full screen.", 3000)
        else:
            self.showFullScreen()
            self.statusBar().showMessage("Full screen — press F11 or Esc to exit.", 5000)

    @staticmethod
    def _act(menu: QMenu, text: str, slot: Callable[[], None], shortcut: str = "") -> QAction:
        act = menu.addAction(text)
        if shortcut:
            act.setShortcut(shortcut)
            menu.window().addAction(act)  # keep shortcuts alive when the menu bar is hidden (compact mode)
        act.triggered.connect(lambda _c=False: slot())
        return act

    # ── helpers ────────────────────────────────────────────────────────────
    def pages(self) -> list[RollPage]:
        return [self.tabs.widget(i) for i in range(self.tabs.count())]  # type: ignore[misc]

    def current_page(self) -> Optional[RollPage]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, RollPage) else None

    def _wire_page(self, page: RollPage) -> None:
        page.canvas.toolChanged.connect(self._canvas_tool_changed)
        page.canvas.brushChanged.connect(self._canvas_brush_changed)
        page.equipmentChanged.connect(self._refresh_all_equipment)
        page.openEquipment.connect(self.open_equipment)
        page.canvas.compareChanged.connect(self._canvas_compare_changed)
        page.canvas.status.connect(lambda m: self.statusBar().showMessage(m, 5000))
        page.syncJobs.connect(self._forward_jobs)
        page.dirty.connect(self.mark_dirty)

    def _forward_jobs(self, jobs: list) -> None:
        for j in jobs:
            self.sync_worker.submit(j)

    def mark_dirty(self) -> None:
        self._session_dirty = True

    # ── tool / view state ──────────────────────────────────────────────────
    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.tool_actions[tool].setChecked(True)
        for page in self.pages():
            page.canvas.set_tool(tool)

    def _canvas_tool_changed(self, tool: str) -> None:
        self.tool = tool
        self.tool_actions[tool].setChecked(True)
        for page in self.pages():
            if page.canvas.tool != tool:
                page.canvas.tool = tool

    def _compare_clicked(self, checked: bool) -> None:
        page = self.current_page()
        if page:
            page.canvas.toggle_compare(checked)
            page.canvas.setFocus()
        else:
            self.compare_action.setChecked(False)

    def _canvas_compare_changed(self, on: bool) -> None:
        self.compare_action.blockSignals(True)
        self.compare_action.setChecked(on)
        self.compare_action.blockSignals(False)

    def _loupe_toggled(self, on: bool) -> None:
        for page in self.pages():
            page.canvas.set_loupe_enabled(on)

    def _columns_changed(self, n: int) -> None:
        for page in self.pages():
            page.canvas.set_columns(n)
        self.mark_dirty()

    def _tab_changed(self, _i: int) -> None:
        page = self.current_page()
        if page:
            self._canvas_compare_changed(page.canvas.compare)
            page.canvas.setFocus()
        self.mark_dirty()

    # ── roll management ────────────────────────────────────────────────────
    def import_roll(self) -> None:
        start = self.cfg.data.get("last_import_parent") or str(Path.home() / "Pictures")
        if not Path(start).is_dir():
            start = str(Path.home())
        # Opens the PARENT directory holding your roll folders, not inside an active roll.
        folder = QFileDialog.getExistingDirectory(self, "Import Roll — choose a roll folder", start)
        if not folder:
            return
        path = Path(folder)
        self.cfg.data["last_import_parent"] = str(path.parent)
        self.cfg.save()
        self.open_roll(path)

    def open_roll(self, folder: Path, film: str = "", camera: str = "", lens: str = "",
                  marks: Optional[dict[str, list[dict[str, Any]]]] = None) -> Optional[RollPage]:
        prof = self.cfg.profile
        try:
            page = RollPage(self.cfg, folder, camera or prof.get("camera", ""), film or prof.get("film", ""),
                            lens or prof.get("lens", ""), marks, self.col_spin.value(), self.tool,
                            self.loupe_action.isChecked())
        except OSError as exc:
            QMessageBox.warning(self, "Cannot open roll", f"{folder}\n\n{exc}")
            return None
        self._wire_page(page)
        page.canvas.brush_size = self.brush_size
        page.canvas.brush_color = self.brush_color
        idx = self.tabs.addTab(page, folder.name)
        self.tabs.setCurrentIndex(idx)
        self.mark_dirty()
        self.statusBar().showMessage(f"Opened {folder} — {len(page.roll.frames)} frames", 5000)
        return page

    def close_tab(self, index: int) -> None:
        if index < 0:
            return
        page = self.tabs.widget(index)
        if isinstance(page, RollPage):
            page.flush_sync()
            page.shutdown()
        self.tabs.removeTab(index)
        if page:
            page.deleteLater()
        self.mark_dirty()

    def sync_all(self) -> None:
        for page in self.pages():
            page.flush_sync(everything=True)
        self.statusBar().showMessage("Sync queued for all frames…", 4000)

    # ── dialogs ────────────────────────────────────────────────────────────
    def _refresh_all_equipment(self) -> None:
        for p in self.pages():
            p.refresh_equipment()
        self.cfg.renames.clear()
        self.mark_dirty()

    def open_equipment(self, kind: str = "") -> None:
        dlg = EquipmentManagerDialog(self.cfg, self)
        if kind:
            dlg.select_tab(kind)
        dlg.changed.connect(self._refresh_all_equipment)
        dlg.exec()
        self._refresh_all_equipment()

    def open_profile(self) -> None:
        UserProfileDialog(self.cfg, self).exec()

    def show_keys(self) -> None:
        QMessageBox.information(self, "Keyboard Reference", (
            "TOOLS\n"
            "P or *  Star pick      X  Boxed reject      + / -  Exposure push / pull\n"
            "#  Crop box            R  Squircle ring     L  Line      A  Arrow\n"
            "E  Adjust (select / move / resize marks)     V or Esc  Inspect\n\n"
            "PLACING MARKS\n"
            "Click places at the brush size.  Click-drag: stars, X, + and - grow outward;\n"
            "crop box and ring drag out a box.  Box, arrow and line stay selected so you can drag\n"
            "their handles.  Adjust tool: click a mark, drag to move, drag handles to resize.\n"
            "Delete removes the selected mark, arrow keys nudge it (Shift = bigger steps), Esc deselects.\n\n"
            "BRUSH\n[ / ]  Smaller / larger     1-4  Red / Toxic green / Silver / Yellow\n\n"
            "VIEWING\n"
            "Hover  50% loupe     Click (Inspect) / Right-click / Space  Enlarge to 85%\n"
            "Ctrl+Wheel  Zoom 0.7×–4.5×     Right-drag  Pan     0  Reset zoom\n"
            "C  2-up compare (hover for [B]); C or Esc exits\n\n"
            "UNDO\nDouble-click  Pop topmost mark     Ctrl+Z  Pop most recent mark\n\n"
            "FILE\nCtrl+O Import   Ctrl+P Print   Ctrl+Shift+E Export PDF   Ctrl+, Settings   F11 Full screen"))

    # ── session persistence ────────────────────────────────────────────────
    def save_session(self) -> None:
        state = {"version": 1, "current_tab": self.tabs.currentIndex(), "tool": self.tool,
                 "columns": self.col_spin.value(), "brush_size": self.brush_size,
                 "brush_color": self.brush_color, "rolls": [p.to_session() for p in self.pages()]}
        try:
            atomic_write_json(SESSION_FILE, state)
            self._session_dirty = False
        except OSError:
            pass

    def _autosave_tick(self) -> None:
        if self._session_dirty and self._session_ready:
            self.save_session()

    def offer_session_restore(self) -> None:
        try:
            state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            rolls = state.get("rolls", [])
        except (OSError, ValueError):
            rolls, state = [], {}
        if rolls and QMessageBox.question(
                self, "Restore session", "Reopen previous contact sheet session?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
            self.col_spin.setValue(int(state.get("columns", 6)))
            self._apply_brush(float(state.get("brush_size", 1.0)), state.get("brush_color", DEFAULT_BRUSH_COLOR))
            self.set_tool(state.get("tool", T_INSPECT) if state.get("tool") in self.tool_actions else T_INSPECT)
            for r in rolls:
                folder = Path(r.get("folder", ""))
                if folder.is_dir():
                    self.open_roll(folder, r.get("film", ""), r.get("camera", ""), r.get("lens", ""),
                                   r.get("marks"))
            cur = int(state.get("current_tab", 0))
            if 0 <= cur < self.tabs.count():
                self.tabs.setCurrentIndex(cur)
        self._session_ready = True
        self._session_dirty = False
        prev = self.cfg.data.get("last_seen_version")
        if prev and prev != APP_VERSION:
            self.statusBar().showMessage(f"Updated to v{APP_VERSION} (build {BUILD_NUMBER}) — see Help ▸ Version History", 12000)
        if prev != APP_VERSION:
            self.cfg.data["last_seen_version"] = APP_VERSION
            self.cfg.save()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._session_ready:
            self.save_session()
        for page in self.pages():
            page.flush_sync()
        self.sync_worker.stop()
        self.sync_worker.wait()  # finish writing pending sidecars before quitting
        for page in self.pages():
            page.shutdown()
        for worker in (self._upd_worker, self._upd_install):
            if worker is not None and worker.isRunning():
                worker.wait()  # bounded by the workers' own network / git timeouts
        event.accept()

    def show_about(self, tab: int = 0) -> None:
        AboutDialog(self.cfg, tab, self).exec()

    # ── settings ───────────────────────────────────────────────────────────
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.apply_settings()
            if dlg.restart_needed():
                QMessageBox.information(self, "Restart needed",
                                        "The interface scale will apply the next time ProofMark starts.")

    def apply_settings(self) -> None:
        c = self.cfg
        app = QApplication.instance()
        set_accent(str(c.setting("accent")))
        set_rebate_scale(int(c.setting("rebate_scale")))
        family, size = str(c.setting("font_family")), int(c.setting("font_size"))
        font = QFont(app.font())
        if family:
            font.setFamily(family)
        font.setPointSize(size)
        app.setFont(font)
        app.setStyleSheet(build_style(family, size, ACCENT_HEX))
        self._apply_layout_prefs()
        self.autosave.setInterval(max(2, int(c.setting("autosave_secs"))) * 1000)
        self.sync_worker.rapidraw_exif = bool(c.setting("rapidraw_exif"))
        for page in self.pages():
            page.canvas.viewport().update()

    def _apply_layout_prefs(self) -> None:
        area = Qt.BottomToolBarArea if self.cfg.setting("toolbar_area") == "bottom" else Qt.TopToolBarArea
        if area != self._tb_area:
            self.removeToolBarBreak(self.brush_bar)
            self.removeToolBar(self.tool_bar)
            self.removeToolBar(self.brush_bar)
            self.addToolBar(area, self.tool_bar)
            self.addToolBarBreak(area)
            self.addToolBar(area, self.brush_bar)
            self.tool_bar.show()
            self.brush_bar.show()
            self._tb_area = area
        compact = self.cfg.setting("menu_mode") == "compact"
        self.menuBar().setVisible(not compact)
        self.menu_button_action.setVisible(compact)

    # ── printing ───────────────────────────────────────────────────────────
    def _make_printer(self, page: RollPage, pdf_path: str = "") -> Any:
        printer = QPrinter(QPrinter.HighResolution)
        if pdf_path:
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(pdf_path)
        printer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)
        printer.setDocName(f"{APP_NAME} – {page.roll.folder.name}")
        return printer

    def _print_roll(self, page: RollPage, printer: Any) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            pages = render_contact_sheet(
                printer, page.roll, page.canvas.columns, self.cfg.film_meta(page.roll.film),
                page.canvas.preview_pixmap, bool(self.cfg.setting("print_ink_saver")),
                bool(self.cfg.setting("print_header")))
        finally:
            QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"Contact sheet sent: {pages} page(s)." if pages else
                                     "Printing failed — check the printer or output path.", 6000)

    def _printable_page(self) -> Optional[RollPage]:
        page = self.current_page()
        if QPrinter is None:
            QMessageBox.warning(self, "Print", "Qt print support isn't available in this installation.")
            return None
        if page is None:
            self.statusBar().showMessage("Import a roll first.", 4000)
            return None
        if page.loader.isRunning() and QMessageBox.question(
                self, "Print", "Some frames are still loading. Print anyway?") != QMessageBox.Yes:
            return None
        return page

    def print_sheet(self) -> None:
        page = self._printable_page()
        if page is None:
            return
        printer = self._make_printer(page)
        if not self.cfg.setting("print_direct"):
            dlg = QPrintDialog(printer, self)
            dlg.setWindowTitle("Print Contact Sheet")
            if not dlg.exec():
                return
        self._print_roll(page, printer)

    def export_pdf(self) -> None:
        page = self._printable_page()
        if page is None:
            return
        default = str(page.roll.folder / f"{page.roll.folder.name}_contact_sheet.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Export Contact Sheet as PDF", default, "PDF (*.pdf)")
        if path:
            self._print_roll(page, self._make_printer(page, path if path.lower().endswith(".pdf") else path + ".pdf"))

    # ── updates ────────────────────────────────────────────────────────────
    def _update_repo(self) -> str:
        return (str(self.cfg.setting("update_repo")).strip() or UPDATE_REPO).strip()

    def _auto_update_check(self) -> None:
        import time
        if self.cfg.setting("auto_update_check") and time.time() - float(self.cfg.data.get("last_update_check", 0)) > 86400:
            self.check_for_updates(silent=True)

    def check_for_updates(self, silent: bool = False) -> None:
        repo = self._update_repo()
        if not repo:
            if not silent:
                QMessageBox.information(
                    self, "Check for Updates",
                    "No update source is configured yet.\n\nOpen Settings ▸ Updates and enter the GitHub "
                    "repository that publishes ProofMark releases (owner/repo).")
            return
        if self._upd_worker is not None and self._upd_worker.isRunning():
            return
        if not silent:
            self.statusBar().showMessage("Checking for updates…", 4000)
        worker = UpdateCheckWorker(repo, self)
        worker.finishedCheck.connect(lambda info: self._update_checked(info, silent))
        self._upd_worker = worker
        worker.start()

    def _update_checked(self, info: dict, silent: bool) -> None:
        import time
        if info.get("ok"):
            self.cfg.data["last_update_check"] = time.time()
            self.cfg.save()
        else:
            if not silent:
                QMessageBox.warning(self, "Check for Updates",
                                    f"Couldn't reach the update server.\n\n{info.get('error', '')}")
            return
        if parse_version(str(info["latest"])) <= parse_version(APP_VERSION):
            if not silent:
                QMessageBox.information(self, "Check for Updates", f"You're up to date — ProofMark {version_string()}.")
            return
        kind = install_kind()
        if UpdateDialog(info, kind, self).exec() == 2:
            self.statusBar().showMessage("Installing update…")
            worker = UpdateInstallWorker(kind, str(info["latest"]), info.get("assets", {}), self)
            worker.finishedInstall.connect(self._update_installed)
            self._upd_install = worker
            worker.start()

    def _update_installed(self, ok: bool, message: str) -> None:
        if not ok:
            QMessageBox.warning(self, "Update failed", message)
            return
        if QMessageBox.question(self, "Update installed", f"{message}\n\nRestart ProofMark now?") == QMessageBox.Yes:
            script = Path(__file__).resolve()
            QProcess.startDetached("/bin/sh", ["-c", f'sleep 1; exec "{sys.executable}" "{script}"'])
            self.close()


# ── Fedora integration ──────────────────────────────────────────────────────

def build_icon_pixmap(size: int = 512) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    s = float(size)
    path = QPainterPath()
    path.addRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.16, s * 0.16)
    p.fillPath(path, QColor("#141414"))
    p.setPen(QPen(C_AMBER, s * 0.025))
    p.drawPath(path)
    # contact-sheet frames
    p.setPen(Qt.NoPen)
    for r in range(2):
        for c in range(3):
            p.fillRect(QRectF(s * (0.15 + c * 0.235), s * (0.17 + r * 0.28), s * 0.2, s * 0.2), QColor("#2a2a2a"))
            p.fillRect(QRectF(s * (0.15 + c * 0.235), s * (0.375 + r * 0.28), s * 0.2, s * 0.02), C_AMBER_DIM)
    # rebate strip
    p.fillRect(QRectF(s * 0.12, s * 0.75, s * 0.76, s * 0.06), QColor("#050505"))
    draw_barcode(p, QRectF(s * 0.62, s * 0.762, s * 0.24, s * 0.036), "|!| |!!| ! |.|!| |!|", C_AMBER)
    # grease star
    cx, cy, r = s * 0.5, s * 0.37, s * 0.19
    star = [QPointF(cx + r * math.cos(-math.pi / 2 + 4 * math.pi / 5 * i),
                    cy + r * math.sin(-math.pi / 2 + 4 * math.pi / 5 * i)) for i in range(6)]
    render_heavy_grease_stroke(p, star, s * 0.03, C_GREASE, 42)
    p.end()
    return pm


def _desktop_dir() -> Path:
    """Locate the user's Desktop folder (honours XDG user dirs / localisation)."""
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5, check=False)
        p = Path(out.stdout.strip())
        if out.returncode == 0 and out.stdout.strip() and p != Path.home():
            return p
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Desktop"


def install_desktop_integration(force: bool = False, desktop_shortcut: bool = False) -> bool:
    """
    Write proofmark.desktop (app menu) and proofmark.png, and optionally place a clickable
    launcher on the Desktop, marked executable + trusted so GNOME/KDE run it on double-click.
    """
    if install_kind() in ("flatpak", "system"):
        return True  # the package already ships the .desktop file and icon
    try:
        icon_dir = Path.home() / ".local" / "share" / "pixmaps"
        app_dir = Path.home() / ".local" / "share" / "applications"
        icon_dir.mkdir(parents=True, exist_ok=True)
        app_dir.mkdir(parents=True, exist_ok=True)
        icon_path = icon_dir / "proofmark.png"
        script = Path(sys.argv[0]).resolve()
        entry = (
            "[Desktop Entry]\nVersion=1.0\nType=Application\nName=ProofMark\nGenericName=Contact Sheet Proofing\n"
            "Comment=Darkroom contact sheet marking with wax grease pencil\n"
            f'Exec="{sys.executable}" "{script}"\nPath={script.parent}\nIcon={icon_path}\nTerminal=false\n'
            "Categories=Graphics;Photography;\nStartupWMClass=proofmark\nStartupNotify=true\n")
        if force or not icon_path.exists():
            build_icon_pixmap(512).save(str(icon_path), "PNG")
        targets = [app_dir / "proofmark.desktop"]
        if desktop_shortcut:
            desk = _desktop_dir()
            desk.mkdir(parents=True, exist_ok=True)
            targets.append(desk / "ProofMark.desktop")
        for target in targets:
            if force or not target.exists() or target.read_text(encoding="utf-8") != entry:
                target.write_text(entry, encoding="utf-8")
            target.chmod(0o755)
            if target.parent != app_dir and shutil.which("gio"):
                # GNOME requires the "trusted" flag before a Desktop launcher will run
                subprocess.run(["gio", "set", str(target), "metadata::trusted", "true"],
                               capture_output=True, timeout=10, check=False)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(app_dir)], capture_output=True, timeout=15, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    if "--version" in sys.argv:
        print(f"{APP_NAME} {version_string(True)}")
        return 0
    cfg = ConfigStore()
    scale = int(cfg.setting("ui_scale"))
    if scale != 100 and "QT_SCALE_FACTOR" not in os.environ:
        os.environ["QT_SCALE_FACTOR"] = f"{scale / 100:.2f}"
    exporting = "--export-icon" in sys.argv
    if exporting:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    kind = install_kind()
    # Identity must be set before QApplication exists: Qt registers the app ID with the
    # desktop portal during start-up, and changing it afterwards fails on Wayland.
    QApplication.setApplicationName("proofmark")
    QApplication.setApplicationVersion(APP_VERSION)
    QApplication.setDesktopFileName(APP_ID if kind in ("flatpak", "system") else "proofmark")
    app = QApplication(sys.argv)
    if exporting:
        i = sys.argv.index("--export-icon")
        out = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else Path("proofmark-512.png")
        build_icon_pixmap(512).save(str(out), "PNG")
        print(f"Wrote {out}")
        return 0
    app.setApplicationDisplayName(APP_NAME)
    set_accent(str(cfg.setting("accent")))
    app.setStyleSheet(build_style(str(cfg.setting("font_family")), int(cfg.setting("font_size")), ACCENT_HEX))
    first_time = not cfg.data.get("desktop_shortcut_made", False)
    if install_desktop_integration(desktop_shortcut=first_time) and first_time:
        cfg.data["desktop_shortcut_made"] = True  # only auto-create once
        cfg.save()
    app.setWindowIcon(QIcon(build_icon_pixmap(256)))
    win = MainWindow(cfg)
    win.showMaximized()  # fills the screen but keeps the title bar (move / resize / close); F11 = true full screen
    QTimer.singleShot(0, win.offer_session_restore)
    QTimer.singleShot(5000, win._auto_update_check)
    # Ctrl+C in the terminal: close the window normally (saves the session and sidecars).
    # Python only sees the signal when it runs some code, so a timer wakes it periodically.
    signal.signal(signal.SIGINT, lambda *_a: win.close())
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
