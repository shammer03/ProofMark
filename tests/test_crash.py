"""Crashes and broken installs leave a trail: crash.log, a clear start-up message and a safe restart."""
from __future__ import annotations

import faulthandler
import subprocess
import sys
import unittest
from unittest import mock

from support import SANDBOX, ROOT, pm, pump
from PySide6.QtWidgets import QMessageBox


class CrashLogTest(unittest.TestCase):
    def setUp(self) -> None:
        pm.CRASH_LOG.unlink(missing_ok=True)
        pm.RUNNING_FLAG.unlink(missing_ok=True)
        self.old_hook = sys.excepthook

    def tearDown(self) -> None:
        sys.excepthook = self.old_hook
        faulthandler.disable()
        if getattr(self, "notice", None) is not None and hasattr(self.notice, "fault_file"):
            self.notice.fault_file.close()
        pm.qInstallMessageHandler(None)
        pm.RUNNING_FLAG.unlink(missing_ok=True)

    def test_errors_in_qt_callbacks_are_logged_and_reported_once(self) -> None:
        notice = self.notice = pm.start_crash_logging()
        shown = []
        with mock.patch.object(QMessageBox, "open", lambda box: shown.append(box.detailedText())), \
                mock.patch("sys.stderr"):
            for _ in range(2):
                try:
                    raise RuntimeError("frame went missing")
                except RuntimeError as exc:
                    notice.excepthook(type(exc), exc, exc.__traceback__)
            pump(100)
        log = pm.CRASH_LOG.read_text(encoding="utf-8")
        self.assertEqual(log.count("RuntimeError: frame went missing"), 2)
        self.assertIn(pm.APP_VERSION, log)
        self.assertEqual(len(shown), 1, "the warning should appear once per run")
        self.assertIn("frame went missing", shown[0])

    def test_errors_in_background_threads_are_logged(self) -> None:
        notice = self.notice = pm.start_crash_logging()

        class Worker(pm.QThread):
            @pm.logs_errors
            def run(self) -> None:
                raise ValueError("bad raw file")

        with mock.patch.object(QMessageBox, "open", lambda box: None), mock.patch("sys.stderr"):
            w = Worker()
            w.start()
            self.assertTrue(w.wait(5000), "a thread that hit an error must still finish")
            pump(100)
        self.assertIn("ValueError: bad raw file", pm.CRASH_LOG.read_text(encoding="utf-8"))

    def test_sync_keeps_going_after_an_error(self) -> None:
        self.notice = pm.start_crash_logging()
        sync = pm.RapidSyncWorker()
        done = []
        sync.status.connect(done.append)
        with mock.patch.object(QMessageBox, "open", lambda box: None), mock.patch("sys.stderr"), \
                mock.patch.object(sync, "_process", side_effect=OSError("disk full")):
            sync.start()
            for _ in range(2):  # the second job shows the worker is still alive after the first error
                sync.submit(pm.SyncJob(SANDBOX / "a.jpg", [], 0, "", "", "", "", 0, "", {}))
                for _ in range(50):
                    pump(20)
                    if done:
                        break
                self.assertTrue(done, "sync stopped after an error")
                done.clear()
            sync.stop()
            self.assertTrue(sync.wait(5000), "sync must still stop cleanly")
        self.assertEqual(pm.CRASH_LOG.read_text(encoding="utf-8").count("OSError: disk full"), 2)

    def test_hard_crash_is_written_by_faulthandler(self) -> None:
        home = SANDBOX / "home"
        code = ("import sys, ctypes; sys.path.insert(0, %r); import proofmark as pm; "
                "from PySide6.QtWidgets import QApplication; app = QApplication([]); "
                "pm.start_crash_logging(); ctypes.string_at(0)" % str(ROOT))
        subprocess.run([sys.executable, "-c", code], env={**pm.os.environ, "HOME": str(home)},
                       capture_output=True, timeout=60)
        self.assertIn("Fatal Python error", pm.CRASH_LOG.read_text(encoding="utf-8"))

    def test_log_is_kept_small(self) -> None:
        pm.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        pm.CRASH_LOG.write_text("old\n" * pm.CRASH_LOG_MAX, encoding="utf-8")
        pm.trim_crash_log()
        self.assertEqual(pm.CRASH_LOG.stat().st_size, pm.CRASH_LOG_MAX)

    def test_running_flag_detects_a_crash(self) -> None:
        self.assertFalse(pm.mark_running())
        pm.mark_closed()
        self.assertFalse(pm.mark_running(), "a clean close is not a crash")
        self.assertTrue(pm.mark_running(), "the flag was never removed: the last run crashed")


class SafeStartTest(unittest.TestCase):
    def ask(self, crashed: bool) -> tuple[str, object]:
        pm.atomic_write_json(pm.SESSION_FILE, {"rolls": [{"folder": "/nonexistent"}]})
        win = pm.MainWindow(pm.ConfigStore())
        win.last_run_crashed = crashed
        asked = []
        with mock.patch.object(QMessageBox, "question",
                               lambda *a: asked.append((a[2], a[4])) or QMessageBox.No):
            win.offer_session_restore()
        win.close()
        win.deleteLater()
        pump(20)
        return asked[0]

    def test_normal_start_offers_to_reopen(self) -> None:
        text, default = self.ask(False)
        self.assertNotIn("unexpectedly", text)
        self.assertEqual(default, QMessageBox.Yes)

    def test_after_a_crash_the_default_is_not_to_reopen(self) -> None:
        text, default = self.ask(True)
        self.assertIn("closed unexpectedly", text)
        self.assertEqual(default, QMessageBox.No)


class StartupCheckTest(unittest.TestCase):
    def test_missing_library_names_the_package_to_install(self) -> None:
        self.assertIn("sudo dnf install python3-pyside6",
                      pm.missing_library_help(ImportError("No module named 'PySide6'", name="PySide6")))
        self.assertIn("sudo dnf install python3-pillow",
                      pm.missing_library_help(ImportError("No module named 'PIL'", name="PIL")))
        broken = ImportError("libQt6Core.so.6: version `Qt_6.12' not found")
        self.assertIn("python3-pyside6", pm.missing_library_help(broken))

    def test_missing_pyside_exits_with_a_message(self) -> None:
        home = SANDBOX / "home"
        code = ("import sys; sys.modules['PySide6'] = None; sys.path.insert(0, %r); import proofmark" % str(ROOT))
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={**pm.os.environ, "HOME": str(home), "PATH": "/nonexistent"})
        self.assertEqual(res.returncode, 1)
        self.assertIn("sudo dnf install python3-pyside6", res.stderr)
        self.assertIn("start-up", (home / ".config" / "proofmark" / "crash.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
