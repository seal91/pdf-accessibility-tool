"""The browser version: upload, processing, review, edits, export, and the security rules."""
import io
import time
import zipfile

import pytest
from conftest import wait_ready
from starlette.testclient import TestClient

from pdfa11y import verapdf as vp
from pdfa11y.web.app import create_app
from pdfa11y.web.store import DocStore


@pytest.fixture()
def make_client(tmp_path):
    made = []

    def make(**kw):
        store = DocStore(tmp_path / f"data{len(made)}", workers=1)
        app = create_app(store, **kw)
        made.append((store, app))
        return TestClient(app, base_url="http://testserver")

    yield make
    for store, app in made:
        app.state.exports.shutdown()
        store.shutdown()


@pytest.fixture()
def client(make_client):
    return make_client(auth_mode="none")


def _upload(c, pdf, name="test document.pdf", folder=""):
    data = {"relpaths": [f"{folder}/{name}"]} if folder else {}
    with open(pdf, "rb") as fh:
        r = c.post("/api/documents", files=[("files", (name, fh, "application/pdf"))], data=data)
    assert r.status_code == 201, r.text
    return r.json()["results"][0]["id"]


def test_full_review_flow(client, test_pdf):
    doc = _upload(client, test_pdf, folder="Reports/2025")
    d = wait_ready(client, doc)
    assert d["document"]["status"] == "ready", d
    assert len(d["figures"]) == 1 and d["figures"][0]["status"] == "todo"

    # exporting is refused until the image is described
    assert client.post(f"/api/documents/{doc}/finalize", json={}).status_code == 409

    # the Pages tab data
    p = client.get(f"/api/documents/{doc}/pages/1").json()
    assert p["blocks"] and p["tree"] and p["width"] > 0
    assert client.get(f"/api/documents/{doc}/pages/1/image?scale=1").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/documents/{doc}/pages/99").status_code == 404

    # an edit, undo, redo
    para = next(b for b in p["blocks"] if b["role"] == "P")
    r = client.post(f"/api/documents/{doc}/edits", json={"op": "set_role", "args": [para["ref"], "Caption"]})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/documents/{doc}/history/undo").json()["changed"] is True
    assert client.post(f"/api/documents/{doc}/history/redo").json()["changed"] is True
    assert client.post(f"/api/documents/{doc}/edits", json={"op": "nonsense", "args": []}).status_code == 400

    # describe the image, then export
    key = d["figures"][0]["key"]
    r = client.put(f"/api/documents/{doc}/figures/{key}", json={"alt": "A small coloured square"})
    assert r.status_code == 200, r.text
    fin = client.post(f"/api/documents/{doc}/finalize", json={})
    assert fin.status_code == 200, fin.text
    dl = client.get(fin.json()["download"])
    assert dl.status_code == 200 and dl.content.startswith(b"%PDF")


def test_bulk_export_zip_keeps_folders(client, test_pdf):
    doc = _upload(client, test_pdf, folder="Reports/2025")
    d = wait_ready(client, doc)
    assert d["document"]["folder"] == "Reports/2025"
    client.put(f"/api/documents/{doc}/figures/{d['figures'][0]['key']}", json={"decorative": True})
    job = client.post("/api/exports", json={"ids": [doc], "suffix": True}).json()["job"]
    for _ in range(240):
        s = client.get(f"/api/exports/{job}").json()
        if s["state"] != "running":
            break
        time.sleep(0.5)
    assert s["state"] == "done", s
    assert s["items"][0]["outcome"] == "exported", s
    names = zipfile.ZipFile(io.BytesIO(client.get(s["download"]).content)).namelist()
    assert "Reports/2025/test document (accessible).pdf" in names
    assert "report.csv" in names and "report.html" in names


@pytest.mark.skipif(not vp.available(), reason="veraPDF is not installed here")
def test_validator_runs(client, test_pdf):
    doc = _upload(client, test_pdf)
    wait_ready(client, doc)
    r = client.post(f"/api/documents/{doc}/check")
    assert r.status_code == 200 and "failed_rules" in r.json()


def test_token_security_rules(make_client):
    c = make_client(auth_mode="token", token="sekret", allowed_hosts={"testserver"})
    assert c.get("/api/documents").status_code == 401                         # no key
    assert c.get("/?token=wrong").status_code == 401
    r = c.get("/?token=sekret", follow_redirects=False)
    assert "pdfa11y_auth" in r.headers["set-cookie"]
    assert c.get("/api/documents").status_code == 200                         # cookie now works
    assert c.get("/api/info", headers={"host": "evil.example"}).status_code >= 400
    assert c.post("/api/documents/000000000000/retry", headers={"origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/documents/..%2F..%2Fx").status_code == 404
    r = c.post("/api/documents", files=[("files", ("x.pdf", b"not really a pdf", "application/pdf"))])
    assert r.status_code == 400 or r.json()["results"][0].get("error")
    assert "frame-ancestors" in c.get("/static/style.css").headers["content-security-policy"]


def test_password_mode_and_https_cookie(make_client):
    c = make_client(mode="server", auth_mode="password", password="correct horse")
    assert c.get("/api/documents").status_code == 401
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    r = c.post("/login", data={"password": "correct horse"}, headers={"x-forwarded-proto": "https"},
               follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "secure" in r.headers["set-cookie"].lower()
