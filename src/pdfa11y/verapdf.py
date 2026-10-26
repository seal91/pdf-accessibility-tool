"""veraPDF integration: independent PDF/UA-1 validation, fully offline.

The tool looks for a bundled Java runtime and veraPDF under a `tools` folder:
    tools/jre/<jdk-...>/bin/java.exe   (Linux: .../bin/java; a Java on the PATH or JAVA_HOME is used if none is bundled)
    tools/verapdf/bin/*.jar
Set PDFA11Y_TOOLS to point somewhere else (e.g. next to a packaged .exe).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pikepdf

PDFUA_NS = "http://www.aiim.org/pdfua/ns/id/"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Plain-language versions of the rules we see most. Anything else falls back to veraPDF's own text.
FRIENDLY = {
    ("7.3", 1): "Images need alt text (or must be marked as decorative)",
    ("7.1", 3): "Some content is neither tagged nor marked as decoration",
    ("7.1", 11): "The document has no tag structure",
    ("7.1", 10): "The window title setting (DisplayDocTitle) is missing",
    ("7.1", 9): "The document title is missing from the metadata",
    ("7.2", 34): "The document language is missing",
    ("5", 1): "The PDF/UA identifier is not set",
    ("7.18.1", 2): "A link/annotation is not tagged",
    ("7.18.5", 1): "A link is not tagged as a Link",
    ("7.18.5", 2): "A link has no description",
    ("7.18.3", 1): "Tab order is not set on a page with links",
    ("7.5", 1): "A table has no header cells",
    ("7.5", 2): "A table header cell has no scope/association",
    ("7.4.4", 1): "Heading levels skip or do not nest correctly",
}


def tools_dir() -> Path:
    env = os.environ.get("PDFA11Y_TOOLS")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):                       # packaged exe
        return Path(sys.executable).parent / "tools"
    return Path(__file__).resolve().parents[2] / "tools"


def _find_java(t: Path) -> Path | None:
    """The bundled Java if there is one (Windows: java.exe, Linux/macOS: java), else a Java on this machine."""
    exe = "java.exe" if os.name == "nt" else "java"
    bundled = next(iter(sorted(t.glob(f"jre/*/bin/{exe}"))), None) or next(iter(sorted(t.glob(f"jre/bin/{exe}"))), None)
    if bundled:
        return bundled
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / exe).exists():
        return Path(home) / "bin" / exe
    found = shutil.which("java")
    return Path(found) if found else None


@lru_cache(maxsize=1)
def locate() -> tuple[Path, Path] | None:
    """(java executable, verapdf folder) or None when not installed."""
    t = tools_dir()
    java = _find_java(t)
    vp = t / "verapdf"
    if java and (vp / "bin").is_dir() and any((vp / "bin").glob("cli-*.jar")):
        return java, vp
    return None


def available() -> bool:
    return locate() is not None


def _run(pdf: Path, timeout: int = 600) -> dict:
    loc = locate()
    if not loc:
        raise RuntimeError("veraPDF is not installed (expected under the tools folder)")
    java, vp = loc
    cmd = [str(java), "-Xmx1g", "-Dfile.encoding=UTF8", "-XX:+IgnoreUnrecognizedVMOptions",
           "--add-exports=java.base/sun.security.pkcs=ALL-UNNAMED",
           f"-Dapp.home={vp}", f"-Dbasedir={vp}",
           "-cp", f"{vp / 'etc'};{vp / 'bin'}/*" if os.name == "nt" else f"{vp / 'etc'}:{vp / 'bin'}/*",
           "org.verapdf.apps.GreenfieldCliWrapper",
           "--flavour", "ua1", "--format", "json", "--maxfailuresdisplayed", "1", str(pdf)]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out = p.stdout.decode("utf-8", "replace")
    if "{" not in out:
        raise RuntimeError(f"veraPDF produced no report: {p.stderr.decode('utf-8', 'replace')[:300]}")
    return json.loads(out[out.index("{"):])


def validate(pdf: str | Path) -> dict:
    """{'compliant': bool, 'failed_rules': [{clause, test, count, description, friendly}], 'failed_checks': int}"""
    d = _run(Path(pdf))
    job = d["report"]["jobs"][0]
    vr = job.get("validationResult")
    if isinstance(vr, list):
        vr = vr[0] if vr else None
    if vr is None:
        task = (job.get("taskException") or job.get("taskResult") or {})
        raise RuntimeError(f"veraPDF could not validate the file: {str(task)[:200]}")
    det = vr["details"]
    rules = []
    for r in sorted(det["ruleSummaries"], key=lambda r: -r["failedChecks"]):
        key = (str(r["clause"]), int(r["testNumber"]))
        rules.append({"clause": r["clause"], "test": r["testNumber"], "count": r["failedChecks"],
                      "description": r["description"], "friendly": FRIENDLY.get(key)})
    return {"compliant": bool(vr["compliant"]), "failed_rules": rules,
            "failed_checks": det["failedChecks"], "passed_rules": det["passedRules"]}


def is_identifier_only(v: dict) -> bool:
    """True when the ONLY failing rule is the missing PDF/UA identifier (clause 5, test 1)."""
    return bool(v["failed_rules"]) and all(str(r["clause"]) == "5" and int(r["test"]) == 1 for r in v["failed_rules"])


def stamp_pdfua(src: str | Path, dst: str | Path) -> None:
    """Write the PDF/UA-1 identification schema into the XMP metadata. Only call this for a file that
    veraPDF says passes everything else: the identifier is a conformance claim."""
    from pikepdf.models.metadata import PdfMetadata

    PdfMetadata.register_xml_namespace(PDFUA_NS, "pdfuaid")
    with pikepdf.open(src) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["pdfuaid:part"] = "1"
        pdf.save(dst)
