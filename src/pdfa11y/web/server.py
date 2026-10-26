"""Start the web app.

Local mode (default): listens on this computer only, protected by a one-time key in the address, opens your browser.
Server mode (--host 0.0.0.0 or --password): for a shared machine. Always requires a password or key.

For the packaged launcher: `single_instance` re-opens the running copy instead of starting a second one, and
`idle_minutes` makes the tool stop by itself a while after the last browser tab was closed (never while work is running).
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .store import DocStore, default_data_dir


def _say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001  (the windowed launcher has no console)
        pass


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- one running copy at a time

def _instance_file(root: Path) -> Path:
    return root / "server.json"


def existing_instance(root: Path) -> str | None:
    """The address of an already-running copy for this data folder, or None (stale records are removed)."""
    f = _instance_file(root)
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", f"/?token={token}")
        conn.getresponse().read()
        conn.close()
        return f"http://127.0.0.1:{port}/?token={token}"
    except OSError:
        try:
            f.unlink()
        except OSError:
            pass
        return None


# --------------------------------------------------------------------------- start / stop helpers

def _idle_watch(app, server, idle_minutes: float) -> None:
    """Stop the server when nobody has talked to it for a while and no work is running."""
    limit = idle_minutes * 60
    while not server.should_exit:
        time.sleep(min(5.0, max(0.5, limit / 4)))
        st = app.state
        busy = any(m.get("status") == "processing" for m in st.store.list()) or st.exports.is_busy()
        if not busy and time.time() - st.last_seen > limit:
            _say(f"No browser has been connected for {idle_minutes:g} minutes: stopping. Your work is saved.")
            server.should_exit = True
            return


def serve_in_thread(data_dir: str | Path, token: str = "selftest", workers: int = 1) -> dict:
    """Start a local server in a background thread (used by the self-test). Returns handles to stop it."""
    port = _free_port("127.0.0.1")
    store = DocStore(Path(data_dir), workers=workers)
    app = create_app(store, mode="local", auth_mode="token", token=token,
                     allowed_hosts={f"127.0.0.1:{port}", f"localhost:{port}"}, allow_quit=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", http="h11", ws="none"))
    app.state.server = server
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    return {"port": port, "token": token, "server": server, "thread": t, "store": store, "app": app}


def stop_thread_server(h: dict) -> None:
    h["server"].should_exit = True
    h["thread"].join(15)
    h["app"].state.exports.shutdown()
    h["store"].shutdown()


def run(host: str = "127.0.0.1", port: int = 0, data_dir: str | Path | None = None, token: str | None = None,
        password: str | None = None, open_browser: bool = True, max_upload_mb: int = 150,
        retention_days: float | None = None, workers: int = 2, no_auth: bool = False,
        idle_minutes: float | None = None, single_instance: bool = False) -> int:
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not password and not token:
        _say("Refusing to listen on the network without a password. Add  --password <something>.")
        return 2
    if no_auth and not loopback:
        _say("--no-auth is only allowed when listening on this computer only.")
        return 2
    root = Path(data_dir) if data_dir else default_data_dir()
    root.mkdir(parents=True, exist_ok=True)

    if single_instance and loopback and not password:
        url = existing_instance(root)
        if url:
            _say(f"PDF Accessibility Studio is already running: {url}")
            if open_browser:
                webbrowser.open(url)
            return 0

    port = port or _free_port(host)
    store = DocStore(root, workers=workers, max_upload_bytes=max_upload_mb * 1024 * 1024)
    if retention_days:
        n = store.purge_older_than(retention_days)
        if n:
            _say(f"Removed {n} document(s) older than {retention_days:g} days.")
    resumed = store.resume_pending()
    if resumed:
        _say(f"Resuming {resumed} unfinished document(s).")

    if password:
        auth_mode = "password"
    elif no_auth:
        auth_mode = "none"
    else:
        auth_mode, token = "token", token or secrets.token_urlsafe(18)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} if loopback else None
    app = create_app(store, mode="local" if loopback else "server", auth_mode=auth_mode, token=token,
                     password=password, allowed_hosts=allowed, allow_quit=loopback)

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown_host}:{port}/"
    open_url = url + (f"?token={token}" if auth_mode == "token" else "")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", http="h11", ws="none"))
    app.state.server = server

    _say("PDF Accessibility Studio (browser version)")
    _say(f"  Data folder : {store.root}")
    _say(f"  Address     : {open_url}")
    if not loopback:
        _say("  NOTE: traffic on the network is not encrypted unless you put this behind HTTPS.")
    _say("  Press Ctrl+C (or use Quit in the app) to stop.")

    inst = _instance_file(root)
    if single_instance and loopback and auth_mode == "token":
        inst.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token, "started": time.time()}), encoding="utf-8")
    if idle_minutes:
        threading.Thread(target=_idle_watch, args=(app, server, idle_minutes), daemon=True).start()
    if open_browser and loopback:
        threading.Timer(1.2, webbrowser.open, [open_url]).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            inst.unlink()
        except OSError:
            pass
        app.state.exports.shutdown()
        store.shutdown()
    return 0
