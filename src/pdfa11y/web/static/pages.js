// Pages tab: the page with its tag blocks outlined, the structure tree, and editing actions.
import { announce, api, h, showDialog } from './util.js';

const ROLE_NAMES = {
  P: 'Paragraph', H: 'Heading', H1: 'Heading 1', H2: 'Heading 2', H3: 'Heading 3', H4: 'Heading 4', H5: 'Heading 5', H6: 'Heading 6',
  Caption: 'Caption', BlockQuote: 'Quote', LBody: 'List item text', Lbl: 'List label', L: 'List', LI: 'List item',
  Table: 'Table', TR: 'Table row', TD: 'Table cell', TH: 'Table header cell', THead: 'Table head', TBody: 'Table body',
  Figure: 'Image', Link: 'Link', Note: 'Note', TOC: 'Contents', TOCI: 'Contents entry', Document: 'Document', Sect: 'Section',
  Span: 'Text span', Part: 'Part', Art: 'Article', Div: 'Group', Title: 'Title', Formula: 'Formula', Form: 'Form field',
};
const TYPE_CHOICES = [['P', 'Paragraph'], ['H1', 'Heading 1'], ['H2', 'Heading 2'], ['H3', 'Heading 3'], ['H4', 'Heading 4'],
  ['H5', 'Heading 5'], ['H6', 'Heading 6'], ['Caption', 'Caption'], ['BlockQuote', 'Quote']];
const CELL_CHOICES = [['td', 'Data cell'], ['th_col', 'Header for its column'], ['th_row', 'Header for its row']];
const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5];
const PX_PER_PT = 96 / 72;

const roleName = (r) => ROLE_NAMES[r] || r;
const refKey = (r) => `${r[0]}-${r[1]}`;
const colourClass = (role) => (/^H[1-6]?$/.test(role) ? 'r-head' : role === 'P' ? 'r-text' : role === 'LBody' || role === 'Lbl' ? 'r-list'
  : role === 'TD' || role === 'TH' ? 'r-table' : role === 'Figure' ? 'r-fig' : 'r-other');
const shortLabel = (b) => `${b.order + 1}${/^H[1-6]$/.test(b.role) ? ' ' + b.role : ''}`;

export function pagesPanel(ctx) {
  const { id, S } = ctx;
  const st = { page: 1, pages: S.doc.pages || 1, data: null, sel: null, info: null, zoom: 'fit', collapsed: new Set(),
               selToken: 0, flat: [], itemEls: [], canUndo: false, canRedo: false, started: false, pendingSelect: null };

  // ------------------------------------------------------------------ elements
  const prev = h('button', { type: 'button', onclick: () => go(st.page - 1) }, '◀ Previous page');
  const next = h('button', { type: 'button', onclick: () => go(st.page + 1) }, 'Next page ▶');
  const num = h('input', { type: 'number', id: 'pg-num', min: 1, max: st.pages, value: 1, 'aria-label': 'Page number' });
  num.addEventListener('change', () => go(parseInt(num.value, 10) || 1));
  const symbolButton = (symbol, name, fn) => h('button', { type: 'button', onclick: fn },
    h('span', { 'aria-hidden': 'true' }, symbol), h('span', { class: 'sr-only' }, name));
  const zoomOut = symbolButton('−', 'Zoom out', () => zoomBy(-1));
  const zoomIn = symbolButton('+', 'Zoom in', () => zoomBy(1));
  const zoomLabel = h('span', { class: 'muted', 'aria-live': 'off' }, 'Fit');
  const fit = h('button', { type: 'button', onclick: () => { st.zoom = 'fit'; applyZoom(); } }, 'Fit width');
  const undo = h('button', { type: 'button', onclick: () => history('undo') }, 'Undo');
  const redo = h('button', { type: 'button', onclick: () => history('redo') }, 'Redo');
  const fixHead = h('button', { type: 'button', title: 'Renumber headings so they nest properly and start at Heading 1',
    onclick: () => edit('renumber_headings', [], 'Heading levels renumbered.') }, 'Fix heading levels');

  const toolbar = h('div', { class: 'toolbar', role: 'toolbar', 'aria-label': 'Page tools' },
    prev, h('label', { for: 'pg-num', class: 'inline' }, 'Page'), num, h('span', { id: 'pg-of', class: 'muted' }, `of ${st.pages}`), next,
    h('span', { class: 'sep', 'aria-hidden': 'true' }), zoomOut, zoomLabel, zoomIn, fit,
    h('span', { class: 'sep', 'aria-hidden': 'true' }), undo, redo, fixHead);

  const tree = h('ul', { role: 'tree', id: 'struct-tree', 'aria-labelledby': 'tree-h', class: 'struct-tree' });
  const props = h('div', { id: 'props' });
  const img = h('img', { alt: '', class: 'page-img', draggable: 'false' });
  const overlay = h('div', { class: 'overlay', 'aria-hidden': 'true' });
  const pagebox = h('div', { class: 'pagebox' }, img, overlay);
  const viewer = h('div', { class: 'viewer', tabindex: '0', role: 'region', 'aria-label': 'Page preview with numbered blocks' }, pagebox);
  const status = h('p', { class: 'muted', role: 'status', id: 'pg-status' });

  const root = h('div', { class: 'pages-wrap' },
    toolbar,
    h('p', { class: 'muted' }, 'Pick a block in the list or click it on the page. Alt+↑ / Alt+↓ moves the selected block in the reading order; Ctrl+Z undoes.'),
    status,
    h('div', { class: 'pages-grid' },
      h('div', { class: 'side' },
        h('section', { class: 'card', 'aria-labelledby': 'tree-h' }, h('h2', { id: 'tree-h' }, 'Structure on this page'), tree),
        h('section', { class: 'card', 'aria-labelledby': 'props-h' }, h('h2', { id: 'props-h' }, 'Selected block'), props)),
      viewer));

  // keyboard: undo / redo anywhere in the panel (except while typing), Alt+arrows to reorder
  root.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    if (e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); history('undo'); }
    else if (((e.ctrlKey && e.key.toLowerCase() === 'y') || (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'z')) && !typing) { e.preventDefault(); history('redo'); }
    else if (e.altKey && e.key === 'ArrowUp' && !typing) { e.preventDefault(); edit('move', [st.sel, -1], 'Moved up.'); }
    else if (e.altKey && e.key === 'ArrowDown' && !typing) { e.preventDefault(); edit('move', [st.sel, 1], 'Moved down.'); }
  });
  window.addEventListener('resize', () => { if (st.zoom === 'fit' && st.started) applyZoom(); });

  // ------------------------------------------------------------------ loading
  async function load(page, { select, focusTree } = {}) {
    st.page = Math.max(1, Math.min(st.pages, page));
    num.value = st.page;
    prev.disabled = st.page <= 1; next.disabled = st.page >= st.pages;
    status.textContent = 'Loading page…';
    let d;
    try { d = await api(`/documents/${id}/pages/${st.page}`); }
    catch (e) { status.textContent = e.message; announce(e.message); return; }
    st.data = d;
    st.canUndo = d.can_undo; st.canRedo = d.can_redo;
    undo.disabled = !st.canUndo; redo.disabled = !st.canRedo;
    if (img.dataset.page !== String(st.page)) {
      img.dataset.page = String(st.page);
      img.src = `/api/documents/${id}/pages/${st.page}/image?scale=2`;
      img.alt = `Page ${st.page}`;
    }
    const keep = select !== undefined ? select : st.sel;
    st.sel = keep && d.blocks.concat(flattenNodes(d.tree)).some((b) => refKey(b.ref) === refKey(keep)) ? keep : null;
    drawOverlay();
    drawTree();
    applyZoom();
    status.textContent = d.blocks.length ? `${d.blocks.length} blocks on page ${st.page}.` : `Page ${st.page} has no tagged content.`;
    await select_(st.sel, { scroll: true, focusTree });
  }

  const flattenNodes = (nodes) => nodes.flatMap((n) => [n, ...flattenNodes(n.children)]);

  function go(p) { st.sel = null; load(p); }

  // ------------------------------------------------------------------ drawing
  function applyZoom() {
    if (!st.data) return;
    const wPx = st.zoom === 'fit' ? Math.max(260, viewer.clientWidth - 24) : st.data.width * PX_PER_PT * st.zoom;
    pagebox.style.width = `${Math.round(wPx)}px`;
    zoomLabel.textContent = st.zoom === 'fit' ? 'Fit' : `${Math.round(st.zoom * 100)}%`;
  }

  function zoomBy(dir) {
    const cur = st.zoom === 'fit' ? (viewer.clientWidth - 24) / (st.data.width * PX_PER_PT) : st.zoom;
    let i = ZOOMS.findIndex((z) => z >= cur - 0.001);
    if (i < 0) i = ZOOMS.length - 1;
    if (dir > 0 && ZOOMS[i] <= cur + 0.001) i = Math.min(ZOOMS.length - 1, i + 1);
    if (dir < 0) i = Math.max(0, (ZOOMS[i] >= cur - 0.001 ? i - 1 : i));
    st.zoom = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, i))];
    applyZoom();
  }

  function drawOverlay() {
    overlay.replaceChildren();
    const d = st.data;
    d.blocks.forEach((b) => {
      if (!b.bbox) return;
      const [x0, y0, x1, y1] = b.bbox;
      const left = Math.max(0, (x0 - d.left) / d.width * 100);
      const top = Math.max(0, (d.top - y1) / d.height * 100);
      const w = Math.min(100 - left, (x1 - x0) / d.width * 100);
      const hh = Math.min(100 - top, (y1 - y0) / d.height * 100);
      const el = h('button', { type: 'button', tabindex: '-1', class: `blk ${colourClass(b.role)}`, 'data-ref': refKey(b.ref),
        title: `${roleName(b.role)}: ${b.text.slice(0, 100)}`, onclick: () => select_(b.ref, { scroll: true, fromPage: true }) },
        h('span', { class: 'bdg', 'aria-hidden': 'true' }, shortLabel(b)));
      Object.assign(el.style, { left: `${left}%`, top: `${top}%`, width: `${w}%`, height: `${hh}%` });
      overlay.append(el);
    });
  }

  function drawTree() {
    const blocks = new Map(st.data.blocks.map((b) => [refKey(b.ref), b]));
    st.flat = [];
    const walk = (nodes, level, hidden) => nodes.forEach((n, i) => {
      const k = refKey(n.ref);
      st.flat.push({ n, level, k, hidden, posinset: i + 1, setsize: nodes.length, block: blocks.get(k) });
      walk(n.children, level + 1, hidden || st.collapsed.has(k));
    });
    walk(st.data.tree, 1, false);
    st.itemEls = [];
    tree.replaceChildren();
    if (!st.flat.length) { tree.append(h('li', { class: 'muted', role: 'none' }, 'Nothing is tagged on this page.')); return; }
    st.flat.forEach((f, idx) => {
      const sel = st.sel && refKey(st.sel) === f.k;
      const hasKids = f.n.children.length > 0;
      const li = h('li', { role: 'treeitem', 'aria-level': f.level, 'aria-posinset': f.posinset, 'aria-setsize': f.setsize, 'data-ref': f.k,
        'aria-selected': String(!!sel), 'aria-expanded': hasKids ? String(!st.collapsed.has(f.k)) : false, tabindex: '-1',
        class: `ti ${f.block ? colourClass(f.n.role) : 'r-container'}`, hidden: f.hidden },
        h('strong', {}, roleName(f.n.role)), f.n.text ? h('span', { class: 'ti-text' }, ` ${f.n.text}`) : '');
      li.style.paddingLeft = `${8 + (f.level - 1) * 14}px`;
      li.addEventListener('click', (e) => { e.stopPropagation(); select_(f.n.ref, { scroll: true, fromTree: true }); li.focus(); });
      li.addEventListener('keydown', (e) => treeKey(e, idx));
      st.itemEls.push(li);
      tree.append(li);
    });
    rove(st.sel ? st.flat.findIndex((f) => refKey(st.sel) === f.k) : 0);
  }

  function rove(idx) {
    const i = idx < 0 ? 0 : idx;
    st.itemEls.forEach((el, j) => { el.tabIndex = j === i ? 0 : -1; });
  }

  function visibleIdx() { return st.flat.map((f, i) => (f.hidden ? -1 : i)).filter((i) => i >= 0); }

  function treeKey(e, idx) {
    const vis = visibleIdx();
    const pos = vis.indexOf(idx);
    const f = st.flat[idx];
    const focusTo = (i) => { rove(i); st.itemEls[i].focus(); select_(st.flat[i].n.ref, { scroll: true, fromTree: true, debounce: true }); };
    if (e.altKey) return;                                         // Alt+arrows are handled for the whole panel
    if (e.key === 'ArrowDown' && pos < vis.length - 1) { e.preventDefault(); focusTo(vis[pos + 1]); }
    else if (e.key === 'ArrowUp' && pos > 0) { e.preventDefault(); focusTo(vis[pos - 1]); }
    else if (e.key === 'Home') { e.preventDefault(); focusTo(vis[0]); }
    else if (e.key === 'End') { e.preventDefault(); focusTo(vis[vis.length - 1]); }
    else if (e.key === 'ArrowRight' && f.n.children.length) {
      e.preventDefault();
      if (st.collapsed.has(f.k)) { st.collapsed.delete(f.k); drawTree(); st.itemEls[idx].focus(); }
      else if (idx + 1 < st.flat.length) focusTo(idx + 1);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (f.n.children.length && !st.collapsed.has(f.k)) { st.collapsed.add(f.k); drawTree(); st.itemEls[idx].focus(); }
      else { for (let j = idx - 1; j >= 0; j--) if (st.flat[j].level < f.level) { focusTo(j); break; } }
    } else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select_(f.n.ref, { scroll: true, fromTree: true }); }
  }

  // ------------------------------------------------------------------ selection
  let debounceTimer = null;
  async function select_(ref, opts = {}) {
    st.sel = ref ? [ref[0], ref[1]] : null;
    const key = st.sel ? refKey(st.sel) : null;
    st.itemEls.forEach((el) => el.setAttribute('aria-selected', String(el.dataset.ref === key)));
    overlay.querySelectorAll('.blk').forEach((el) => el.classList.toggle('sel', el.dataset.ref === key));
    if (opts.scroll && key) {
      const box = overlay.querySelector(`.blk[data-ref="${key}"]`);
      if (box) box.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const it = st.itemEls.find((el) => el.dataset.ref === key);
      if (it && !opts.fromTree) { rove(st.itemEls.indexOf(it)); it.scrollIntoView({ block: 'nearest' }); if (opts.focusTree) it.focus(); }
    }
    const token = ++st.selToken;
    if (!st.sel) { st.info = null; renderProps(); return; }
    const run = async () => {
      let info = null;
      try { info = await api(`/documents/${id}/element?num=${st.sel[0]}&gen=${st.sel[1]}`); } catch { /* element vanished */ }
      if (token !== st.selToken) return;
      st.info = info;
      renderProps();
    };
    if (opts.debounce) { clearTimeout(debounceTimer); debounceTimer = setTimeout(run, 160); } else await run();
  }

  // ------------------------------------------------------------------ properties
  function renderProps() {
    props.replaceChildren();
    const info = st.info;
    if (!st.sel || !info) {
      props.append(h('p', { class: 'muted' }, 'Nothing selected. Pick a block in the list or on the page.'));
      return;
    }
    const blk = st.data.blocks.find((b) => refKey(b.ref) === refKey(st.sel));
    const msg = h('p', { class: 'muted', role: 'status', id: 'props-msg' });
    props.append(h('p', {}, h('strong', {}, roleName(info.role)), blk && blk.text ? ` · ${blk.text.slice(0, 140)}` : ''));

    if (info.is_text) {
      const sel = h('select', { id: 'sel-type' }, TYPE_CHOICES.map(([v, l]) => h('option', { value: v }, l)));
      if (!TYPE_CHOICES.some(([v]) => v === info.role)) sel.prepend(h('option', { value: info.role }, roleName(info.role)));
      sel.value = info.role;
      sel.addEventListener('change', () => edit('set_role', [st.sel, sel.value],
        /^H/.test(sel.value) ? 'Type changed. Check the heading levels (Fix heading levels).' : 'Type changed.'));
      props.append(h('div', { class: 'field' }, h('label', { for: 'sel-type' }, 'Type'), sel));
    }
    if (info.cell) {
      const sel = h('select', { id: 'sel-cell' }, CELL_CHOICES.map(([v, l]) => h('option', { value: v }, l)));
      sel.value = info.cell;
      sel.addEventListener('change', () => edit('cell', [st.sel, sel.value], 'Cell type changed.'));
      props.append(h('div', { class: 'field' }, h('label', { for: 'sel-cell' }, 'Cell type'), sel));
    }
    if (info.table) {
      const row = h('input', { type: 'checkbox', id: 'hdr-row', checked: info.table.header_row });
      const col = h('input', { type: 'checkbox', id: 'hdr-col', checked: info.table.header_col });
      const send = () => edit('table_headers', [st.sel, row.checked, col.checked], 'Table headers updated.');
      row.addEventListener('change', send); col.addEventListener('change', send);
      props.append(h('fieldset', {}, h('legend', {}, 'Table headers'),
        h('div', { class: 'check' }, row, h('label', { for: 'hdr-row' }, 'First row is a header')),
        h('div', { class: 'check' }, col, h('label', { for: 'hdr-col' }, 'First column is a header'))));
    }
    if (info.figure_key) props.append(figureEditor(info.figure_key, msg));

    props.append(h('div', { class: 'actions' },
      h('button', { type: 'button', disabled: !info.can_up, onclick: () => edit('move', [st.sel, -1], 'Moved up.') }, 'Move up'),
      h('button', { type: 'button', disabled: !info.can_down, onclick: () => edit('move', [st.sel, 1], 'Moved down.') }, 'Move down'),
      info.is_text || info.role === 'LBody' ? h('button', { type: 'button', disabled: !info.can_merge, title: 'Join this block and the next into one',
        onclick: () => edit('merge_next', [st.sel], 'Merged with the next block.') }, 'Merge with next') : null,
      info.figure_key ? null : h('button', { type: 'button', class: 'danger', title: 'Screen readers will skip this block. You can undo.',
        onclick: () => hide() }, 'Mark as decoration')));
    props.append(msg);
  }

  function figureEditor(key, msg) {
    const fig = S.figs.find((f) => f.key === key);
    const ta = h('textarea', { id: 'pg-alt', rows: 3 });
    ta.value = fig && fig.status === 'done' ? fig.alt : '';
    const save = async (body) => {
      try {
        const r = await api(`/documents/${id}/figures/${key}`, { method: 'PUT', json: body });
        if (fig) { fig.status = r.status; fig.alt = body.alt || ''; }
        ctx.onFigureChanged(key);
        msg.textContent = body.decorative ? 'Marked decorative.' : body.alt ? 'Alt text saved.' : 'Cleared.';
        announce(msg.textContent);
      } catch (e) { msg.textContent = e.message; announce(e.message); }
    };
    return h('div', { class: 'field' },
      h('label', { for: 'pg-alt' }, 'Alt text for this image'), ta,
      fig && fig.status === 'decorative' ? h('p', { class: 'hint' }, 'Currently marked decorative.') : null,
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'primary', onclick: () => { const t = ta.value.trim(); if (!t) { msg.textContent = 'Type a description first, or choose Decorative.'; ta.focus(); } else save({ alt: t }); } }, 'Save alt text'),
        h('button', { type: 'button', onclick: () => { ta.value = ''; save({ decorative: true }); } }, 'Decorative'),
        h('button', { type: 'button', onclick: () => { ta.value = ''; save({}); } }, 'Clear')));
  }

  async function hide() {
    const isGroup = !st.data.blocks.some((b) => refKey(b.ref) === refKey(st.sel));
    if (isGroup) {
      const c = await showDialog({ title: 'Hide this whole group?', body: 'This group contains several blocks. Screen readers will skip all of them. You can undo this.',
        buttons: [{ label: 'Cancel', value: 'cancel' }, { label: 'Mark as decoration', value: 'go', primary: true }] });
      if (c !== 'go') return;
    }
    edit('artifact', [st.sel], 'Marked as decoration. Press Undo to bring it back.');
  }

  // ------------------------------------------------------------------ edits
  function setMsg(text) {
    const m = props.querySelector('#props-msg');
    if (m) m.textContent = text;
    status.textContent = text;
    announce(text);
  }

  async function edit(op, args, okMsg) {
    if (op !== 'renumber_headings' && !st.sel) { setMsg('Select a block first.'); return; }
    try {
      await api(`/documents/${id}/edits`, { method: 'POST', json: { op, args } });
    } catch (e) { setMsg(e.message); return; }
    const key = st.sel ? refKey(st.sel) : null;
    const hadFocus = key && st.itemEls.some((el) => el === document.activeElement);
    await load(st.page, { focusTree: hadFocus });
    setMsg(okMsg);
  }

  async function history(which) {
    let r;
    try { r = await api(`/documents/${id}/history/${which}`, { method: 'POST' }); } catch (e) { setMsg(e.message); return; }
    await load(st.page);
    setMsg(r.changed ? (which === 'undo' ? 'Undone.' : 'Redone.') : `Nothing to ${which}.`);
  }

  // ------------------------------------------------------------------ public
  return {
    root,
    /** Called whenever the tab becomes visible. */
    activate() {
      if (!st.started) { st.started = true; load(1); }
      else if (st.data) applyZoom();
    },
    /** Jump to a page and select an element (used by "Show on page" in the Images tab). */
    goto(page, ref) {
      st.started = true;
      st.sel = ref;
      load(page, { select: ref });
    },
  };
}
