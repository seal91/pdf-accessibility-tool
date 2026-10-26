r"""Re-measure the before/after numbers for the demo.

    .venv\Scripts\python demo\evidence\collect.py

Runs the batch tool over the sample publications (into demo/evidence/batch), validates every ORIGINAL with veraPDF,
and writes demo/evidence/results.json, then rebuilds results-at-a-glance.html.
"""
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from pdfa11y import verapdf as vp  # noqa: E402
from pdfa11y.audit import audit  # noqa: E402

out = ROOT / "demo" / "evidence" / "batch"
subprocess.run([sys.executable, "-m", "pdfa11y", "batch", str(ROOT / "samples"), "--out", str(out), "--jobs", "3"], check=True, cwd=ROOT)
batch = {r["file"]: r for r in json.loads((out / "_report" / "batch.json").read_text(encoding="utf-8"))}
rows = []
for f in sorted(glob.glob(str(ROOT / "samples" / "**" / "*.pdf"), recursive=True)):
    name = os.path.basename(f)
    before, a, r = vp.validate(f), audit(f), batch[name]
    after = r["verapdf"]
    norm = f.replace("\\", "/")
    rows.append({
        "file": name, "area": "International" if "/international/" in norm else "National",
        "source": "InDesign" if "/indesign/" in norm else "Word",
        "pages": r["pages"], "size_mb": a["size_mb"], "tagged_before": a["structure"] is not None,
        "before_checks": before["failed_checks"], "before_rules": len(before["failed_rules"]), "before_pass": before["compliant"],
        "after_checks": after["failed_checks"], "after_rules": len(after["failed_rules"]), "after_pass": after["compliant"],
        "after_rules_text": [(x["friendly"] or x["description"][:60], x["count"]) for x in after["failed_rules"]],
        "images_needing_alt": sum(i["count"] for i in r["issues"] if i["kind"] == "alt-text"),
        "seconds": r["seconds"], "action": r["action"], "stamped": r.get("stamped", False),
    })
(ROOT / "demo" / "evidence" / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
subprocess.run([sys.executable, str(ROOT / "demo" / "evidence" / "make_results.py")], check=True)
