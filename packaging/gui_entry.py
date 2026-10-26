"""Entry point of the desktop app (packaged as 'PDF Accessibility Studio.exe')."""
import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _log_crash() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "pdfa11y"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "crash.log"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n" + "=" * 60 + "\n" + traceback.format_exc())
    return p


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from pdfa11y.gui.main import main
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        log = _log_crash()
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "PDF Accessibility Studio",
                                 f"The app hit an unexpected problem and has to close.\n\nDetails were saved to:\n{log}")
        except Exception:
            pass
        raise SystemExit(1)
