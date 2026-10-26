# PyInstaller recipe: one shared folder holding the desktop app and the command-line tool.
# Build with:  python packaging/build.py     (which also adds the Java/veraPDF tools, self-tests and zips)
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
SRC = str(ROOT / "src")

datas, binaries, hidden = [], [], ["lxml.etree", "lxml._elementpath"]
for pkg in ("pikepdf", "pdfminer", "pypdfium2", "pypdfium2_raw"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
datas.append((str(ROOT / "src" / "pdfa11y" / "gui" / "icon.png"), "pdfa11y/gui"))
datas.append((str(ROOT / "src" / "pdfa11y" / "web" / "static"), "pdfa11y/web/static"))   # the browser interface
# the web server loads parts of itself by name at run time: make sure they are all included
for pkg in ("uvicorn", "anyio", "starlette", "h11", "python_multipart", "multipart", "pdfa11y.web"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

# Qt modules the app never touches: keeping them out saves a lot of space
EXCLUDES = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "IPython", "pytest", "setuptools",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSql", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSerialPort", "PySide6.QtSensors",
    "PySide6.QtRemoteObjects", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtNetwork", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg",
    "PySide6.QtSvgWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPrintSupport", "PySide6.QtXml",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
    "PySide6.QtWebSockets", "PySide6.QtSpatialAudio", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
]

def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[SRC], binaries=binaries, datas=datas,
                    hiddenimports=hidden, excludes=EXCLUDES, noarchive=False)

a_gui, a_cli, a_web = analysis("gui_entry.py"), analysis("cli_entry.py"), analysis("web_entry.py")
MERGE((a_gui, "gui_entry", "PDF Accessibility Studio"), (a_cli, "cli_entry", "pdfa11y"),
      (a_web, "web_entry", "PDF Accessibility Studio (browser)"))

exe_gui = EXE(PYZ(a_gui.pure), a_gui.scripts, [], exclude_binaries=True, name="PDF Accessibility Studio",
              console=False, icon=str(ROOT / "packaging" / "app.ico"))
exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True, name="pdfa11y",
              console=True, icon=str(ROOT / "packaging" / "app.ico"))

exe_web = EXE(PYZ(a_web.pure), a_web.scripts, [], exclude_binaries=True, name="PDF Accessibility Studio (browser)",
              console=False, icon=str(ROOT / "packaging" / "app.ico"))

COLLECT(exe_gui, a_gui.binaries, a_gui.datas, exe_cli, a_cli.binaries, a_cli.datas, exe_web, a_web.binaries, a_web.datas,
        strip=False, upx=False, name="PDF Accessibility Studio")
