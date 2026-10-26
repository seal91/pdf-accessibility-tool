# Demo kit

Everything for demonstrating the tool to Aske and the department. Real numbers, real publications, a script, and a safety net.

| File | What it is |
|---|---|
| `DEMO-SCRIPT.html` | The run sheet: checklist, 7 acts with exact clicks and words, timings (about 18 min), likely questions with straight answers, recoveries, private notes. Open in a browser; print if you like. |
| `evidence/results-at-a-glance.html` | One page with the real before/after numbers for all ten publications. Show it first. |
| `alt-text-drafts.txt` | Draft image descriptions (Danish for the live demo, Spanish for the Colombia photos). Check them against the images. |
| `reset-demo.bat` | Rebuilds both demo libraries (about a minute). Run before every demo. |
| `start-demo-live.bat` | Empty library on port 8765: drag the `samples` folder in live. |
| `start-demo-ready.bat` | Everything already processed on port 8766: your safety net, and the quickest way to show the editor. |
| `prepare_demo.py` | What the reset script runs. |
| `evidence/collect.py` | Re-measures the before/after numbers (about two minutes) and rebuilds the evidence page. |

## Quick start

1. Double-click `reset-demo.bat`, wait for "Demo libraries are ready".
2. Double-click `start-demo-live.bat` and `start-demo-ready.bat` (two black windows; leave them open).
3. Follow `DEMO-SCRIPT.html`.

## Notes

* The demo uses the ten publications in `samples\`. Your originals there are only read, never changed.
* The numbers on the evidence page are from this PC on the date shown on the page. If the tool changes, re-run `evidence\collect.py`.
* Nothing here needs a network connection.
* Showing it on a locked-down work laptop is a question for IT first. The easy route: run it on this PC and share the browser window in Teams.
