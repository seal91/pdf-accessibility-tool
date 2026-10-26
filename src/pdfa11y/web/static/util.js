// Shared helpers. Everything that comes from a PDF or the server is inserted as TEXT (never as HTML).

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

/** Say something to screen-reader users (polite live region). */
export function announce(message) {
  const live = $('#live');
  live.textContent = '';
  setTimeout(() => { live.textContent = message; }, 60);
}

export async function api(path, { method = 'GET', json, form } = {}) {
  const opt = { method, credentials: 'same-origin', headers: {} };
  if (json !== undefined) { opt.body = JSON.stringify(json); opt.headers['Content-Type'] = 'application/json'; }
  if (form) opt.body = form;
  let r;
  try { r = await fetch('/api' + path, opt); }
  catch { throw new Error('Could not reach the tool. Is it still running?'); }
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || `The request failed (${r.status}).`);
    e.status = r.status; e.data = data;
    throw e;
  }
  return data;
}

/** Modal dialog. buttons: [{label, value, primary}]. Resolves to the chosen value ('cancel' if dismissed). */
export function showDialog({ title, body, buttons }) {
  return new Promise((resolve) => {
    const dlg = $('#dialog');
    $('#dialog-title').textContent = title;
    $('#dialog-body').replaceChildren(...[].concat(body).map((x) => (typeof x === 'string' ? h('p', {}, x) : x)));
    const opener = document.activeElement;
    let finished = false;
    // Resolve straight from the click / Escape key; the 'close' event is only a fallback (it is not reliable everywhere).
    const finish = (value) => {
      if (finished) return;
      finished = true;
      resolve(value);
      if (dlg.open) dlg.close(value);
      if (opener && opener.focus) opener.focus();
    };
    $('#dialog-actions').replaceChildren(...buttons.map((b) =>
      h('button', { type: 'button', value: b.value, class: b.primary ? 'primary' : '', onclick: () => finish(b.value) }, b.label)));
    dlg.returnValue = 'cancel';
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish('cancel'); }, { once: true });
    dlg.addEventListener('close', () => finish(dlg.returnValue || 'cancel'), { once: true });
    dlg.showModal();
  });
}

const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
export const fmtDate = (ts) => (ts ? dateFmt.format(new Date(ts * 1000)) : '');
export const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

/** One place that turns a document's data into a status badge: symbol + words, never colour alone. */
export function docState(d) {
  if (d.status === 'processing') return { cls: 'busy', symbol: '⏳', label: 'Processing…' };
  if (d.status === 'failed') return { cls: 'bad', symbol: '✕', label: 'Failed' };
  if (d.status === 'skipped') return { cls: 'bad', symbol: '✕', label: 'Skipped (scanned)' };
  if (d.figures_remaining > 0) return { cls: 'warn', symbol: '○', label: `Needs ${plural(d.figures_remaining, 'image description')}` };
  const toConfirm = (d.confirm_total || 0) - (d.confirm_done || 0);
  if (toConfirm > 0) return { cls: 'warn', symbol: '○', label: `Needs review (${toConfirm} to confirm)` };
  if (d.export && d.export.compliant) return { cls: 'ok', symbol: '✓', label: 'Exported · passes check' };
  return { cls: 'ok', symbol: '✓', label: 'Ready to export' };
}

export function badge(state) {
  return h('span', { class: `badge ${state.cls}` }, h('span', { 'aria-hidden': 'true' }, state.symbol), state.label);
}

export function focusHeading(root) {
  const el = root.querySelector('h1');
  if (el) { el.setAttribute('tabindex', '-1'); el.focus({ preventScroll: false }); }
}
