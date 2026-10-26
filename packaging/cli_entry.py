"""Entry point of the command-line tool (packaged as 'pdfa11y.exe')."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()          # needed for batch mode's worker processes in a packaged app
    from pdfa11y.__main__ import main
    raise SystemExit(main())
