"""Entry point of the browser version (packaged as 'PDF Accessibility Studio (browser).exe').

No console window: it starts the local server, opens your browser, and stops by itself a few minutes after the last
browser tab closes. Starting it a second time just re-opens the running copy. Problems are written to
%LOCALAPPDATA%\pdfa11y\web.log and shown in a message box.
"""
import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _logdir() -> Path:
    d = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "pdfa11y"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _redirect_output() -> None:
    """A windowed program has no console: send anything printed to a log file instead of failing."""
    if sys.stdout is None or sys.stderr is None:
        log = open(_logdir() / "web.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def _message(title: str, text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    multiprocessing.freeze_support()          # the processing workers are started from this same program
    _redirect_output()
    try:
        from pdfa11y.web.server import run
        code = run(open_browser=os.environ.get("PDFA11Y_NO_BROWSER") != "1", idle_minutes=10, single_instance=True)
        raise SystemExit(code)
    except SystemExit:
        raise
    except BaseException:
        p = _logdir() / "web.log"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 60 + "\n" + traceback.format_exc())
        _message("PDF Accessibility Studio", f"The tool could not start.\n\nDetails were saved to:\n{p}")
        raise SystemExit(1)
