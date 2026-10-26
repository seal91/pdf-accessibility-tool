// Router and start-up.
import { $, api, announce, focusHeading, h, showDialog } from './util.js';
import { libraryView } from './library.js';
import { reviewView } from './review.js';

let cleanup = null;
let info = { mode: 'local', can_quit: false };

async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const main = $('#main');
  const hash = location.hash || '#/';
  document.title = 'PDF Accessibility Studio';
  const m = hash.match(/^#\/doc\/([0-9a-f]{12})(?:\/(\w+))?$/);
  $('#nav-library').setAttribute('aria-current', m ? 'false' : 'page');
  if (m) cleanup = reviewView(main, m[1], m[2]);
  else cleanup = libraryView(main, info);
}

async function start() {
  try { info = await api('/info'); } catch (e) {
    $('#main').replaceChildren(h('h1', {}, 'Cannot reach the tool'), h('p', { role: 'alert' }, e.message));
    return;
  }
  const quit = $('#quit');
  if (info.can_quit) {
    quit.hidden = false;
    quit.addEventListener('click', async () => {
      const c = await showDialog({
        title: 'Quit PDF Accessibility Studio?',
        body: 'Your work is saved. You can start the tool again later and carry on.',
        buttons: [{ label: 'Cancel', value: 'cancel' }, { label: 'Quit', value: 'quit', primary: true }],
      });
      if (c !== 'quit') return;
      try { await api('/quit', { method: 'POST' }); } catch { /* the server is going away */ }
      document.body.replaceChildren(h('main', {}, h('h1', {}, 'The tool has stopped'), h('p', {}, 'You can close this browser tab.')));
    });
  }
  window.addEventListener('hashchange', route);
  route();
  // Heartbeat: while this page is open the tool stays running; after the last tab closes it stops by itself.
  setInterval(() => { fetch('/api/info', { credentials: 'same-origin' }).catch(() => {}); }, 30000);
}

start();
