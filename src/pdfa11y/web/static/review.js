// Review one document: image descriptions, document details, validation result, download.
import { $, announce, api, focusHeading, h, plural, showDialog } from './util.js';
import { pagesPanel } from './pages.js';

const LANGS = [['da', 'Danish (da)'], ['en', 'English (en)'], ['es', 'Spanish (es)'], ['en-US', 'English, US (en-US)'],
  ['en-GB', 'English, UK (en-GB)'], ['da-DK', 'Danish, Denmark (da-DK)']];
const TAB_NAMES = ['images', 'pages', 'document', 'result'];

export function reviewView(main, id, startTab) {
  let dead = false;
  let timer = null;
  const S = { id, doc: null, summary: {}, figs: [], idx: 0, drafts: {}, title: '', lang: '', confirm: [],
              verdict: null, download: null, tab: TAB_NAMES.includes(startTab) ? startTab : 'images' };
  const panels = {};
  const tabButtons = {};
  let paint = {};
  let pagesCtl = null;

  main.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  load();
  return () => { dead = true; clearTimeout(timer); };

  // ------------------------------------------------------------------ loading
  async function load() {
    let d;
    try { d = await api(`/documents/${id}`); }
    catch (e) {
      main.replaceChildren(h('h1', {}, 'Document not found'), h('p', {}, e.message), h('a', { href: '#/' }, 'Back to the library'));
      focusHeading(main);
      return;
    }
    if (dead) return;
    S.doc = d.document; S.summary = d.summary || {};
    document.title = `${S.doc.name} · PDF Accessibility Studio`;
    if (S.doc.status === 'processing') { showProcessing(); timer = setTimeout(load, 1500); return; }
    if (S.doc.status !== 'ready') { showFailed(); return; }
    Object.assign(S, { figs: d.figures, title: d.title, lang: d.lang, confirm: d.confirm, verdict: d.verdict || null });
    build();
  }

  function showProcessing() {
    if (!main.querySelector('#processing')) {
      main.replaceChildren(
        h('h1', {}, S.doc.name),
        h('div', { id: 'processing', class: 'card' },
          h('p', {}, 'Making this document accessible: tagging the structure, fixing document settings and checking the result. ' +
            'Long documents can take a minute.'),
          h('progress', { 'aria-label': 'Processing' })),
        h('p', {}, h('a', { href: '#/' }, 'Back to the library (processing continues)')));
      focusHeading(main);
      announce('Processing has started.');
    }
  }

  function showFailed() {
    const skipped = S.doc.status === 'skipped';
    main.replaceChildren(
      h('h1', {}, S.doc.name),
      h('div', { class: 'card', role: 'alert' },
        h('p', {}, h('strong', {}, skipped ? 'This file was skipped.' : 'This file could not be processed.')),
        h('p', {}, S.doc.error || 'Unknown problem.')),
      h('div', { class: 'row' },
        h('a', { class: 'button', href: '#/' }, 'Back to the library'),
        !skipped && h('button', { type: 'button', onclick: async () => { await api(`/documents/${id}/retry`, { method: 'POST' }); load(); } }, 'Try again')));
    focusHeading(main);
  }

  // ------------------------------------------------------------------ page shell
  function build() {
    const sm = S.summary || {};
    const finalizeBtn = h('button', { type: 'button', class: 'primary', id: 'finalize', onclick: finalizeFlow }, 'Validate and download accessible PDF');
    main.replaceChildren(
      h('div', { class: 'page-head' },
        h('div', {},
          h('h1', {}, S.doc.name),
          h('p', { class: 'muted' }, `${plural(S.doc.pages || 0, 'page')} · ${sm.action || ''} · language ${S.lang || 'not set'}`)),
        h('div', { class: 'row' }, h('a', { class: 'button', href: '#/' }, 'Back to library'), finalizeBtn)),
      tabList(), ...TAB_NAMES.map((n) => (panels[n] = panelFor(n))));
    focusHeading(main);
    paint.tabs();
    activate(S.tab, false);
  }

  function tabList() {
    const list = h('div', { role: 'tablist', 'aria-label': 'Review sections' });
    TAB_NAMES.forEach((n) => {
      const b = h('button', { type: 'button', role: 'tab', id: `tab-${n}`, 'aria-controls': `panel-${n}`, onclick: () => activate(n, true) });
      b.addEventListener('keydown', (e) => {
        const i = TAB_NAMES.indexOf(n);
        let j = null;
        if (e.key === 'ArrowRight') j = (i + 1) % TAB_NAMES.length;
        if (e.key === 'ArrowLeft') j = (i + TAB_NAMES.length - 1) % TAB_NAMES.length;
        if (e.key === 'Home') j = 0;
        if (e.key === 'End') j = TAB_NAMES.length - 1;
        if (j !== null) { e.preventDefault(); activate(TAB_NAMES[j], true); }
      });
      tabButtons[n] = b;
      list.append(b);
    });
    paint.tabs = () => {
      const left = S.figs.filter((f) => f.status === 'todo').length;
      const toConfirm = S.confirm.filter((c) => !c.checked).length;
      tabButtons.images.textContent = S.figs.length ? (left ? `Images (${left} left)` : 'Images ✓') : 'Images';
      tabButtons.pages.textContent = 'Pages';
      tabButtons.document.textContent = toConfirm ? `Document (${toConfirm} to confirm)` : 'Document ✓';
      tabButtons.result.textContent = 'Result';
    };
    return list;
  }

  function panelFor(name) {
    const body = { images: imagesPanel, pages: pagesTab, document: documentPanel, result: resultPanel }[name]();
    return h('div', { role: 'tabpanel', id: `panel-${name}`, 'aria-labelledby': `tab-${name}`, tabindex: '0', hidden: true }, body);
  }

  function activate(name, focus) {
    S.tab = name;
    TAB_NAMES.forEach((n) => {
      const on = n === name;
      tabButtons[n].setAttribute('aria-selected', String(on));
      tabButtons[n].tabIndex = on ? 0 : -1;
      panels[n].hidden = !on;
    });
    history.replaceState(null, '', `#/doc/${id}/${name}`);
    if (focus) tabButtons[name].focus();
    if (name === 'images' && paint.images) paint.images();
    if (name === 'pages' && pagesCtl) pagesCtl.activate();
  }

  // ------------------------------------------------------------------ Images
  function imagesPanel() {
    if (!S.figs.length) return h('p', {}, 'No images in this document need a description. Check the Document and Result tabs, then download.');
    const url = (f, kind) => `/api/documents/${id}/figures/${f.key}/image?kind=${kind}`;
    const itemBtns = [];
    const list = h('ul', { class: 'img-list', 'aria-label': 'Images in this document' },
      S.figs.map((f, i) => {
        const label = h('span', {});
        const b = h('button', { type: 'button', onclick: () => select(i, true) },
          h('img', { src: url(f, 'thumb'), alt: '', loading: 'lazy', width: 52, height: 52 }), label);
        b.addEventListener('keydown', (e) => {
          if (e.key === 'ArrowDown' && i < S.figs.length - 1) { e.preventDefault(); itemBtns[i + 1].btn.focus(); select(i + 1, false); }
          if (e.key === 'ArrowUp' && i > 0) { e.preventDefault(); itemBtns[i - 1].btn.focus(); select(i - 1, false); }
        });
        itemBtns.push({ btn: b, label });
        return h('li', {}, b);
      }));

    const progress = h('p', { class: 'muted' });
    const title = h('h2', { id: 'img-title' });
    const crop = h('img', { alt: '' });
    const pageImg = h('img', { alt: '' });
    const near = h('p', { class: 'near' });
    const decoNote = h('p', { class: 'hint', hidden: true }, 'This image is marked decorative: screen readers will skip it. Clear it below to write a description instead.');
    const ta = h('textarea', { id: 'alt', rows: 4, 'aria-describedby': 'alt-hint alt-count' });
    const count = h('span', { id: 'alt-count', class: 'counter' });
    const prev = h('button', { type: 'button', onclick: () => select(S.idx - 1, true) }, '← Previous');
    const deco = h('button', { type: 'button', onclick: () => decide({ decorative: true }) }, 'Decorative');
    const clear = h('button', { type: 'button', onclick: () => decide({}) }, 'Clear');
    const where = h('button', { type: 'button', onclick: () => showOnPage(cur()) }, 'Show on page');
    const skip = h('button', { type: 'button', onclick: () => select(S.idx + 1, true) }, 'Skip →');
    const save = h('button', { type: 'button', class: 'primary', onclick: () => saveAlt() }, 'Save and next');
    const cur = () => S.figs[S.idx];

    ta.addEventListener('input', () => { S.drafts[cur().key] = ta.value; updateCount(); });
    ta.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'Enter') { e.preventDefault(); decide({ decorative: true }); }
      else if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); saveAlt(); }
    });

    function updateCount() {
      const n = ta.value.trim().length;
      count.textContent = `${n} characters` + (n > 250 ? ' · quite long: consider shortening' : '');
    }

    function itemText(f) {
      if (f.status === 'done') return ['✓', f.alt.length > 42 ? f.alt.slice(0, 42) + '…' : f.alt, 'st-done'];
      if (f.status === 'decorative') return ['⊘', 'Decorative', 'st-decorative'];
      return ['○', 'Needs alt text', 'st-todo'];
    }

    function paintItem(i) {
      const f = S.figs[i];
      const [sym, text, cls] = itemText(f);
      const { btn, label } = itemBtns[i];
      label.replaceChildren(h('strong', {}, `Page ${f.page}`), h('br'), h('span', { class: cls }, `${sym} `), text);
      btn.setAttribute('aria-current', String(i === S.idx));
    }

    function paintProgress() {
      const n = S.figs.length;
      const done = S.figs.filter((f) => f.status !== 'todo').length;
      progress.textContent = `${done} of ${n} images decided` + (done === n ? ' · all done' : '');
    }

    function select(i, focusText) {
      if (i < 0 || i >= S.figs.length) return;
      const prevI = S.idx;
      S.idx = i;
      paintItem(prevI); paintItem(i);
      const f = cur();
      title.textContent = `Page ${f.page} · image ${i + 1} of ${S.figs.length}`;
      crop.src = url(f, 'crop'); crop.alt = `Preview of the image on page ${f.page}`;
      pageImg.src = url(f, 'page'); pageImg.alt = `Page ${f.page} with the image outlined in red`;
      near.hidden = !f.near_text;
      near.textContent = f.near_text ? `Text near this image: ${f.near_text}` : '';
      ta.value = S.drafts[f.key] ?? (f.status === 'done' ? f.alt : '');
      ta.disabled = false;
      decoNote.hidden = f.status !== 'decorative';
      clear.hidden = f.status === 'todo';
      prev.disabled = i === 0;
      skip.disabled = i === S.figs.length - 1;
      updateCount();
      itemBtns[i].btn.scrollIntoView({ block: 'nearest' });
      if (focusText) ta.focus();
    }

    async function decide(body) {
      const f = cur();
      busy(true);
      try {
        const r = await api(`/documents/${id}/figures/${f.key}`, { method: 'PUT', json: body });
        f.status = r.status; f.alt = body.alt || '';
        delete S.drafts[f.key];
        paintItem(S.idx); paintProgress(); paint.tabs();
        const left = S.figs.filter((x) => x.status === 'todo').length;
        announce(body.decorative ? `Marked decorative. ${left} left.` : body.alt ? `Saved. ${left} left.` : `Cleared. ${left} left.`);
        if (body.alt || body.decorative) {
          const next = nextTodo(S.idx);
          if (next !== null) select(next, true);
          else {
            select(S.idx, false);
            announce('Every image now has a description or is marked decorative. Check the Document tab, then download.');
            save.focus();
          }
        } else select(S.idx, true);
      } catch (e) { announce(e.message); showDialog({ title: 'Could not save', body: e.message, buttons: [{ label: 'OK', value: 'ok', primary: true }] }); }
      finally { busy(false); }
    }

    function saveAlt() {
      const text = ta.value.trim();
      if (!text) { announce('Type a description first, or choose Decorative.'); ta.focus(); return; }
      decide({ alt: text });
    }

    function nextTodo(after) {
      const n = S.figs.length;
      for (let k = 1; k <= n; k++) { const i = (after + k) % n; if (S.figs[i].status === 'todo') return i; }
      return null;
    }

    function busy(on) { [prev, deco, clear, skip, save].forEach((b) => { b.disabled = on; }); if (!on) { prev.disabled = S.idx === 0; skip.disabled = S.idx === S.figs.length - 1; } }

    const main_ = h('section', { class: 'img-main', 'aria-labelledby': 'img-title' },
      title,
      h('div', { class: 'previews' },
        h('figure', {}, crop, h('figcaption', {}, 'The image')),
        h('figure', {}, pageImg, h('figcaption', {}, 'Where it sits on the page'))),
      near,
      h('p', { class: 'hint', id: 'alt-hint' }, 'Describe what the image shows and why it is there. Skip openers like “Image of…”. ' +
        'About 125 characters is a good target. For charts, give the main finding. If it is purely decorative, choose Decorative.'),
      decoNote,
      h('label', { for: 'alt' }, 'Alt text for this image'), ta, count,
      h('div', { class: 'actions' }, prev, deco, clear, where, h('span', { class: 'grow' }), skip, save),
      h('p', { class: 'counter' }, 'Shortcuts: Ctrl+Enter saves and moves on; Ctrl+Shift+Enter marks decorative.'));

    paint.images = () => { paintProgress(); S.figs.forEach((_, i) => paintItem(i)); select(S.idx, false); };
    const first = nextTodo(-1);
    S.idx = first === null ? 0 : first;
    setTimeout(() => { S.figs.forEach((_, i) => paintItem(i)); select(S.idx, false); paintProgress(); }, 0);

    return h('div', {}, progress, h('div', { class: 'images' }, list, main_));
  }

  // ------------------------------------------------------------------ Pages
  function pagesTab() {
    pagesCtl = pagesPanel({ id, S, onFigureChanged: () => { if (paint.images) paint.images(); paint.tabs(); } });
    return pagesCtl.root;
  }

  function showOnPage(fig) {
    activate('pages', true);
    const num = Number(fig.key.slice(1));
    if (pagesCtl && Number.isInteger(num)) pagesCtl.goto(fig.page, [num, 0]);
  }

  // ------------------------------------------------------------------ Document
  function documentPanel() {
    const titleIn = h('input', { type: 'text', id: 'doc-title', value: S.title, maxlength: 300 });
    titleIn.addEventListener('change', () => saveMeta({ title: titleIn.value }));
    const known = LANGS.some(([c]) => c === S.lang);
    const sel = h('select', { id: 'doc-lang' },
      LANGS.map(([c, l]) => h('option', { value: c, selected: c === S.lang }, l)),
      h('option', { value: '__other', selected: !known }, 'Other…'));
    const other = h('input', { type: 'text', id: 'doc-lang-other', value: known ? '' : S.lang, placeholder: 'e.g. sv or de-AT', hidden: known, 'aria-label': 'Other language code' });
    sel.addEventListener('change', () => {
      other.hidden = sel.value !== '__other';
      if (sel.value === '__other') other.focus(); else saveMeta({ lang: sel.value });
    });
    other.addEventListener('change', () => saveMeta({ lang: other.value }));

    const list = h('ul', { class: 'checklist' }, S.confirm.length ? S.confirm.map((c, i) => {
      const cb = h('input', { type: 'checkbox', id: `chk-${i}`, checked: c.checked });
      cb.addEventListener('change', () => { c.checked = cb.checked; saveMeta({ checked: { [c.text]: cb.checked } }); paint.tabs(); });
      return h('li', {}, cb, h('label', { for: `chk-${i}` }, c.text));
    }) : h('li', {}, 'Nothing to confirm.'));

    return h('div', {},
      h('div', { class: 'form-grid' },
        h('div', {}, h('label', { for: 'doc-title' }, 'Document title'), titleIn,
          h('p', { class: 'muted' }, 'Shown in the browser tab and read first by screen readers.')),
        h('div', {}, h('label', { for: 'doc-lang' }, 'Document language'), sel, other)),
      h('fieldset', {}, h('legend', {}, 'Things the tool guessed or wants you to check'),
        h('p', { class: 'muted' }, 'Tick each one when you have looked at it.'), list));
  }

  async function saveMeta(body) {
    try {
      await api(`/documents/${id}/meta`, { method: 'PUT', json: body });
      if ('lang' in body) S.lang = body.lang;
      announce('Saved.');
    } catch (e) { announce(e.message); showDialog({ title: 'Could not save', body: e.message, buttons: [{ label: 'OK', value: 'ok', primary: true }] }); }
  }

  // ------------------------------------------------------------------ Result
  function resultPanel() {
    const verdict = h('p', { class: 'verdict', role: 'status' });
    const details = h('div', {});
    const dl = h('p', {});
    const check = h('button', { type: 'button', onclick: runCheck }, 'Check again now');

    paint.result = () => {
      const v = S.verdict;
      verdict.className = 'verdict';
      if (!v) { verdict.textContent = 'Not checked yet.'; details.replaceChildren(); }
      else if (v.compliant) {
        verdict.classList.add('ok');
        verdict.textContent = '✓ Passes the automated PDF/UA-1 check (veraPDF)';
        details.replaceChildren(h('p', {}, 'The independent validator found no rule violations.'),
          h('p', { class: 'muted' }, 'It cannot judge whether descriptions are meaningful, whether the reading order makes sense, or whether tables ' +
            'are sensible: please look over the pages once before publishing.'));
      } else if (v.compliant === false) {
        verdict.classList.add('bad');
        verdict.textContent = '✗ Not there yet';
        const rules = v.failed_rules.filter((r) => !(String(r.clause) === '5' && Number(r.test) === 1 && v.failed_rules.length > 1));
        details.replaceChildren(h('p', {}, 'Still to do:'), h('ul', {}, rules.map((r) => h('li', {}, `${r.message}${r.count > 1 ? ` (${r.count}×)` : ''}`))));
      } else { verdict.textContent = 'Validation is not available on this installation.'; details.replaceChildren(); }
      dl.replaceChildren(S.download ? h('a', { class: 'button primary', href: S.download, download: '' }, 'Download accessible PDF again') : '');
    };
    paint.result();
    return h('div', {}, h('h2', {}, 'Result'), verdict, details, dl, h('div', { class: 'row' }, check));
  }

  async function runCheck() {
    const btn = $('#panel-result button');
    btn.disabled = true; btn.textContent = 'Checking…'; announce('Checking…');
    try { S.verdict = await api(`/documents/${id}/check`, { method: 'POST' }); paint.result(); announce(S.verdict.compliant ? 'Passes the check.' : 'Not there yet.'); }
    catch (e) { announce(e.message); showDialog({ title: 'The check failed', body: e.message, buttons: [{ label: 'OK', value: 'ok', primary: true }] }); }
    finally { btn.disabled = false; btn.textContent = 'Check again now'; }
  }

  // ------------------------------------------------------------------ Finish
  async function finalizeFlow() {
    const left = S.figs.filter((f) => f.status === 'todo').length;
    let allow = false;
    if (left) {
      const c = await showDialog({
        title: 'Some images still need a description',
        body: `${plural(left, 'image')} still ${left === 1 ? 'has' : 'have'} no description, so the file cannot pass the accessibility check yet. ` +
              'Download a work-in-progress copy anyway?',
        buttons: [{ label: 'Keep working', value: 'cancel' }, { label: 'Download work-in-progress', value: 'go', primary: true }],
      });
      if (c !== 'go') return;
      allow = true;
    }
    const btn = $('#finalize');
    btn.disabled = true; btn.textContent = 'Validating…'; announce('Validating and preparing the file…');
    try {
      const r = await api(`/documents/${id}/finalize`, { method: 'POST', json: { allow_incomplete: allow } });
      S.verdict = r; S.download = r.download;
      paint.result(); activate('result', false);
      const a = h('a', { href: r.download, download: '' }); document.body.append(a); a.click(); a.remove();
      announce(r.compliant ? 'Done. The file passes the check and is downloading.' : 'The work-in-progress file is downloading. It does not pass yet.');
      $('#panel-result .verdict').focus?.();
    } catch (e) {
      showDialog({ title: 'The file could not be produced', body: e.message, buttons: [{ label: 'OK', value: 'ok', primary: true }] });
    } finally { btn.disabled = false; btn.textContent = 'Validate and download accessible PDF'; }
  }
}
