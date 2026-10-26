# pdfa11y – local PDF accessibility remediation

Runs entirely on your own machine. No cloud services, no AI API calls. Original files are never modified.

## Demo kit

`demo/` holds everything for presenting the tool: a printable script with exact clicks and words, a real before/after
evidence page, draft image descriptions, and launchers that prepare a live library and a pre-processed safety-net library.
Start with `demo/README.md`.

## Packaged app (for colleagues)

```
.venv\Scripts\python packaging\build.py
```
builds `build\dist\PDF Accessibility Studio\` (browser version `PDF Accessibility Studio (browser).exe`, desktop app `PDF Accessibility Studio.exe`, command-line `pdfa11y.exe`, bundled
`tools\` with Java + veraPDF) and `build\PDF-Accessibility-Studio-<version>-win64.zip`. The build script self-tests the built
program before zipping. The folder needs no installation and no admin rights; `pdfa11y.exe selftest` checks a machine
(`selftest --deep some.pdf` also opens a real PDF in the app, headless). End-user instructions: `packaging\README-dist.txt`.

## Browser version (milestone 1)

```
.\.venv\Scripts\python -m pdfa11y web
```
Starts a small server on this computer only and opens your browser at an address that contains a one-time key (other programs and
websites cannot reach it). Add PDFs in the **Library**; each is processed automatically. Open one to write image descriptions
(Ctrl+Enter = save and next), check the title/language and the "things to confirm", and **Validate and download** the accessible
PDF. Work is kept in `%LOCALAPPDATA%\pdfa11y\web`, so you can quit and carry on later. Use **Quit** in the page to stop it.

Packaged, the browser version is a windowless launcher: starting it again re-opens the running copy, and it stops by itself ~10 minutes after the last tab closes (`--single-instance`, `--idle-minutes`).

Options: `--port`, `--data-dir`, `--no-browser`, `--max-upload-mb`, `--retention-days N` (auto-delete old documents),
`--workers N`. For a shared machine use `--host 0.0.0.0 --password <secret>` (it refuses to listen on the network without a
password; put it behind HTTPS). Code: `src/pdfa11y/web/` (Starlette API + a no-build HTML/CSS/JS interface in `static/`).
The **Pages** tab is the tag editor: the page with numbered, colour-coded blocks beside an accessible structure tree (arrow keys; Alt+Up/Down reorders; Ctrl+Z / Ctrl+Y undo and redo), with the same editing actions as the desktop app (change type, move, merge, mark as decoration, table headers, image alt text). The **Library** handles a backlog: add many files or a whole folder (folder paths are kept), filter by status (processing / need images / need review / ready / problems), search, sort, select many, and **Export** them in one go: each is validated, and a zip is built with the accessible PDFs (in their folders) plus a report (HTML + CSV). Documents that still need image descriptions are skipped and listed (or included as 'work in progress' if you ask). Unchanged documents reuse their earlier export. Reports can also be downloaded for the whole library. Still to come: an internal-server deployment with Microsoft sign-in.

## Run it in GitHub Codespaces (try it from any computer, nothing to install)

For testing only, with **public** publications. A Codespace is a small private Linux machine that GitHub runs for you; the
browser version runs inside it and you open it in your browser tab.

1. Keep this repository **private** on your own GitHub account (Settings → Danger zone → visibility, if in doubt).
2. On the repository page: **Code → Codespaces → Create codespace on main**. The first start takes a few minutes: it installs
   the Python packages, Java and veraPDF (`.devcontainer/devcontainer.json`, `deploy/install_tools.sh`).
3. In the Codespace terminal run:
   ```
   bash deploy/start_codespace.sh
   ```
   It prints a one-off password (or uses the Codespaces secret `PDFA11Y_PASSWORD` if you created one under
   Settings → Codespaces → Secrets).
4. Open the **PORTS** tab, find port 8765 and click the globe icon. GitHub asks you to sign in (the port is private to you),
   then the app asks for the password.
5. Upload PDFs (individually or a folder), work as usual, download the results. Codespaces stop by themselves after a period
   without use; your documents stay in the Codespace until you delete it (`--retention-days 7` also clears old ones).

Do not upload unpublished or confidential documents to a Codespace. Whether your workplace network allows GitHub Codespaces
is a question for IT.

## Development

```
python -m venv .venv
.\.venv\Scripts\pip install -e ".[desktop,dev]"      (Windows; the desktop app needs PySide6)
pip install -e ".[dev]"                               (Linux/macOS/Codespaces: the browser version and the command line)
python -m pdfa11y selftest
python -m pytest -q
```
The tests generate their own small PDFs, so they need none of the real publications. The veraPDF test is skipped when veraPDF
is not installed. On Linux, `bash deploy/install_tools.sh` installs Java and veraPDF into `tools/`. The same steps run on every
push in `.github/workflows/tests.yml`. A `Dockerfile` builds a server image (`docker run -p 8765:8765 -e PDFA11Y_PASSWORD=... -v data:/data ...`),
which is the starting point for an internal-server deployment.

Licence: no licence is granted; this is a private repository. Third-party components and their licences are listed in
`packaging/THIRD-PARTY-NOTICES.txt` (Java and veraPDF are downloaded at install time, not stored in the repository).

## Desktop app

```
.\.venv\Scripts\pdfa11y-gui.exe            (or:  .\.venv\Scripts\python -m pdfa11y gui  [file.pdf])
```

Open a PDF (or drag one in). The tool does the structural work; then you work through the **Images** queue (alt text or
"Decorative", keyboard-driven: Ctrl+Enter save & next, Ctrl+D decorative, Alt+arrows to move), review and correct the tags in the
**Pages** tab (see below), check the **Document** tab
(title, language, things the tool guessed) and press **Validate and save**. Progress is remembered per file, so a long
queue can be finished later. The original is never overwritten.

### Pages tab: the tag editor

Shows each page with its tag blocks outlined and numbered in reading order, beside the structure tree. Click a block (or a tree
entry) to: change its type (paragraph, heading 1-6, caption, quote), move it earlier/later in the reading order (Alt+Up/Down),
merge it with the next block, mark it as decoration, change a table cell to a header (or set a table's header row/column), or write
an image's alt text. **Fix heading levels** renumbers headings so they nest properly. Every edit can be undone (Ctrl+Z / Ctrl+Y)
and is remembered with the file. Edits never touch the original: they are applied to a copy when you save.

## Quick start (command line)

```
.\.venv\Scripts\python -m pdfa11y batch "C:\path\to\pdfs" --out "C:\path\to\accessible"
```

Then open `accessible\_report\batch.html`.

## Commands

| Command | What it does |
|---|---|
| `batch <folders/files> --out <dir>` | Whole folders: tags untagged PDFs, fixes tagged ones, mirrors the folder structure, writes a report (HTML + CSV + JSON) |
| `tag <pdfs> --out <dir>` | Auto-tag untagged PDFs, then apply the automatic fixes |
| `fix <pdfs> --out <dir>` | Automatic fixes only (title, language, tab order, table headers, artifacts, heading levels…) |
| `validate <pdfs> [--stamp --out <dir>]` | Independent PDF/UA-1 check with veraPDF. `--stamp` adds the PDF/UA identifier only if that is the sole failure |
| `audit <pdfs> [--out <dir>]` | Read-only structural audit (JSON) |

Useful `batch` options: `--jobs N` (parallel files), `--suffix _accessible`, `--skip-existing`, `--no-verify` (skip render comparison), `--no-validate` (skip veraPDF).

## What it does

* Tags untagged PDFs: headings (H1–H6), paragraphs, lists, contents pages, tables, figures, links; sets reading order; marks headers/footers/page numbers/decoration as artifacts; builds bookmarks from headings.
* Fixes tagged PDFs: title + "show title" flag, document language (Danish/English/Spanish detected offline), tab order, empty tags, table header scope, heading level gaps, untagged decorative graphics, RoleMap problems.
* Safety gates (a file failing any of them is **not** written): nothing left untagged, the pages render pixel-identical to the original, the file passes qpdf's structural check.

## Validation (veraPDF)

`tools/verapdf` (veraPDF 1.30.2) and `tools/jre` (Java 21) are bundled, so validation is offline. Set `PDFA11Y_TOOLS` to use another location.
Batch mode validates every output. The PDF/UA identifier is written **only** when veraPDF finds nothing else wrong, because the
identifier is a conformance claim. Files that still need alt text are therefore not stamped until the alt text is supplied.

## What still needs a person

The report lists these per file:

* **Alt text** for every meaningful image (never auto-approved).
* **Assumptions** the tool made (guessed title, changed language, promoted table header row, renumbered headings, tables it detected).
* Complex layouts (infographics), reading order on unusual pages, tables the tool could not detect.

"Passes PDF/UA-1 check" means veraPDF found no rule violations. It cannot judge alt-text quality, reading-order sense or table sense, so a person still confirms those.

## Layout

```
src/pdfa11y/
  audit.py     read-only audit
  extract.py   pdfminer extraction with content-stream operator provenance
  layout.py    reading order, paragraphs, headings, lists, TOC, artifacts
  tables.py    table detection (rules, bands, text alignment)
  autotag.py   rewrites content streams + builds the structure tree
  fix.py       automatic fixes on already-tagged files
  batch.py     folder processing and reports
  verify.py    safety gates
  verapdf.py   veraPDF integration (validate, conditional PDF/UA stamp)
  lang.py      offline da/en/es language detection
  figures.py   find figures, apply alt text / decorative decisions
  session.py   one file's working copy, decisions, save/resume, finalize
  editor.py    tag editor core: page blocks, replayable edits (undo/redo), save
  gui/         PySide6 desktop app (main.py window, pageview.py tag editor)
  web/         browser version: app.py (API + security), store.py (documents), exports.py (bulk export jobs), report.py, render.py, server.py, static/ (UI)
```
