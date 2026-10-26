// Library: add PDFs (files or a whole folder), see every document and its status, work in bulk, export.
import { announce, api, badge, docState, fmtDate, focusHeading, h, plural, showDialog } from './util.js';

const FILTERS = [['all', 'All'], ['processing', 'Processing'], ['images', 'Need images'], ['review', 'Need review'],
  ['ready', 'Ready to export'], ['problem', 'Problems']];

function category(d) {
  if (d.status === 'processing') return 'processing';
  if (d.status === 'failed' || d.status === 'skipped') return 'problem';
  if (d.figures_remaining > 0) return 'images';
  if ((d.confirm_total || 0) - (d.confirm_done || 0) > 0) return 'review';
  return 'ready';
}

export function libraryView(main, ctx) {
  const S = { docs: [], filter: 'all', query: '', sort: 'newest', selected: new Set(), sig: '', job: null };
  let timer = null;
  let dead = false;

  // ------------------------------------------------------------------ add PDFs
  const status = h('p', { class: 'muted', role: 'status' });
  const problemList = h('ul', { class: 'problems' });
  const problems = h('div', { role: 'alert' }, problemList);
  const fileInput = h('input', { type: 'file', id: 'file', accept: 'application/pdf,.pdf', multiple: true });
  const dirInput = h('input', { type: 'file', id: 'dir', webkitdirectory: true, directory: true, multiple: true });
  fileInput.addEventListener('change', () => { upload([...fileInput.files]); fileInput.value = ''; });
  dirInput.addEventListener('change', () => { upload([...dirInput.files]); dirInput.value = ''; });
  const drop = h('div', { class: 'dropzone' },
    h('p', {}, h('strong', {}, 'Drag PDF files here'), ' or choose them below.'),
    h('div', { class: 'row center' },
      h('div', {}, h('label', { for: 'file' }, 'Choose PDF files'), fileInput),
      h('div', {}, h('label', { for: 'dir' }, 'Or add a whole folder'), dirInput)));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('over');
    upload([...e.dataTransfer.files]);
  });

  async function upload(all) {
    const files = all.filter((f) => /\.pdf$/i.test(f.name));
    problemList.replaceChildren();
    const skipped = all.length - files.length;
    if (!files.length) { status.textContent = ''; problemList.append(h('li', {}, 'No PDF files were found in that selection.')); return; }
    let ok = 0;
    // send in small batches so progress is visible and one huge request never builds up
    const batches = [];
    let cur = [], size = 0;
    for (const f of files) {
      if (cur.length && (cur.length >= 5 || size + f.size > 60 * 1024 * 1024)) { batches.push(cur); cur = []; size = 0; }
      cur.push(f); size += f.size;
    }
    if (cur.length) batches.push(cur);
    let sent = 0;
    for (const batch of batches) {
      status.textContent = `Uploading ${sent + 1}–${sent + batch.length} of ${files.length}…`;
      const form = new FormData();
      batch.forEach((f) => { form.append('files', f, f.name); form.append('relpaths', f.webkitRelativePath || ''); });
      try {
        const r = await api('/documents', { method: 'POST', form });
        r.results.forEach((x) => { if (x.error) problemList.append(h('li', {}, `${x.name}: ${x.error}`)); else ok += 1; });
      } catch (e) {
        const res = e.data && e.data.results;
        if (res) res.forEach((x) => problemList.append(h('li', {}, `${x.name}: ${x.error}`)));
        else problemList.append(h('li', {}, e.message));
      }
      sent += batch.length;
      refresh();
    }
    status.textContent = ok ? `Added ${plural(ok, 'file')}${skipped ? ` (${plural(skipped, 'non-PDF file')} ignored)` : ''}. Processing has started.` : '';
    announce(status.textContent || 'Nothing was added.');
  }

  // ------------------------------------------------------------------ filters, bulk bar, export
  const search = h('input', { type: 'search', id: 'q', placeholder: 'Filter by name or folder' });
  search.addEventListener('input', () => { S.query = search.value.trim().toLowerCase(); renderList(); });
  const sortSel = h('select', { id: 'sort' },
    h('option', { value: 'newest' }, 'Newest first'), h('option', { value: 'name' }, 'Name'),
    h('option', { value: 'folder' }, 'Folder, then name'), h('option', { value: 'status' }, 'Status'));
  sortSel.addEventListener('change', () => { S.sort = sortSel.value; renderList(); });
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Show documents that' });
  const summary = h('p', { class: 'muted', id: 'summary' });
  const bulk = h('div', { class: 'bulk', role: 'region', 'aria-label': 'Actions for the selected documents', hidden: true });
  const listHolder = h('div');
  const exportCard = h('section', { class: 'card', 'aria-labelledby': 'exp-h', hidden: true });

  async function bulkDelete() {
    const ids = [...S.selected];
    if (!ids.length) return;
    const names = ids.map((i) => (S.docs.find((d) => d.id === i) || {}).name).filter(Boolean);
    const c = await showDialog({ title: `Delete ${plural(ids.length, 'document')}?`,
      body: [h('p', {}, 'They and all your work on them will be removed from this tool. The original files on your computer are not affected.'),
        h('ul', {}, names.slice(0, 6).map((n) => h('li', {}, n)), names.length > 6 ? h('li', {}, `…and ${names.length - 6} more`) : null)],
      buttons: [{ label: 'Cancel', value: 'cancel' }, { label: 'Delete', value: 'delete', primary: true }] });
    if (c !== 'delete') return;
    await api('/bulk/delete', { method: 'POST', json: { ids } });
    S.selected.clear();
    announce(`Deleted ${plural(ids.length, 'document')}.`);
    refresh(true);
  }

  async function bulkRetry() {
    const r = await api('/bulk/retry', { method: 'POST', json: { ids: [...S.selected] } });
    announce(`${plural(r.retried, 'document')} sent for processing again.`);
    refresh(true);
  }

  async function exportDialog(ids) {
    if (!ids.length) return;
    const incomplete = h('input', { type: 'checkbox', id: 'exp-inc' });
    const suffix = h('input', { type: 'checkbox', id: 'exp-suf' });
    const ready = ids.filter((i) => { const d = S.docs.find((x) => x.id === i); return d && d.status === 'ready' && d.figures_remaining === 0; }).length;
    const c = await showDialog({
      title: `Export ${plural(ids.length, 'document')}`,
      body: [h('p', {}, `${ready} of them ${ready === 1 ? 'has' : 'have'} every image described and will be validated and added to a zip file. ` +
        'The rest are listed as skipped, unless you include work in progress.'),
        h('div', { class: 'check' }, incomplete, h('label', { for: 'exp-inc' }, 'Also include documents that are not finished (marked “work in progress”)')),
        h('div', { class: 'check' }, suffix, h('label', { for: 'exp-suf' }, 'Add “(accessible)” to the file names'))],
      buttons: [{ label: 'Cancel', value: 'cancel' }, { label: 'Export', value: 'go', primary: true }],
    });
    if (c !== 'go') return;
    startExport(ids, incomplete.checked, suffix.checked);
  }

  async function startExport(ids, includeIncomplete, suffix) {
    let r;
    try { r = await api('/exports', { method: 'POST', json: { ids, include_incomplete: includeIncomplete, suffix } }); }
    catch (e) { announce(e.message); showDialog({ title: 'Could not start the export', body: e.message, buttons: [{ label: 'OK', value: 'ok', primary: true }] }); return; }
    S.job = r.job;
    exportCard.hidden = false;
    exportCard.replaceChildren(h('h2', { id: 'exp-h' }, 'Export'), h('p', { role: 'status' }, 'Starting…'), h('progress', { 'aria-label': 'Export progress' }));
    exportCard.scrollIntoView({ block: 'nearest' });
    pollExport(r.job);
  }

  async function pollExport(job) {
    if (dead || S.job !== job) return;
    let j;
    try { j = await api(`/exports/${job}`); } catch (e) { exportCard.replaceChildren(h('h2', { id: 'exp-h' }, 'Export'), h('p', { role: 'alert' }, e.message)); return; }
    if (j.state === 'running') {
      exportCard.replaceChildren(h('h2', { id: 'exp-h' }, 'Export'),
        h('p', { role: 'status' }, `Exporting ${Math.min(j.done + 1, j.total)} of ${j.total}…`),
        h('progress', { max: j.total, value: j.done, 'aria-label': 'Export progress' }));
      setTimeout(() => pollExport(job), 900);
      return;
    }
    if (j.state === 'failed') { exportCard.replaceChildren(h('h2', { id: 'exp-h' }, 'Export'), h('p', { role: 'alert' }, `The export failed: ${j.error}`)); return; }
    const count = (o) => j.items.filter((i) => i.outcome === o).length;
    const pass = j.items.filter((i) => i.outcome === 'exported' && i.passes).length;
    const text = `Done. ${count('exported')} exported (${pass} pass the check), ${count('skipped')} skipped, ${count('failed')} failed.`;
    exportCard.replaceChildren(
      h('h2', { id: 'exp-h' }, 'Export'),
      h('p', { role: 'status' }, text),
      count('exported') ? h('p', {}, h('a', { class: 'button primary', href: j.download, download: '' }, 'Download the zip file'),
        ' It contains the accessible PDFs (in their folders) and a report.') : null,
      h('div', { class: 'table-wrap' }, h('table', {},
        h('caption', {}, 'What happened to each document'),
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Document'), h('th', { scope: 'col' }, 'Result'), h('th', { scope: 'col' }, 'Details'))),
        h('tbody', {}, j.items.map((i) => h('tr', {}, h('th', { scope: 'row' }, i.name),
          h('td', {}, i.outcome === 'exported' ? '✓ Exported' : i.outcome === 'skipped' ? '○ Skipped' : '✕ Failed'), h('td', {}, i.message)))))));
    announce(text);
    refresh(true);
  }

  // ------------------------------------------------------------------ rendering
  function filtered() {
    let d = S.docs.filter((x) => (S.filter === 'all' || category(x) === S.filter) &&
      (!S.query || `${x.name} ${x.folder || ''}`.toLowerCase().includes(S.query)));
    const by = {
      newest: (a, b) => b.created - a.created,
      name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }),
      folder: (a, b) => (a.folder || '').localeCompare(b.folder || '') || a.name.localeCompare(b.name, undefined, { numeric: true }),
      status: (a, b) => FILTERS.findIndex((f) => f[0] === category(a)) - FILTERS.findIndex((f) => f[0] === category(b)),
    }[S.sort];
    return d.sort(by);
  }

  function renderChips() {
    const counts = Object.fromEntries(FILTERS.map(([k]) => [k, k === 'all' ? S.docs.length : S.docs.filter((d) => category(d) === k).length]));
    chips.replaceChildren(...FILTERS.map(([k, label]) => {
      const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(S.filter === k), onclick: () => { S.filter = k; renderList(); renderChips(); } },
        `${label} (${counts[k]})`);
      return b;
    }));
    const ready = counts.ready;
    summary.replaceChildren(...[S.docs.length
      ? `${plural(S.docs.length, 'document')}: ${counts.processing} processing, ${counts.images} need images, ${counts.review} need review, ${ready} ready, ${counts.problem} with problems. `
      : '', ready ? h('button', { type: 'button', class: 'link', onclick: () => exportDialog(S.docs.filter((d) => category(d) === 'ready').map((d) => d.id)) },
      `Export all ${ready} that are ready…`) : null].filter(Boolean));
  }

  function renderBulk() {
    const n = S.selected.size;
    bulk.hidden = n === 0;
    if (!n) return;
    const anyFailed = [...S.selected].some((i) => { const d = S.docs.find((x) => x.id === i); return d && d.status === 'failed'; });
    bulk.replaceChildren(...[h('strong', {}, `${plural(n, 'document')} selected`),
      h('button', { type: 'button', class: 'primary', onclick: () => exportDialog([...S.selected]) }, 'Export selected…'),
      anyFailed ? h('button', { type: 'button', onclick: bulkRetry }, 'Try failed ones again') : null,
      h('button', { type: 'button', class: 'danger', onclick: bulkDelete }, 'Delete selected'),
      h('button', { type: 'button', onclick: () => { S.selected.clear(); renderList(); } }, 'Clear selection')].filter(Boolean));
  }

  async function removeOne(d) {
    const c = await showDialog({ title: 'Delete this document?',
      body: `“${d.name}” and all your work on it will be removed from this tool. The original file on your computer is not affected.`,
      buttons: [{ label: 'Cancel', value: 'cancel' }, { label: 'Delete', value: 'delete', primary: true }] });
    if (c !== 'delete') return;
    await api(`/documents/${d.id}`, { method: 'DELETE' });
    S.selected.delete(d.id);
    announce(`Deleted ${d.name}.`);
    refresh(true);
  }

  function renderList() {
    const active = document.activeElement && document.activeElement.id;
    const docs = filtered();
    // a selection must only ever contain documents the person can see: drop anything the filter now hides
    const visible = new Set(docs.map((d) => d.id));
    [...S.selected].forEach((i) => { if (!visible.has(i)) S.selected.delete(i); });
    renderBulk();
    if (!S.docs.length) { listHolder.replaceChildren(h('p', { class: 'muted' }, 'No documents yet. Add a PDF above to begin.')); return; }
    if (!docs.length) { listHolder.replaceChildren(h('p', { class: 'muted' }, 'No documents match this filter.')); return; }
    const visibleIds = docs.map((d) => d.id);
    const nSel = visibleIds.filter((i) => S.selected.has(i)).length;
    const all = h('input', { type: 'checkbox', id: 'sel-all', 'aria-label': 'Select all shown documents', checked: nSel === visibleIds.length });
    all.indeterminate = nSel > 0 && nSel < visibleIds.length;
    all.addEventListener('change', () => { visibleIds.forEach((i) => (all.checked ? S.selected.add(i) : S.selected.delete(i))); renderList(); });
    const rows = docs.map((d) => {
      const cb = h('input', { type: 'checkbox', id: `sel-${d.id}`, 'aria-label': `Select ${d.name}`, checked: S.selected.has(d.id) });
      cb.addEventListener('change', () => { if (cb.checked) S.selected.add(d.id); else S.selected.delete(d.id); renderBulk(); const a = $all(); if (a) { /* keep header state in sync */ renderList(); } });
      const name = d.status === 'skipped' ? d.name : h('a', { href: `#/doc/${d.id}` }, d.name);
      return h('tr', {},
        h('td', {}, cb),
        h('th', { scope: 'row' }, name, d.folder ? h('div', { class: 'small muted' }, `Folder: ${d.folder}`) : null),
        h('td', { class: 'num' }, d.pages ?? '–'),
        h('td', {}, badge(docState(d))),
        h('td', { class: 'num' }, d.status === 'ready' ? `${d.figures_total - d.figures_remaining} / ${d.figures_total}` : '–'),
        h('td', {}, fmtDate(d.created)),
        h('td', {}, h('button', { type: 'button', class: 'danger', 'aria-label': `Delete ${d.name}`, onclick: () => removeOne(d) }, 'Delete')));
    });
    listHolder.replaceChildren(h('div', { class: 'table-wrap' }, h('table', {},
      h('caption', {}, `${docs.length} of ${S.docs.length} documents shown`),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, all), h('th', { scope: 'col' }, 'Document'), h('th', { scope: 'col' }, 'Pages'),
        h('th', { scope: 'col' }, 'Status'), h('th', { scope: 'col' }, 'Images described'), h('th', { scope: 'col' }, 'Added'), h('th', { scope: 'col' }, 'Actions'))),
      h('tbody', {}, rows))));
    if (active) { const el = document.getElementById(active); if (el) el.focus(); }
  }
  const $all = () => document.getElementById('sel-all');

  async function refresh(force) {
    clearTimeout(timer);
    if (dead) return;
    let docs;
    try { docs = (await api('/documents')).documents; }
    catch (e) { listHolder.replaceChildren(h('p', { role: 'alert' }, e.message)); return; }
    const sig = JSON.stringify(docs.map((d) => [d.id, d.status, d.figures_remaining, d.confirm_done, d.confirm_total, d.pages, d.export && d.export.compliant]));
    S.docs = docs;
    [...S.selected].forEach((i) => { if (!docs.some((d) => d.id === i)) S.selected.delete(i); });
    if (force || sig !== S.sig) { S.sig = sig; renderChips(); renderList(); }
    if (docs.some((d) => d.status === 'processing')) timer = setTimeout(refresh, 1800);
  }

  main.replaceChildren(
    h('h1', {}, 'Library'),
    h('p', { class: 'muted' }, ctx.mode === 'local'
      ? 'Your PDFs stay on this computer. Add one or many, or a whole folder; each is made accessible automatically, then you review it.'
      : 'Add one or many PDFs, or a whole folder; each is made accessible automatically, then you review it.'),
    h('section', { class: 'card', 'aria-labelledby': 'add-h' }, h('h2', { id: 'add-h' }, 'Add PDFs'), drop, status, problems),
    h('section', { 'aria-labelledby': 'docs-h' },
      h('h2', { id: 'docs-h' }, 'Documents'),
      summary,
      h('div', { class: 'filters' },
        h('div', {}, h('label', { for: 'q', class: 'sr-only' }, 'Filter documents'), search),
        h('div', {}, h('label', { for: 'sort', class: 'inline' }, 'Sort by '), sortSel),
        h('p', { class: 'small' }, 'Reports: ', h('a', { href: '/api/report?format=html', download: '' }, 'readable (HTML)'), ' · ',
          h('a', { href: '/api/report?format=csv', download: '' }, 'spreadsheet (CSV)'))),
      chips, bulk, listHolder),
    exportCard);
  focusHeading(main);
  refresh(true);
  return () => { dead = true; clearTimeout(timer); };
}
